# Verificador de Backlink e Outbound-domains

Uma aplicação web para verificar a disponibilidade de domínios .br e .com.br a partir de arquivos Excel contendo backlinks.

## Funcionalidades

- Upload de arquivos Excel
- Verificação automática de disponibilidade de domínios
- Exibição em tempo real do progresso
- Lista de domínios disponíveis com opção de cópia
- Download automático do arquivo com domínios disponíveis

## Como Usar

1. Acesse a aplicação em [URL_DO_SEU_APP]
2. Arraste e solte seu arquivo Excel ou clique para selecionar
3. Aguarde o processamento dos domínios
4. Copie os domínios disponíveis ou faça o download do arquivo

## Requisitos do Arquivo Excel

O arquivo Excel deve conter uma das seguintes colunas:
- Source URL / Target URL
- Domain
- URL

## Tecnologias Utilizadas

- Python
- FastAPI
- Pandas
- Python-whois
- HTML/CSS/JavaScript

## Desenvolvimento Local

1. Clone o repositório
2. Instale as dependências:
   ```bash
   pip install -r requirements.txt
   ```
3. Execute o servidor:
   ```bash
   python main.py
   ```
4. Acesse http://localhost:5000 