from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pandas as pd
import os
from pathlib import Path
import logging
from urllib.parse import urlparse
import re
import requests
import time
from typing import Optional, List, Dict
import asyncio
import aiohttp
import json
import urllib.parse

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Processador de Domínios")

# Configuração dos arquivos estáticos e templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Criar diretórios necessários
os.makedirs("uploads", exist_ok=True)
os.makedirs("processed", exist_ok=True)
os.makedirs("cleaned", exist_ok=True)
os.makedirs("verified", exist_ok=True)
os.makedirs("disponiveis", exist_ok=True)  # Nova pasta para domínios disponíveis

# Dicionário para armazenar o progresso de cada processamento
processing_status = {}

def extract_domain(url):
    """Extrai o domínio de uma URL mantendo o TLD (.com.br, .br, etc)"""
    try:
        # Remove espaços e converte para minúsculas
        url = url.strip().lower()
        # Adiciona http:// se não tiver protocolo
        if not url.startswith(('http://', 'https://')):
            url = 'http://' + url
        # Extrai o domínio
        domain = urlparse(url).netloc
        # Remove www. se existir
        domain = re.sub(r'^www\.', '', domain)
        return domain
    except:
        return url.strip().lower()

def clean_domains_from_backlinks(df):
    """Limpa e organiza os domínios mantendo os dados originais"""
    logger.info("Iniciando limpeza de domínios do arquivo de backlinks")
    
    # Criar uma cópia do DataFrame original
    new_df = pd.DataFrame()
    
    # Identificar colunas que podem conter URLs
    url_columns = []
    for col in df.columns:
        sample = df[col].astype(str).iloc[0]
        if 'http' in sample.lower() or '.' in sample:
            url_columns.append(col)
            logger.info(f"Coluna com URLs encontrada: {col}")
    
    # Extrair e limpar domínios
    if url_columns:
        # Usar a primeira coluna que contém URLs
        domains = df[url_columns[0]].astype(str).apply(extract_domain)
    else:
        # Se não encontrar URLs, usar a primeira coluna
        domains = df.iloc[:, 0].astype(str).apply(extract_domain)
    
    # Criar DataFrame temporário com dados originais e domínios
    temp_df = pd.DataFrame({
        'original': df.iloc[:, 0],
        'domain': domains
    })
    
    # Filtrar apenas domínios válidos (.br e .com.br)
    temp_df = temp_df[temp_df['domain'].apply(lambda x: x.endswith('.br') if isinstance(x, str) else False)]
    
    # Remover duplicatas mantendo a primeira ocorrência de cada domínio
    temp_df = temp_df.drop_duplicates(subset='domain', keep='first')
    
    # Criar DataFrame final com a estrutura desejada
    new_df = pd.DataFrame({
        'A': temp_df['original'],
        'B': temp_df['domain']
    })
    
    logger.info(f"Total de domínios únicos .br/.com.br encontrados: {len(new_df)}")
    return new_df

async def check_domain_availability(domain: str) -> Optional[bool]:
    """Verifica a disponibilidade do domínio no Registro.br usando a API WHOIS"""
    try:
        # Remove protocolo e www se existir
        domain = domain.replace('http://', '').replace('https://', '').replace('www.', '')
        
        # Extrai apenas o domínio base (remove subdomínios)
        parts = domain.split('.')
        if len(parts) >= 3 and parts[-2:] == ['com', 'br']:
            # Para domínios .com.br, pegamos as últimas 3 partes
            domain = '.'.join(parts[-3:])
        elif len(parts) >= 2 and parts[-1] == 'br':
            # Para domínios .br, pegamos as últimas 2 partes
            domain = '.'.join(parts[-2:])
        else:
            logger.warning(f"Domínio {domain} não é .br ou .com.br")
            return None
        
        logger.info(f"Verificando disponibilidade do domínio: {domain}")
        
        # URL da API WHOIS do Registro.br
        url = f"https://rdap.registro.br/domain/{domain}"
        
        # Headers necessários para a requisição
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Connection': 'keep-alive'
        }
        
        # Faz a requisição usando aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=30) as response:
                # Se retornar 404, o domínio está disponível
                if response.status == 404:
                    logger.info(f"Domínio {domain}: Disponível")
                    return True
                    
                # Se retornar 200, o domínio está registrado
                elif response.status == 200:
                    logger.info(f"Domínio {domain}: Indisponível")
                    return False
                    
                # Outros códigos de status são considerados erro
                else:
                    logger.error(f"Erro ao verificar domínio {domain}. Status code: {response.status}")
                    return None
                    
    except asyncio.TimeoutError:
        logger.error(f"Timeout ao verificar domínio {domain}")
        return None
    except aiohttp.ClientError as e:
        logger.error(f"Erro na requisição para {domain}: {str(e)}")
        return None
    except Exception as e:
        logger.error(f"Erro ao verificar domínio {domain}: {str(e)}")
        return None

