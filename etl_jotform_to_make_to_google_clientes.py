#!/usr/bin/env python3
"""ETL v2.0: arquivos brutos (Google Drive) -> .txt no formato do motor relatorio_churn_notion.py.

Uso:
    python etl_jotform_to_make_to_google_clientes.py                      # lê a pasta ./entrada (ou ETL_PASTA_ENTRADA)
    python etl_jotform_to_make_to_google_clientes.py arquivo.xlsx         # processa arquivos específicos
    python etl_jotform_to_make_to_google_clientes.py --drive              # baixa do Google Drive e processa
    python etl_jotform_to_make_to_google_clientes.py --mapa-status "ativo=ABERTO"

Entradas aceitas: .csv, .xlsx, .xls, .json, .txt (tabular, ou já no formato do motor).
Para cada arquivo gera, na pasta de saída:
    <nome>.txt             arquivo no formato do motor (só se o arquivo for aceito)
    <nome>_qualidade.json  relatório de qualidade (o que foi lido, descartado, mascarado, avisos)
    <nome>_mapa.csv        mapa TK-id -> ID original (contém dados pessoais: não compartilhe)

Regras do contrato com o motor:
    - CLIENTE vem do ticket: organização > domínio do e-mail corporativo > pseudônimo do e-mail pessoal.
      Nunca um valor fixo. Arquivo com menos de 2 clientes distintos é REJEITADO.
    - LTV só de coluna explícita de LTV; sem ela, 'R$ 0' em todos (o motor trata como "não informado").
    - STATUS: ticket aberto/pendente -> ABERTO; resolvido/fechado -> RESOLVIDO; CANCELADO somente com
      indicador explícito de cancelamento do cliente (contrato, flag, motivo, tag). Nunca de 'Closed'.
    - DATA em AAAA-MM-DD (datas ambíguas são lidas como dia/mês/ano). Ticket sem data é descartado.
    - RELATO em uma linha, sem HTML, sem os delimitadores do motor e com dados pessoais mascarados.

Dependências: pip install pandas openpyxl xlrd python-dotenv
              (--drive: pip install google-api-python-client google-auth)
"""

import argparse
import hashlib
import html
import json
import os
import re
import sys
import unicodedata
import warnings
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dotenv import dotenv_values, find_dotenv, load_dotenv


# ===== BLOCO DE CONFIGURAÇÃO (idêntico nos scripts) ============================
def carregar_env():
    """Carrega UM .env de forma explícita e informa conflitos com variáveis já definidas no sistema."""
    caminho = Path(__file__).resolve().with_name(".env")
    if not caminho.exists():
        achado = find_dotenv(usecwd=True)
        caminho = Path(achado) if achado else None
    if caminho is None:
        return None, []
    valores = dotenv_values(caminho)
    conflitos = sorted(k for k, v in valores.items() if k in os.environ and os.environ[k] != v)
    load_dotenv(caminho, override=True)
    return caminho, conflitos


ENV_PATH, ENV_CONFLITOS = carregar_env()
# ===============================================================================

EXTENSOES = {".csv", ".xlsx", ".xls", ".json", ".txt"}
VERSAO = "2.0"

# --------------------------------------------------------------- sinônimos de colunas
SINONIMOS = {
    "id": ["id", "ticket id", "id do ticket", "ticket", "numero", "numero do ticket", "protocolo", "key", "issue key"],
    "assunto": ["subject", "assunto", "titulo", "title", "summary", "resumo"],
    "descricao": ["description", "descricao", "body", "mensagem", "message", "texto", "conteudo", "detalhes",
                  "comentario", "relato", "content", "first comment", "primeiro comentario"],
    "status": ["status", "estado", "situacao", "ticket status", "status do ticket"],
    "data": ["created at", "created", "created_at", "creation date", "data de criacao", "data criacao",
             "criado em", "data de abertura", "data abertura", "abertura", "opened at", "date", "data"],
    "contrato": ["status do contrato", "contract status", "status da conta", "account status", "customer status",
                 "status do cliente", "situacao do contrato", "situacao do cliente"],
    "motivo_cancel": ["cancellation reason", "motivo de cancelamento", "motivo do cancelamento", "churn reason",
                      "motivo churn"],
    "churn_flag": ["churn", "churned", "cancelado", "cancelled", "canceled", "is churned"],
    "tags": ["tags", "tag", "labels", "etiquetas"],
    "ltv": ["ltv", "lifetime value", "valor vitalicio", "customer lifetime value"],
    "mrr": ["mrr", "receita mensal", "monthly recurring revenue"],
    "canal": ["channel", "canal", "via", "source", "origem"],
    "email": ["requester email", "email do solicitante", "e mail", "email", "requester", "solicitante",
              "contact email", "customer email", "email do cliente"],
    "cliente": ["organization", "organization name", "organizacao", "empresa", "company", "company name",
                "cliente", "customer", "account", "account name", "conta", "razao social", "nome fantasia"],
}
SUBSTRING_OK = {"descricao", "cliente", "email", "ltv", "contrato", "motivo_cancel", "tags"}
TODAS_SINONIMOS = {s for lst in SINONIMOS.values() for s in lst}

