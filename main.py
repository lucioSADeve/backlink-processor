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
import whois

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
    try:
        # Remove protocolo e www se existirem
        url = re.sub(r'^https?://(www\.)?', '', str(url))
        # Pega apenas o domínio (primeira parte antes da primeira barra)
        domain = url.split('/')[0].strip().lower()
        return domain
    except:
        return ""

async def verify_domain(domain: str) -> bool:
    """Verifica se um domínio está disponível."""
    try:
        w = whois.whois(domain)
        return w.domain_name is None
    except Exception as e:
        logger.error(f"Erro ao verificar domínio {domain}: {str(e)}")
        return False

async def verify_domains(domains: List[str], process_id: str):
    """Verifica a disponibilidade de uma lista de domínios."""
    available_domains = []
    processed = 0
    total = len(domains)
    
    for domain in domains:
        try:
            is_available = await verify_domain(domain)
            processed += 1
            
            if is_available:
                available_domains.append(domain)
            
            # Atualiza o status
            processing_status[process_id].update({
                "current_domain": domain,
                "processed": processed,
                "total": total,
                "available": len(available_domains),
                "available_domains": available_domains
            })
            
            # Pequena pausa entre verificações
            await asyncio.sleep(1)
            
        except Exception as e:
            logger.error(f"Erro ao verificar domínio {domain}: {str(e)}")
            processed += 1
            continue

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/process")
async def process_file(file: UploadFile = File(...)):
    try:
        # Lê o arquivo Excel
        contents = await file.read()
        df = pd.read_excel(io.BytesIO(contents))
        
        # Lista de colunas possíveis
        source_columns = ['Source url', 'Source URL', 'Source URL (from)', 'Source URL (to)', 'Source']
        target_columns = ['Target url', 'Target URL', 'Target URL (from)', 'Target URL (to)', 'Target']
        domain_columns = ['Domain', 'Domain ascore', 'URL', 'Url']
        
        domains = []
        
        # Primeiro tenta ler da coluna B (índice 1)
        if len(df.columns) > 1:
            domains = df.iloc[:, 1].dropna().unique().tolist()
        
        # Se não encontrou domínios na coluna B, tenta outras colunas
        if not domains:
            # Procura por colunas conhecidas
            for col in df.columns:
                if col in source_columns or col in target_columns or col in domain_columns:
                    domains.extend(df[col].dropna().unique().tolist())
        
        # Se ainda não encontrou domínios, tenta qualquer coluna que pareça conter URLs
        if not domains:
            for col in df.columns:
                sample = str(df[col].iloc[0]).lower()
                if 'http' in sample or '.br' in sample:
                    domains.extend(df[col].dropna().unique().tolist())
        
        # Remove duplicatas
        domains = list(set(domains))
        
        # Limpa os domínios
        domains = [extract_domain(domain) for domain in domains if domain]
        domains = [d for d in domains if d and d.endswith(('.br', '.com.br'))]
        
        if not domains:
            raise ValueError("Nenhum domínio .br/.com.br encontrado no arquivo")
        
        logger.info(f"Total de domínios únicos encontrados: {len(domains)}")
        
        # Gera um ID único para este processamento
        process_id = str(time.time())
        
        # Inicializa o status do processamento
        processing_status[process_id] = {
            "status": "processing",
            "processed": 0,
            "total": len(domains),
            "available": 0,
            "available_domains": [],
            "current_domain": ""
        }
        
        # Inicia o processamento em background
        asyncio.create_task(verify_domains(domains, process_id))
        
        return {"process_id": process_id, "message": "Processamento iniciado"}
        
    except Exception as e:
        logger.error(f"Erro ao processar arquivo: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/progress/{process_id}")
async def get_progress(process_id: str):
    """Endpoint para verificar o progresso do processamento."""
    if process_id not in processing_status:
        raise HTTPException(status_code=404, detail="Processo não encontrado")
    
    status = processing_status[process_id]
    
    # Se terminou o processamento, marca como completed
    if status["processed"] >= status["total"]:
        status["status"] = "completed"
    
    return JSONResponse(content=status)

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