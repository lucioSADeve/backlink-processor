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
from concurrent.futures import ThreadPoolExecutor

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Verificador de Backlink e Outbound-domains")

# Configuração dos templates
templates = Jinja2Templates(directory="templates")

# Dicionário para armazenar o status de processamento
processing_status: Dict[str, Dict] = {}

# Pool de threads para processamento paralelo
thread_pool = ThreadPoolExecutor(max_workers=3)

def extract_domain(url: str) -> str:
    """Extrai o domínio de uma URL."""
    if not url or not isinstance(url, str):
        return ""
    try:
        # Remove protocolo e www se existirem
        url = re.sub(r'^https?://(www\.)?', '', str(url))
        # Pega apenas o domínio (primeira parte antes da primeira barra)
        domain = url.split('/')[0].strip().lower()
        return domain
    except:
        return ""

def verify_domain_sync(domain: str) -> bool:
    """Versão síncrona da verificação de domínio."""
    try:
        domain = domain.strip().lower()
        if not domain or not domain.endswith(('.br', '.com.br')):
            return False
            
        if domain.startswith('www.'):
            domain = domain[4:]
            
        try:
            w = whois.whois(domain)
            return w.domain_name is None
        except Exception as e:
            logger.error(f"Erro na consulta whois para {domain}: {str(e)}")
            return False
            
    except Exception as e:
        logger.error(f"Erro ao verificar domínio {domain}: {str(e)}")
        return False

async def verify_domain(domain: str) -> bool:
    """Versão assíncrona da verificação de domínio usando thread pool."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(thread_pool, verify_domain_sync, domain)

async def verify_domains(domains: List[str], process_id: str):
    """Verifica a disponibilidade de uma lista de domínios."""
    available_domains = []
    processed = 0
    total = len(domains)
    errors = 0
    batch_size = 5  # Processa em lotes para evitar timeout
    
    try:
        for i in range(0, total, batch_size):
            batch = domains[i:i + batch_size]
            tasks = [verify_domain(domain) for domain in batch]
            
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                for domain, result in zip(batch, results):
                    processed += 1
                    
                    if isinstance(result, Exception):
                        errors += 1
                        logger.error(f"Erro ao verificar {domain}: {str(result)}")
                        continue
                        
                    if result:
                        available_domains.append(domain)
                    
                    # Atualiza o status
                    processing_status[process_id].update({
                        "status": "processing",
                        "current_domain": domain,
                        "processed": processed,
                        "total": total,
                        "available": len(available_domains),
                        "available_domains": available_domains,
                        "errors": errors
                    })
                    
            except Exception as batch_error:
                logger.error(f"Erro no processamento do lote: {str(batch_error)}")
                errors += len(batch)
                processed += len(batch)
                
            # Pequena pausa entre lotes
            await asyncio.sleep(0.5)
            
    except Exception as e:
        logger.error(f"Erro no processamento principal: {str(e)}")
        
    finally:
        # Atualiza o status final
        processing_status[process_id].update({
            "status": "completed",
            "processed": processed,
            "total": total,
            "available": len(available_domains),
            "available_domains": available_domains,
            "errors": errors
        })

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/process")
async def process_file(file: UploadFile = File(...)):
    try:
        if not file.filename.endswith(('.xlsx', '.xls')):
            raise ValueError("Por favor, envie apenas arquivos Excel (.xlsx ou .xls)")

        try:
            contents = await file.read()
            df = pd.read_excel(io.BytesIO(contents), engine='openpyxl')
        except Exception as e:
            logger.error(f"Erro ao ler arquivo Excel: {str(e)}")
            raise ValueError("Erro ao ler o arquivo Excel. Verifique se o arquivo está corrompido ou no formato correto.")
        
        domains = []
        
        try:
            # Primeiro tenta ler da coluna B (índice 1)
            if len(df.columns) > 1:
                col_data = df.iloc[:, 1].dropna().astype(str)
                if not col_data.empty:
                    domains.extend(col_data.tolist())
            
            # Se não encontrou domínios, tenta outras colunas
            if not domains:
                for col in df.columns:
                    try:
                        col_data = df[col].dropna().astype(str)
                        if not col_data.empty and any('.br' in str(x).lower() for x in col_data.head()):
                            domains.extend(col_data.tolist())
                    except:
                        continue
                        
            if not domains:
                raise ValueError("Nenhum domínio encontrado no arquivo")
                
        except Exception as e:
            logger.error(f"Erro ao extrair domínios: {str(e)}")
            raise ValueError("Erro ao processar as colunas do arquivo. Verifique o formato do arquivo.")
        
        # Limpa e filtra os domínios
        cleaned_domains = []
        for domain in domains:
            try:
                clean_domain = extract_domain(str(domain))
                if clean_domain and clean_domain.endswith(('.br', '.com.br')):
                    cleaned_domains.append(clean_domain)
            except:
                continue
        
        # Remove duplicatas
        domains = list(set(cleaned_domains))
        
        if not domains:
            raise ValueError("Nenhum domínio .br/.com.br válido encontrado no arquivo")
        
        if len(domains) > 100:
            domains = domains[:100]  # Limita a 100 domínios para evitar timeout
            
        # Gera um ID único para este processamento
        process_id = str(time.time())
        
        # Inicializa o status do processamento
        processing_status[process_id] = {
            "status": "processing",
            "processed": 0,
            "total": len(domains),
            "available": 0,
            "available_domains": [],
            "current_domain": "",
            "errors": 0
        }
        
        # Inicia o processamento em background
        asyncio.create_task(verify_domains(domains, process_id))
        
        return {"process_id": process_id, "message": "Processamento iniciado"}
        
    except ValueError as e:
        logger.error(f"Erro de validação: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Erro inesperado: {str(e)}")
        raise HTTPException(status_code=500, detail="Erro interno ao processar o arquivo. Por favor, tente novamente.")

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