FREEMAIL = {"gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "yahoo.com.br", "uol.com.br", "bol.com.br",
            "terra.com.br", "icloud.com", "live.com", "msn.com", "ig.com.br", "me.com", "proton.me", "protonmail.com"}
VAZIOS = {"", "nan", "none", "null", "-", "n/a", "na", "nat", "<na>"}

STATUS_ABERTO = {"open", "new", "pending", "hold", "on hold", "aberto", "novo", "pendente", "aguardando",
                 "em andamento", "em atendimento", "in progress", "waiting", "reopened", "reaberto",
                 "aguardando cliente", "aguardando resposta"}
STATUS_RESOLVIDO = {"solved", "closed", "resolved", "done", "resolvido", "fechado", "concluido", "encerrado",
                    "finalizado", "completed"}
CANCEL_RE = re.compile(r"cancel|churn|inativ|rescind|perdid", re.I)
FLAG_VERDADEIRO = {"1", "true", "sim", "yes", "y", "s", "verdadeiro", "x", "cancelado", "churned"}

# cópia do REGISTRO do motor (manter sincronizada) para validar a saída antes de entregar
REGISTRO_MOTOR = re.compile(
    r"ID:\s*(?P<id>TK-\d+)\s*\|\s*CANAL:\s*(?P<canal>[^|\n]+?)\s*\|\s*DATA:\s*(?P<data>\S+)"
    r"\s*\|\s*CLIENTE:\s*(?P<cliente>[^|\n]+?)\s*\|\s*LTV:\s*R\$\s*(?P<ltv>[\d\.,]+)\s*"
    r"STATUS DO CLIENTE:\s*(?P<status>[^\n]+?)\s*RELATO:\s*(?P<relato>.+?)\s*--- FIM",
    re.S,
)


