from fastapi import FastAPI, UploadFile, File, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import pandas as pd
import logging
import re
import time
from typing import List, Dict
import asyncio
import json
from datetime import datetime
import io

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Verificador de Backlink e Outbound-domains")

# Configuração dos templates
templates = Jinja2Templates(directory="templates")

# Dicionário para armazenar o status de processamento
processing_status: Dict[str, Dict] = {}

def extract_domain(url: str) -> str:
    """Extrai o domínio de uma URL."""
    if not url:
        return ""
    # Remove protocolo e www se existirem
    url = re.sub(r'^https?://(www\.)?', '', url)
    # Pega apenas o domínio (primeira parte antes da primeira barra)
    domain = url.split('/')[0]
    return domain.lower()

def clean_domain(domain: str) -> str:
    """Limpa o domínio removendo caracteres especiais."""
    if not domain:
        return ""
    # Remove caracteres especiais e espaços
    domain = re.sub(r'[^\w.-]', '', domain)
    return domain.lower()

async def verify_domain(domain: str) -> bool:
    """Verifica se um domínio está disponível."""
    try:
        # Simula verificação de domínio (em produção, use uma API real)
        await asyncio.sleep(1)  # Simula delay de rede
        return True  # Simula domínio disponível
    except Exception as e:
        logger.error(f"Erro ao verificar domínio {domain}: {str(e)}")
        return False

async def verify_domains(domains: List[str], process_id: str):
    """Verifica a disponibilidade de uma lista de domínios em lotes."""
    batch_size = 3  # Processa 3 domínios por vez
    available_domains_list = []
    processed_count = 0
    consecutive_errors = 0
    
    for i in range(0, len(domains), batch_size):
        batch = domains[i:i + batch_size]
        tasks = []
        
        for domain in batch:
            if consecutive_errors >= 3:
                logger.warning("Muitos erros consecutivos, aguardando 5 segundos...")
                await asyncio.sleep(5)
                consecutive_errors = 0
            
            tasks.append(verify_domain(domain))
            processed_count += 1
            
            # Atualiza o status
            processing_status[process_id].update({
                "current_domain": domain,
                "processed": processed_count,
                "total": len(domains),
                "available": len(available_domains_list),
                "available_domains": available_domains_list
            })
            
            # Pequena pausa entre verificações
            await asyncio.sleep(0.5)
        
        # Aguarda a conclusão do lote atual
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Processa os resultados
        for domain, result in zip(batch, results):
            if isinstance(result, bool) and result:
                available_domains_list.append(domain)
                consecutive_errors = 0
            else:
                consecutive_errors += 1

async def process_domains(domains: List[str], process_id: str, original_filename: str):
    """Processa os domínios em background e salva os resultados."""
    try:
        # Processa os domínios
        await verify_domains(domains, process_id)
        
        # Cria DataFrame com domínios disponíveis
        if processing_status[process_id]["available"] > 0:
            df = pd.DataFrame({
                'Domain': processing_status[process_id]["available_domains"]
            })
            
            # Gera nome do arquivo com timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_filename = f"disponiveis_{timestamp}.xlsx"
            
            # Salva o DataFrame em memória
            output_buffer = io.BytesIO()
            df.to_excel(output_buffer, index=False)
            output_buffer.seek(0)
            
            # Atualiza o status com o nome do arquivo e dados
            processing_status[process_id]["output_file"] = output_filename
            processing_status[process_id]["file_data"] = output_buffer.getvalue()
        
        # Marca o processamento como concluído
        processing_status[process_id]["status"] = "completed"
        
    except Exception as e:
        logger.error(f"Erro no processamento: {str(e)}")
        processing_status[process_id]["status"] = "error"
        processing_status[process_id]["error"] = str(e)

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/process")
async def process_file(file: UploadFile = File(...)):
    try:
        # Lê o arquivo Excel
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        
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
        
        # Inicializa o status do processamento
        processing_status[process_id] = {
            "status": "processing",
            "processed": 0,
            "total": len(br_domains),
            "available": 0,
            "available_domains": [],
            "current_domain": "",
            "output_file": None,
            "file_data": None
        }
        
        # Inicia o processamento em background
        asyncio.create_task(process_domains(br_domains, process_id, file.filename))
        
        return {"process_id": process_id, "message": "Processamento iniciado"}
        
    except Exception as e:
        logger.error(f"Erro ao processar arquivo: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/progress/{process_id}")
async def get_progress(process_id: str):
    """Endpoint para verificar o progresso do processamento."""
    if process_id not in processing_status:
        raise HTTPException(status_code=404, detail="Processo não encontrado")
    
    return JSONResponse(content=processing_status[process_id])

@app.get("/download/{filename}")
async def download_file(filename: str):
    """Endpoint para download do arquivo de domínios disponíveis."""
    try:
        # Procura o arquivo em todos os processos
        for process_id, status in processing_status.items():
            if status.get("output_file") == filename and status.get("file_data"):
                return JSONResponse(
                    content={
                        "status": "success",
                        "file_data": status["file_data"].decode('utf-8'),
                        "filename": filename
                    }
                )
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) 