async def verify_domains(domains: List[str], process_id: str) -> List[Dict[str, str]]:
    """
    Verifica a disponibilidade de uma lista de domínios no Registro.br
    Retorna uma lista de dicionários com o domínio e seu status
    """
    results = []
    total_domains = len(domains)
    processed_domains = 0
    available_domains = 0
    available_domains_list = []  # Lista para armazenar domínios disponíveis
    consecutive_errors = 0
    base_delay = 3  # Delay base entre requisições
    error_delay = 15  # Delay quando há erros consecutivos

    for domain in domains:
        try:
            # Verifica disponibilidade
            is_available = await check_domain_availability(domain)
            processed_domains += 1
            
            if is_available:
                available_domains += 1
                available_domains_list.append(domain)  # Adiciona à lista de disponíveis
                results.append({
                    'domain': domain,
                    'status': 'Sim'
                })
            
            # Atualiza o progresso
            processing_status[process_id] = {
                'processed': processed_domains,
                'total': total_domains,
                'available': available_domains,
                'current_domain': domain,
                'available_domains': available_domains_list  # Inclui a lista de domínios disponíveis
            }
            
            # Ajusta o delay baseado em erros consecutivos
            if consecutive_errors > 0:
                await asyncio.sleep(error_delay)
                consecutive_errors = 0
            else:
                await asyncio.sleep(base_delay)
                
        except Exception as e:
            logger.error(f"Erro ao verificar domínio {domain}: {str(e)}")
            consecutive_errors += 1
            processed_domains += 1
            results.append({
                'domain': domain,
                'status': 'Erro'
            })
            await asyncio.sleep(error_delay)
    
    logger.info(f"Verificação concluída. Total: {total_domains}, Disponíveis: {available_domains}, Erros: {len(results) - available_domains}")
    return results, processed_domains, total_domains, available_domains, available_domains_list

async def send_progress_update(domain: str, processed: int, total: int, available: int, output_file: str = None, available_domains: List[str] = None):
    """Envia atualização de progresso para o cliente"""
    progress_data = {
        "domain": domain,
        "processed": processed,
        "total": total,
        "available": available,
        "percent": round((processed / total) * 100) if total > 0 else 0,
        "output_file": output_file,
        "available_domains": available_domains or []
    }
    return f"data: {json.dumps(progress_data)}\n\n"

@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    try:
        # Cria o diretório de uploads se não existir
        os.makedirs("uploads", exist_ok=True)
        
        # Remove espaços do nome do arquivo
        filename = file.filename.replace(" ", "_")
        
        # Salva o arquivo
        file_path = os.path.join("uploads", filename)
        with open(file_path, "wb") as buffer:
            content = await file.read()
            buffer.write(content)
        
        logger.info(f"Arquivo {filename} enviado com sucesso")
        return {"success": True, "filename": filename}
        
    except Exception as e:
        logger.error(f"Erro ao fazer upload do arquivo: {str(e)}")
        return {"success": False, "error": str(e)}

