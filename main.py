from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse, HTMLResponse
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
import whois
import tempfile
from datetime import datetime

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
        w = whois.whois(domain)
        return w.domain_name is None
    except Exception as e:
        logger.error(f"Erro ao verificar domínio {domain}: {str(e)}")
        return False

async def verify_domains(domains: List[str], process_id: str):
    """Verifica a disponibilidade de uma lista de domínios em lotes."""
    batch_size = 5  # Processa 5 domínios por vez
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
            
            # Envia atualização de progresso
            await send_progress_update(process_id)
            
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
        # Cria diretório temporário
        with tempfile.TemporaryDirectory() as temp_dir:
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
                output_path = os.path.join(temp_dir, output_filename)
                
                # Salva o arquivo
                df.to_excel(output_path, index=False)
                
                # Atualiza o status com o nome do arquivo
                processing_status[process_id]["output_file"] = output_filename
                processing_status[process_id]["file_path"] = output_path
                
                # Envia atualização final
                await send_progress_update(process_id)
            
            # Marca o processamento como concluído
            processing_status[process_id]["status"] = "completed"
            
    except Exception as e:
        logger.error(f"Erro no processamento: {str(e)}")
        processing_status[process_id]["status"] = "error"
        processing_status[process_id]["error"] = str(e)

async def send_progress_update(process_id: str):
    """Envia atualização de progresso para o cliente."""
    if process_id in processing_status:
        status = processing_status[process_id]
        data = {
            "status": status.get("status", "processing"),
            "current_domain": status.get("current_domain", ""),
            "processed": status.get("processed", 0),
            "total": status.get("total", 0),
            "available": status.get("available", 0),
            "available_domains": status.get("available_domains", []),
            "output_file": status.get("output_file", "")
        }
        return data
    return None

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
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
        
        # Inicializa o status do processamento
        processing_status[process_id] = {
            "status": "processing",
            "processed": 0,
            "total": len(br_domains),
            "available": 0,
            "available_domains": [],
            "current_domain": "",
            "output_file": None,
            "file_path": None
        }
        
        # Inicia o processamento em background
        asyncio.create_task(process_domains(br_domains, process_id, file.filename))
        
        return {"process_id": process_id, "message": "Processamento iniciado"}
        
    except Exception as e:
        logger.error(f"Erro ao processar arquivo: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/progress/{process_id}")
async def get_progress(process_id: str):
    """Endpoint para receber atualizações de progresso via SSE."""
    async def event_generator():
        while True:
            if process_id not in processing_status:
                yield "data: " + json.dumps({"error": "Processo não encontrado"}) + "\n\n"
                break
                
            status = processing_status[process_id]
            if status["status"] in ["completed", "error"]:
                yield "data: " + json.dumps(status) + "\n\n"
                break
                
            yield "data: " + json.dumps(status) + "\n\n"
            await asyncio.sleep(1)
    
    return StreamingResponse(event_generator(), media_type="text/event-stream")

@app.get("/download/{filename}")
async def download_file(filename: str):
    """Endpoint para download do arquivo de domínios disponíveis."""
    try:
        # Procura o arquivo em todos os processos
        for process_id, status in processing_status.items():
            if status.get("output_file") == filename and status.get("file_path"):
                return FileResponse(
                    status["file_path"],
                    filename=filename,
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    
    # Criar diretórios necessários se não existirem
    for directory in ['uploads', 'cleaned', 'verified', 'processed', 'disponiveis']:
        if not os.path.exists(directory):
            os.makedirs(directory)
    
    # Configuração mais simples do servidor
    uvicorn.run("main:app", host="localhost", port=5000) 