# ------------------------------------------------------------------ utilitários
def _norm(s):
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def sha1_arquivo(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    return h.hexdigest()


def nome_seguro(nome):
    return re.sub(r"[^\w.\-]+", "_", Path(nome).name, flags=re.U)


# ------------------------------------------------------------------ leitura
def dedup_nomes(nomes):
    vistos, saida = Counter(), []
    for n in nomes:
        vistos[n] += 1
        saida.append(n if vistos[n] == 1 else f"{n}_{vistos[n]}")
    return saida


def detectar_cabecalho(raw):
    """Acha a linha de cabeçalho entre as 15 primeiras (planilhas com título/linhas em branco no topo)."""
    melhor, idx = -1, 0
    for i in range(min(15, len(raw))):
        score = sum(1 for v in raw.iloc[i].tolist() if isinstance(v, str) and _norm(v) in TODAS_SINONIMOS)
        if score > melhor:
            melhor, idx = score, i
    if melhor < 2:
        idx = 0
    cab = [str(v).strip() if isinstance(v, str) and str(v).strip() else f"coluna_{j}"
           for j, v in enumerate(raw.iloc[idx].tolist())]
    df = raw.iloc[idx + 1:].copy()
    df.columns = dedup_nomes(cab)
    return df.reset_index(drop=True), idx


def ler_csv_bruto(path):
    """Devolve (tabela_bruta, codificação, nº de linhas malformadas ignoradas)."""
    ultimo = None
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        melhor = None  # (colunas, -ruins, raw, ruins)
        for sep in (None, ";", ",", "\t", "|"):
            ruins = []
            try:
                with warnings.catch_warnings(record=True) as avisos_pd:
                    warnings.simplefilter("always")
                    raw = pd.read_csv(path, sep=sep, engine="python", header=None, dtype=str, encoding=enc,
                                      keep_default_na=False, on_bad_lines="warn")
                ruins = [w for w in avisos_pd if "Skipping line" in str(w.message)]
            except UnicodeDecodeError as e:
                ultimo, melhor = e, None
                break
            except (pd.errors.ParserError, pd.errors.EmptyDataError, ValueError, csv_error()) as e:
                ultimo = e
                continue
            cand = (raw.shape[1], -len(ruins), raw, len(ruins))
            if melhor is None or cand[:2] > melhor[:2]:
                melhor = cand
            if sep is None and raw.shape[1] > 1:
                break
        if melhor is not None:
            return melhor[2], enc, melhor[3]
    raise ValueError(f"não consegui ler o arquivo como tabela: {ultimo}")


def csv_error():
    import csv
    return csv.Error


def ler_excel(path):
    engine = "xlrd" if path.suffix.lower() == ".xls" else "openpyxl"
    abas = pd.read_excel(path, sheet_name=None, header=None, dtype=str, engine=engine)
    melhor = None
    for nome, raw in abas.items():
        raw = raw.fillna("")
        if raw.empty:
            continue
        df, idx = detectar_cabecalho(raw)
        score = len(mapear_colunas(list(df.columns))) * 1_000_000 + len(df)
        if melhor is None or score > melhor[0]:
            melhor = (score, nome, df, idx)
    if melhor is None:
        raise ValueError("planilha vazia")
    return melhor[2], {"aba": str(melhor[1]), "linha_cabecalho": melhor[3] + 1}


def encontrar_lista(dados):
    if isinstance(dados, list):
        return dados
    if isinstance(dados, dict):
        for chave in ("tickets", "results", "data", "items", "records", "issues", "conversations"):
            if isinstance(dados.get(chave), list):
                return dados[chave]
        listas = [v for v in dados.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        if listas:
            return max(listas, key=len)
        return [dados]
    raise ValueError("JSON sem lista de registros")


def ler_json(path):
    with open(path, encoding="utf-8-sig") as f:
        texto = f.read()
    try:
        dados = json.loads(texto)
    except json.JSONDecodeError:  # JSON Lines
        dados = [json.loads(l) for l in texto.splitlines() if l.strip()]
    registros = [r for r in encontrar_lista(dados) if isinstance(r, dict)]
    if not registros:
        raise ValueError("JSON sem registros (objetos)")
    df = pd.json_normalize(registros, sep=".")
    return df.fillna("").astype(str), {}


def ler_arquivo(path):
    """Devolve (df, info). Para .txt já no formato do motor devolve (None, {'passthrough': texto})."""
    ext = path.suffix.lower()
    if ext == ".json":
        return ler_json(path)
    if ext in (".xlsx", ".xls"):
        return ler_excel(path)
    if ext in (".csv", ".txt"):
        if ext == ".txt":
            for enc in ("utf-8-sig", "cp1252"):
                try:
                    texto = path.read_text(encoding=enc)
                    break
                except UnicodeDecodeError:
                    texto = None
            if texto and "ID: TK-" in texto and "--- FIM" in texto and "STATUS DO CLIENTE:" in texto:
                return None, {"passthrough": texto}
        raw, enc, ruins = ler_csv_bruto(path)
        if raw.shape[1] < 2:
            raise ValueError("arquivo de texto sem estrutura tabular (use CSV, XLSX ou JSON)")
        df, idx = detectar_cabecalho(raw)
        return df, {"codificacao": enc, "linha_cabecalho": idx + 1, "linhas_malformadas": ruins}
    raise ValueError(f"extensão não suportada: {ext}")


# ------------------------------------------------------------------ mapeamento de colunas
def mapear_colunas(colunas):
    normed = {c: _norm(c) for c in colunas}
    usados, mapa = set(), {}
    for campo, sins in SINONIMOS.items():
        for sin in sins:
            achada = next((c for c, n in normed.items() if n == sin and c not in usados), None)
            if achada:
                mapa[campo] = achada
                usados.add(achada)
                break
        else:
            if campo in SUBSTRING_OK:
                for sin in sins:
                    achada = next((c for c, n in normed.items() if sin in n and c not in usados), None)
                    if achada:
                        mapa[campo] = achada
                        usados.add(achada)
                        break
    return mapa


# ------------------------------------------------------------------ limpeza de texto
INVISIVEIS = re.compile("[\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060\ufeff\u00ad]")
CONTROLE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
CNPJ_RE = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")
CPF_RE = re.compile(r"(?<!\d)\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?!\d)")
TEL_RE = re.compile(
    r"\(\d{2}\)\s?9?\d{4}[-\s]?\d{4}|\+55\s?\d{2}\s?9?\d{4}[-\s]?\d{4}|(?<!\d)\d{2}\s?9\d{4}[-\s]?\d{4}(?!\d)"
    r"|(?<!\d)9?\d{4}-\d{4}(?!\d)"
)


def corrigir_mojibake(s):
    if re.search(r"Ã[\x80-\xbf¡-¿]|â€", s):
        for enc in ("cp1252", "latin-1"):
            try:
                return s.encode(enc).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
    return s


def limpar_texto(s, mascarar, contadores):
    s = "" if s is None else str(s)
    s = corrigir_mojibake(s)
    s = html.unescape(s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = INVISIVEIS.sub("", s).replace("\xa0", " ")
    s = CONTROLE.sub(" ", s)
    s = re.sub(r"[“”„«»\"]", "'", s)  # aspas duplas -> simples (evita pareamento errado nas citações)
    s = re.sub(r"(?i)-{2,}\s*(?:in[ií]cio do registro|fim)\b\s*-*", " ", s)  # delimitadores do motor
    s = re.sub(r"(?i)\bID:\s*TK-(\d+)", r"ID TK-\1", s)
    if mascarar:
        for nome, rx, rotulo in (("cnpj", CNPJ_RE, "[cnpj]"), ("cpf", CPF_RE, "[cpf]"),
                                 ("email", EMAIL_RE, "[email]"), ("telefone", TEL_RE, "[telefone]")):
            s, n = rx.subn(rotulo, s)
            contadores[nome] += n
    return " ".join(s.split())


def limpar_curto(s):
    s = html.unescape("" if s is None else str(s))
    s = INVISIVEIS.sub("", s).replace("\xa0", " ")
    s = CONTROLE.sub(" ", s).replace("|", "/")
    s = " ".join(s.split())
    return "" if s.lower() in VAZIOS else s


# ------------------------------------------------------------------ campos
def parse_valor(s):
    s = re.sub(r"[^\d,.\-]", "", str(s))
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if re.fullmatch(r"-?\d+,\d{1,2}", s) else s.replace(",", "")
    elif re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def fmt_brl(v):
    if not v or v <= 0:
        return "R$ 0"
    return "R$ " + f"{v:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


FORMATOS_DATA = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y", "%d/%m/%Y %H:%M:%S",
                 "%d/%m/%Y %H:%M", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y")


def parse_data(s):
    s = str(s).strip()
    if not s or s.lower() in VAZIOS:
        return None
    if re.fullmatch(r"\d{5}(\.\d+)?", s) and 20000 <= float(s) <= 70000:  # serial do Excel
        return datetime(1899, 12, 30) + timedelta(days=float(s))
    for fmt in FORMATOS_DATA:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        d = pd.to_datetime(s, dayfirst=True, errors="coerce", utc=True)
    if pd.isna(d):
        return None
    return d.tz_localize(None).to_pydatetime()


def extrair_email(s):
    m = EMAIL_RE.search(str(s or ""))
    return m.group(0).lower() if m else ""


def resolver_cliente(org, email):
    if org:
        return org, "organizacao", ""
    if email:
        dom = email.split("@")[-1]
        if dom in FREEMAIL:
            return f"Pessoa-{hashlib.sha1(email.encode()).hexdigest()[:8]}", "pseudonimo_email", email
        return dom, "dominio_email", ""
    return "", "sem_cliente", ""


def mapear_status(txt, mapa_extra):
    n = _norm(txt)
    if n in mapa_extra:
        return mapa_extra[n]
    if CANCEL_RE.search(n):
        return "CANCELADO"
    if n in STATUS_RESOLVIDO or re.search(r"resolv|solv|closed|fechad|conclu|encerr|finaliz", n):
        return "RESOLVIDO"
    if n in STATUS_ABERTO or re.search(r"abert|open|pend|andament|aguard|novo|new|hold|progress", n):
        return "ABERTO"
    return None


def indicador_cancelamento(row, cols):
    """Retorna (True, campo) se houver indicador EXPLÍCITO de cancelamento do cliente nesta linha."""
    if "churn_flag" in cols and _norm(row.get(cols["churn_flag"], "")) in FLAG_VERDADEIRO:
        return True, "churn_flag"
    if "contrato" in cols and CANCEL_RE.search(_norm(row.get(cols["contrato"], ""))):
        return True, "contrato"
    if "motivo_cancel" in cols and limpar_curto(row.get(cols["motivo_cancel"], "")):
        return True, "motivo_cancel"
    if "tags" in cols and re.search(r"cancel|churn", _norm(row.get(cols["tags"], ""))):
        return True, "tags"
    return False, ""


# ------------------------------------------------------------------ processamento de um arquivo
def validar_saida(texto, min_clientes):
    regs = list(REGISTRO_MOTOR.finditer(texto))
    esperado = len(re.findall(r"ID:\s*TK-\d+", texto))
    clientes = {m.group("cliente").strip().casefold() for m in regs}
    erros = []
    if len(regs) != esperado:
        erros.append(f"saída inconsistente: {esperado} cabeçalhos, {len(regs)} registros válidos")
    if len(clientes) < min_clientes:
        erros.append(f"só {len(clientes)} cliente(s) distinto(s); o motor exige no mínimo {min_clientes}")
    return len(regs), len(clientes), erros


def processar_arquivo(path, saida, a, mapa_extra):
    log = {"arquivo": path.name, "versao_etl": VERSAO, "sha1": sha1_arquivo(path), "estado": "REJEITADO",
           "motivos": [], "avisos": []}
    stem = nome_seguro(path.stem)
    try:
        df, info = ler_arquivo(path)
    except Exception as e:  # leitura é a fronteira com dados externos: qualquer falha vira motivo de rejeição
        log["motivos"].append(f"leitura: {e}")
        return log
    log["leitura"] = {k: v for k, v in info.items() if k != "passthrough"}

    if df is None:  # já no formato do motor
        n, nc, erros = validar_saida(info["passthrough"], a.min_clientes)
        if erros:
            log["motivos"] += erros
            return log
        (saida / f"{stem}.txt").write_text(info["passthrough"], encoding="utf-8")
        log.update(estado="OK_JA_NO_FORMATO", tickets_emitidos=n, clientes_distintos=nc)
        return log

    df.columns = [str(c) for c in df.columns]
    df = df.fillna("").astype(str)
    malformadas = int(info.get("linhas_malformadas", 0))
    log["linhas_lidas"] = len(df)
    log["linhas_malformadas"] = malformadas
    if malformadas:
        log["avisos"].append(f"{malformadas} linha(s) malformada(s) do arquivo (aspas ou separadores quebrados) "
                             "foram ignoradas; confira a exportação")
    cols = mapear_colunas(list(df.columns))
    log["colunas_mapeadas"] = cols
    log["colunas_originais"] = list(df.columns)[:40]

    falta = [c for c, rot in (("data", "data de criação"), ("status", "status"))
             if c not in cols] + ([] if ("descricao" in cols or "assunto" in cols) else ["relato (assunto/descrição)"])
    if falta:
        log["motivos"].append(f"colunas obrigatórias não encontradas: {', '.join(falta)}")
        return log
    if "cliente" not in cols and "email" not in cols:
        log["motivos"].append("sem coluna de cliente (organização/empresa) nem de e-mail do solicitante: "
                              "sem identificar o cliente de cada ticket não há análise de churn")
        return log
    if "mrr" in cols and "ltv" not in cols:
        log["avisos"].append(f"coluna MRR ({cols['mrr']}) encontrada mas NÃO usada como LTV; LTV ficará R$ 0")

    descartes, contadores, datas_dm = Counter(), Counter(), []
    status_nao_mapeados = Counter()
    indicadores = Counter()
    origem_cliente = Counter()
    registros = []
    for ordem, row in enumerate(df.to_dict("records")):
        # data
        bruto_data = row.get(cols["data"], "")
        m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/\d{2,4}", bruto_data)
        if m:
            datas_dm.append((int(m.group(1)), int(m.group(2))))
        dt = parse_data(bruto_data)
        if dt is None:
            descartes["sem_data"] += 1
            continue
        # relato
        assunto = limpar_texto(row.get(cols.get("assunto", ""), ""), not a.sem_mascara, contadores)
        desc = limpar_texto(row.get(cols.get("descricao", ""), ""), not a.sem_mascara, contadores)
        if desc and assunto and not desc.lower().startswith(assunto.lower()[:40]):
            relato = f"{assunto}. {desc}"
        else:
            relato = desc or assunto
        if len(relato) < 3:
            descartes["sem_relato"] += 1
            continue
        truncado = len(relato) > a.max_relato
        if truncado:
            relato = relato[:a.max_relato - 1].rstrip() + "…"
            contadores["relatos_truncados"] += 1
        # cliente
        org = limpar_curto(row.get(cols.get("cliente", ""), ""))
        email = extrair_email(row.get(cols.get("email", ""), ""))
        cliente, origem, email_original = resolver_cliente(org, email)
        if not cliente:
            descartes["sem_cliente"] += 1
            continue
        origem_cliente[origem] += 1
        # status
        cancelado, campo = indicador_cancelamento(row, cols)
        status_ticket = mapear_status(row.get(cols["status"], ""), mapa_extra)
        if cancelado:
            status = "CANCELADO"
            indicadores[campo] += 1
        elif status_ticket == "CANCELADO":
            status = "CANCELADO"
            indicadores["status"] += 1
        elif status_ticket:
            status = status_ticket
        else:
            descartes["status_nao_mapeado"] += 1
            status_nao_mapeados[limpar_curto(row.get(cols["status"], "")) or "(vazio)"] += 1
            continue
        # ltv
        ltv = parse_valor(row.get(cols["ltv"], "")) if "ltv" in cols else None
        if ltv is not None and ltv < 0:
            ltv = None
        registros.append({
            "dt": dt, "ordem": ordem, "id_original": limpar_curto(row.get(cols.get("id", ""), "")),
            "canal": limpar_curto(row.get(cols.get("canal", ""), "")) or "Upload",
            "cliente": cliente, "email_original": email_original, "ltv": ltv or 0.0,
            "status": status, "relato": relato,
        })

    # duplicados exatos (mesmo cliente, data e relato)
    vistos, unicos = set(), []
    for r in registros:
        chave = (r["cliente"].casefold(), r["dt"].date(), r["relato"].casefold())
        if chave in vistos:
            descartes["duplicado"] += 1
            continue
        vistos.add(chave)
        unicos.append(r)
    registros = unicos

    ambiguo = bool(datas_dm) and all(d <= 12 and m <= 12 for d, m in datas_dm)
    if ambiguo:
        log["avisos"].append("todas as datas dd/mm podem ser lidas nos dois sentidos; assumido DIA/MÊS/ANO")
    total_lido = len(df) + malformadas
    log["descartes"] = dict(descartes)
    log["status_nao_mapeados"] = dict(status_nao_mapeados.most_common(10))
    if status_nao_mapeados:
        log["avisos"].append("status não mapeados foram descartados; use --mapa-status 'valor=ABERTO|RESOLVIDO|CANCELADO'")
    perdidas = sum(v for k, v in descartes.items() if k != "duplicado") + malformadas  # duplicata não é perda
    taxa_descarte = perdidas / total_lido if total_lido else 1
    if not registros:
        log["motivos"].append("nenhum ticket válido depois da limpeza")
        return log
    if taxa_descarte > a.max_descarte:
        log["motivos"].append(f"{taxa_descarte:.0%} das linhas descartadas (limite {a.max_descarte:.0%}); "
                              "revise o mapeamento de colunas e status")
        return log

    # teto de tickets: mantém os mais recentes
    registros.sort(key=lambda r: (r["dt"], r["ordem"]))
    if len(registros) > a.max_tickets:
        log["avisos"].append(f"{len(registros)} tickets válidos; mantidos os {a.max_tickets} mais recentes "
                             "(o motor não foi validado acima de 50 tickets)")
        registros = registros[-a.max_tickets:]

    if "organizacao" in origem_cliente and ({"dominio_email", "pseudonimo_email"} & set(origem_cliente)):
        log["avisos"].append("clientes identificados por critérios mistos (organização e e-mail): o mesmo cliente "
                             "pode aparecer duas vezes e distorcer o churn; preencha a organização na origem")
    com_ltv = [r for r in registros if r["ltv"] > 0]
    if com_ltv and len(com_ltv) < len(registros):
        log["avisos"].append(f"LTV informado em {len(com_ltv)} de {len(registros)} tickets; "
                             "os demais ficam R$ 0 e os totais em R$ serão subestimados")
    ltv_informado = bool(com_ltv)
    cancel_ind = bool(indicadores)
    if not cancel_ind:
        log["avisos"].append("sem indicador explícito de cancelamento: o churn NÃO é mensurável "
                             "(o relatório será uma análise de tickets)")

    largura = max(4, len(str(len(registros))))
    blocos, mapa_linhas = [], []
    for i, r in enumerate(registros, 1):
        tk = f"TK-{i:0{largura}d}"
        blocos.append(
            f"--- INÍCIO DO REGISTRO ---\n"
            f"ID: {tk} | CANAL: {r['canal']} | DATA: {r['dt']:%Y-%m-%d} | CLIENTE: {r['cliente']} | LTV: {fmt_brl(r['ltv'])}\n"
            f"STATUS DO CLIENTE: {r['status']}\n"
            f"RELATO: {r['relato']}\n"
            f"--- FIM\n"
        )
        mapa_linhas.append((tk, r["id_original"], r["cliente"], r["email_original"]))
    texto = "\n".join(blocos)

    n, nc, erros = validar_saida(texto, a.min_clientes)
    if erros:
        log["motivos"] += erros
        log["clientes_distintos"] = nc
        return log

    (saida / f"{stem}.txt").write_text(texto, encoding="utf-8")
    pd.DataFrame(mapa_linhas, columns=["tk_id", "id_original", "cliente_emitido", "email_original"]).to_csv(
        saida / f"{stem}_mapa.csv", index=False, encoding="utf-8-sig")
    st = Counter(r["status"] for r in registros)
    log.update(
        estado="OK_COM_AVISOS" if log["avisos"] else "OK",
        tickets_emitidos=n, clientes_distintos=nc, status=dict(st), ltv_informado=ltv_informado,
        churn_mensuravel=cancel_ind, indicadores_cancelamento=dict(indicadores),
        origem_cliente=dict(origem_cliente), mascarados=dict(contadores),
        periodo=f"{registros[0]['dt']:%Y-%m-%d} a {registros[-1]['dt']:%Y-%m-%d}",
    )
    return log


# ------------------------------------------------------------------ Google Drive
def baixar_do_drive(destino):
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError:
        sys.exit("Para --drive instale: pip install google-api-python-client google-auth")
    cred = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    pasta = os.environ.get("DRIVE_FOLDER_ID", "").strip()
    if not cred or not pasta:
        sys.exit("Para --drive defina GOOGLE_SERVICE_ACCOUNT_FILE e DRIVE_FOLDER_ID no .env "
                 "(e compartilhe a pasta com o e-mail da conta de serviço).")
    if not Path(cred).is_file():
        sys.exit(f"Arquivo de credencial não encontrado: {cred}")
    creds = service_account.Credentials.from_service_account_file(
        cred, scopes=["https://www.googleapis.com/auth/drive.readonly"])
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    arquivos, token = [], None
    while True:
        resp = svc.files().list(
            q=f"'{pasta}' in parents and trashed = false", pageSize=100, pageToken=token,
            fields="nextPageToken, files(id,name,mimeType)", supportsAllDrives=True,
            includeItemsFromAllDrives=True).execute()
        arquivos += resp.get("files", [])
        token = resp.get("nextPageToken")
        if not token:
            break
    destino.mkdir(parents=True, exist_ok=True)
    baixados = []
    for f in arquivos:
        nome, mime = f["name"], f["mimeType"]
        if mime == "application/vnd.google-apps.spreadsheet":
            req = svc.files().export_media(
                fileId=f["id"], mimeType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            nome = Path(nome).stem + ".xlsx"
        elif Path(nome).suffix.lower() in EXTENSOES:
            req = svc.files().get_media(fileId=f["id"], supportsAllDrives=True)
        else:
            print(f"Ignorado no Drive (extensão não suportada): {nome}")
            continue
        alvo = destino / nome_seguro(nome)
        with open(alvo, "wb") as fh:
            dl, pronto = MediaIoBaseDownload(fh, req), False
            while not pronto:
                _, pronto = dl.next_chunk()
        baixados.append(alvo)
    print(f"Drive: {len(baixados)} arquivo(s) baixado(s) em {destino}")
    return baixados


# ------------------------------------------------------------------ main
def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="ETL multiformato: arquivos brutos -> .txt do motor de churn")
    ap.add_argument("arquivos", nargs="*", help="arquivos a processar (padrão: todos da pasta de entrada)")
    ap.add_argument("--pasta-local", default=os.environ.get("ETL_PASTA_ENTRADA", "entrada"))
    ap.add_argument("--saida", default=os.environ.get("ETL_PASTA_SAIDA", "saida"))
    ap.add_argument("--drive", action="store_true", help="baixa os arquivos da pasta do Google Drive antes")
    ap.add_argument("--forcar", action="store_true", help="reprocessa arquivos já processados (mesmo conteúdo)")
    ap.add_argument("--max-tickets", type=int, default=300)
    ap.add_argument("--max-relato", type=int, default=1500)
    ap.add_argument("--max-descarte", type=float, default=0.3, help="fração máxima de linhas descartadas")
    ap.add_argument("--min-clientes", type=int, default=2)
    ap.add_argument("--sem-mascara", action="store_true", help="não mascara e-mail, telefone, CPF e CNPJ")
    ap.add_argument("--mapa-status", action="append", default=[], metavar="VALOR=STATUS")
    a = ap.parse_args()

    mapa_extra = {}
    for item in a.mapa_status:
        if "=" not in item or item.split("=", 1)[1].strip().upper() not in {"ABERTO", "RESOLVIDO", "CANCELADO"}:
            sys.exit(f"--mapa-status inválido: {item!r} (use valor=ABERTO|RESOLVIDO|CANCELADO)")
        k, v = item.split("=", 1)
        mapa_extra[_norm(k)] = v.strip().upper()

    print(f".env carregado: {ENV_PATH or 'nenhum'} | ETL v{VERSAO}")
    for nome in ENV_CONFLITOS:
        print(f"AVISO: {nome} já estava definida no sistema com valor diferente do .env; usado o do .env.",
              file=sys.stderr)

    entrada, saida = Path(a.pasta_local), Path(a.saida)
    saida.mkdir(parents=True, exist_ok=True)
    arquivos = [Path(x) for x in a.arquivos]
    if a.drive:
        arquivos += baixar_do_drive(entrada)
    if not arquivos:
        if not entrada.is_dir():
            sys.exit(f"Pasta de entrada não encontrada: {entrada} (use --pasta-local, --drive ou passe arquivos)")
        arquivos = sorted(p for p in entrada.iterdir()
                          if p.suffix.lower() in EXTENSOES and not p.name.startswith(("~$", ".")))
    if not arquivos:
        sys.exit("Nenhum arquivo para processar.")

    estado_path = saida / "processados.json"
    try:
        processados = json.loads(estado_path.read_text(encoding="utf-8")) if estado_path.exists() else {}
    except (OSError, ValueError):
        processados = {}

    resultados = []
    for p in arquivos:
        if not p.is_file():
            print(f"{p}: arquivo não encontrado")
            resultados.append({"arquivo": str(p), "estado": "REJEITADO", "motivos": ["arquivo não encontrado"]})
            continue
        h = sha1_arquivo(p)
        if h in processados and not a.forcar:
            print(f"{p.name}: já processado ({processados[h]['estado']}); use --forcar para reprocessar")
            continue
        log = processar_arquivo(p, saida, a, mapa_extra)
        (saida / f"{nome_seguro(p.stem)}_qualidade.json").write_text(
            json.dumps(log, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        processados[h] = {"arquivo": p.name, "estado": log["estado"], "quando": datetime.now().isoformat(timespec="seconds")}
        resultados.append(log)
        if log["estado"].startswith("OK"):
            print(f"{p.name}: {log['estado']} | {log['tickets_emitidos']} tickets | "
                  f"{log['clientes_distintos']} clientes | descartes: {log.get('descartes', {}) or 'nenhum'}")
            for av in log["avisos"]:
                print(f"   AVISO: {av}")
        else:
            print(f"{p.name}: REJEITADO")
            for mo in log["motivos"]:
                print(f"   - {mo}")
    estado_path.write_text(json.dumps(processados, ensure_ascii=False, indent=1), encoding="utf-8")

    ok = sum(1 for r in resultados if r["estado"].startswith("OK"))
    print(f"Resumo: {ok} aceito(s), {len(resultados) - ok} rejeitado(s). Saída em {saida}")
    if resultados and ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()