@app.get("/progress/{process_id}")
async def get_progress(process_id: str):
    """Endpoint para receber atualizações de progresso via SSE"""
    async def event_generator():
        while True:
            if process_id in processing_status:
                status = processing_status[process_id]
                yield await send_progress_update(
                    status['current_domain'],
                    status['processed'],
                    status['total'],
                    status['available'],
                    status.get('output_file'),
                    status.get('available_domains', [])
                )
                
                # Se o processamento terminou, remove o status
                if status['processed'] >= status['total']:
                    del processing_status[process_id]
                    break
            
            await asyncio.sleep(0.5)
    
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.post("/process")
async def process_file(file: UploadFile = File(...)):
    try:
        # Lê o arquivo Excel
        df = pd.read_excel(file.file)
        
        # Lista de colunas possíveis para cada tipo de arquivo
        source_columns = ['Source url', 'Source URL', 'Source URL (from)', 'Source URL (to)', 'Source']
        target_columns = ['Target url', 'Target URL', 'Target URL (from)', 'Target URL (to)', 'Target']
        domain_columns = ['Domain', 'Domain ascore', 'URL', 'Url']
        
        # Identifica as colunas relevantes
        source_col = None
        target_col = None
        domain_col = None
        
        # Procura por colunas de source
        for col in source_columns:
            if col in df.columns:
                source_col = col
                break
        
        # Procura por colunas de target
        for col in target_columns:
            if col in df.columns:
                target_col = col
                break
        
        # Procura por colunas de domain
        for col in domain_columns:
            if col in df.columns:
                domain_col = col
                break
        
        # Log das colunas encontradas
        logger.info(f"Colunas encontradas: {list(df.columns)}")
        
        # Extrai domínios baseado no tipo de arquivo
        if domain_col is not None:
            # Se encontrou coluna de domínio, usa ela diretamente
            logger.info(f"Usando coluna de domínio: {domain_col}")
            domains = df[domain_col].dropna().unique().tolist()
        elif source_col is not None and target_col is not None:
            # Se encontrou colunas de source e target, extrai domínios de ambas
            logger.info(f"Usando colunas de source ({source_col}) e target ({target_col})")
            source_domains = df[source_col].dropna().unique().tolist()
            target_domains = df[target_col].dropna().unique().tolist()
            domains = list(set(source_domains + target_domains))
        else:
            # Se não encontrou colunas específicas, tenta usar todas as colunas que podem conter URLs
            logger.info("Usando todas as colunas que podem conter URLs")
            all_domains = []
            for col in df.columns:
                sample = df[col].astype(str).iloc[0]
                if 'http' in sample.lower() or '.' in sample:
                    all_domains.extend(df[col].dropna().unique().tolist())
            domains = list(set(all_domains))
        
        # Limpa os domínios
        domains = [extract_domain(domain) for domain in domains if domain]
        domains = [domain for domain in domains if domain]
        
        # Filtra apenas domínios .br/.com.br
        br_domains = [domain for domain in domains if domain.endswith(('.br', '.com.br'))]
        logger.info(f"Total de domínios únicos .br/.com.br encontrados: {len(br_domains)}")
        
        if not br_domains:
            raise ValueError("Nenhum domínio .br/.com.br encontrado no arquivo")
        
        # Gera um ID único para este processamento
        process_id = str(time.time())
        
        # Inicia o processamento em background
        asyncio.create_task(process_domains(br_domains, process_id, file.filename))
        
        return {"process_id": process_id, "message": "Processamento iniciado"}
        
    except Exception as e:
        logger.error(f"Erro ao processar arquivo: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

async def process_domains(domains: List[str], process_id: str, filename: str):
    """Processa os domínios em background e salva o resultado"""
    try:
        # Verifica disponibilidade
        results, processed, total, available, available_domains_list = await verify_domains(domains, process_id)
        
        # Cria DataFrame com resultados
        results_df = pd.DataFrame(results)
        
        # Filtra apenas domínios disponíveis
        available_df = results_df[results_df['status'] == 'Sim']
        
        if not available_df.empty:
            # Salva apenas domínios disponíveis na pasta disponiveis
            output_filename = f"disponiveis_{filename}"
            output_path = os.path.join('disponiveis', output_filename)
            available_df.to_excel(output_path, index=False)
            logger.info(f"Arquivo com domínios disponíveis salvo em: {output_path}")
            
            # Atualiza o status com o nome do arquivo para download
            processing_status[process_id]['output_file'] = output_filename
        
        # Atualiza o status final
        processing_status[process_id] = {
            'processed': processed,
            'total': total,
            'available': available,
            'current_domain': 'Concluído',
            'completed': True,
            'available_domains': available_domains_list
        }
        
    except Exception as e:
        logger.error(f"Erro no processamento em background: {str(e)}")
        processing_status[process_id] = {
            'error': str(e),
            'completed': True
        }

@app.get("/download/{filename}")
async def download_file(filename: str):
    try:
        # Remove caracteres especiais e espaços do nome do arquivo
        clean_filename = re.sub(r'[^a-zA-Z0-9._-]', '_', filename)
        
        # Procurar o arquivo na pasta disponiveis
        file_path = os.path.join("disponiveis", clean_filename)
        
        if os.path.exists(file_path):
            logger.info(f"Iniciando download do arquivo: {file_path}")
            return FileResponse(
                file_path,
                filename=clean_filename,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={
                    "Content-Disposition": f"attachment; filename={clean_filename}"
                }
            )
        
        logger.error(f"Arquivo não encontrado: {file_path}")
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    except Exception as e:
        logger.error(f"Erro ao baixar arquivo: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    
    # Criar diretórios necessários se não existirem
    for directory in ['uploads', 'cleaned', 'verified', 'processed', 'disponiveis']:
        if not os.path.exists(directory):
            os.makedirs(directory)
    
    # Configuração mais simples do servidor
    uvicorn.run("main:app", host="localhost", port=5000) 