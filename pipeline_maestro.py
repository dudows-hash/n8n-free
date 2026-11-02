#!/usr/bin/env python3
"""
ARCHOrchestrator® - pipeline_maestro.py (Final)
Orquestra a esteira de 5 agentes, atualiza o Supabase e chama o n8n de volta.
"""

import argparse
import json
import os
import sys
import logging
import re
from datetime import datetime

import requests
from supabase import create_client, Client

import agents

# Configuração do Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
SUPABASE_CLIENT: Client = create_client(SUPABASE_URL, SUPABASE_KEY) if SUPABASE_URL and SUPABASE_KEY else None

# Configuração do n8n (Callback)
N8N_WEBHOOK_URL = os.environ.get("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/commander-executar")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("archtech.pipeline_maestro")


def atualizar_status_card(card_id, status, artefato=None):
    """Atualiza o status e os artefatos no Supabase."""
    if SUPABASE_CLIENT is None:
        log.warning("Supabase não configurado. Status não será atualizado.")
        return
    try:
        dados = {"status": status}
        if artefato:
            # Faz merge com os artefatos existentes
            resposta = SUPABASE_CLIENT.table("cards").select("artefatos").eq("id", card_id).execute()
            artefatos_atuais = resposta.data[0].get("artefatos", {}) if resposta.data else {}
            artefatos_atuais.update(artefato)
            dados["artefatos"] = artefatos_atuais
        SUPABASE_CLIENT.table("cards").update(dados).eq("id", card_id).execute()
        log.info(f"[DB] Card {card_id} -> {status}")
    except Exception as e:
        log.error(f"Erro ao atualizar Supabase: {e}")


def chamar_n8n(card_id, proximo_agente):
    """Chama o n8n de volta para executar o próximo agente (ou finalizar)."""
    try:
        resposta = requests.post(N8N_WEBHOOK_URL, json={"card_id": card_id, "agente": proximo_agente})
        log.info(f"[n8n] Callback enviado para {proximo_agente} (HTTP {resposta.status_code})")
    except Exception as e:
        log.error(f"Falha ao chamar o n8n: {e}")


def extrair_json(texto):
    """Extrai o JSON de uma resposta do LLM (blindado contra markdown)."""
    try:
        match = re.search(r'\{.*\}', texto, re.DOTALL)
        if match:
            return json.loads(match.group(0))
    except json.JSONDecodeError:
        pass
    return {}


def processar_pipeline(card, agente_atual):
    """Executa apenas o agente solicitado (State Machine)."""
    card_id = card.get('id')
    artefatos = card.get("artefatos", {})
    log.info(f"[*] Executando {agente_atual} para o Card: {card_id}")

    try:
        # 1. VISION
        if agente_atual == "vision":
            atualizar_status_card(card_id, "PROCESSANDO_VISION")
            prd = agents.product_architect(card)
            atualizar_status_card(card_id, "PRD_CONCLUIDO", {"prd": prd})
            chamar_n8n(card_id, "sentinel")
            return {"status": "sucesso", "agente": "vision", "proximo": "sentinel"}

        # 2. SENTINEL
        elif agente_atual == "sentinel":
            atualizar_status_card(card_id, "PROCESSANDO_SENTINEL")
            adr = agents.sai(card, artefatos.get("prd", ""))
            atualizar_status_card(card_id, "ADR_CONCLUIDO", {"adr": adr})
            chamar_n8n(card_id, "maker")
            return {"status": "sucesso", "agente": "sentinel", "proximo": "maker"}

        # 3. MAKER
        elif agente_atual == "maker":
            atualizar_status_card(card_id, "PROCESSANDO_MAKER")
            codigo = agents.arch_copilot(card, artefatos.get("adr", ""))
            atualizar_status_card(card_id, "CODIGO_CONCLUIDO", {"codigo": codigo})
            chamar_n8n(card_id, "oracle")
            return {"status": "sucesso", "agente": "maker", "proximo": "oracle"}

        # 4. ORACLE
        elif agente_atual == "oracle":
            atualizar_status_card(card_id, "PROCESSANDO_ORACLE")
            review = agents.data_review(card, artefatos.get("codigo", ""))
            atualizar_status_card(card_id, "REVIEW_CONCLUIDO", {"review": review})
            chamar_n8n(card_id, "prime")
            return {"status": "sucesso", "agente": "oracle", "proximo": "prime"}

        # 5. PRIME (Supervisor / Veredito)
        elif agente_atual == "prime":
            atualizar_status_card(card_id, "PROCESSANDO_PRIME")
            veredito_texto = agents.supervisor(card, artefatos.get("review", ""))
            veredito = extrair_json(veredito_texto)

            if veredito.get("status", "").upper() == "APROVADO":
                atualizar_status_card(card_id, "concluido", {"veredito": veredito})
                chamar_n8n(card_id, "finalizar")
                return {"status": "sucesso", "agente": "prime", "acao": "finalizar"}

            elif veredito.get("status", "").upper() == "DEVOLVER":
                coluna_destino = (veredito.get("agente") or veredito.get("coluna") or "maker").lower()
                motivo = veredito.get("motivo", "Sem motivo")
                log.warning(f"[Prime] Devolvendo para {coluna_destino}. Motivo: {motivo}")

                # Atualiza o card com o feedback e o status de devolução
                atualizar_status_card(card_id, f"DEVOLVIDO_{coluna_destino.upper()}", {"motivo": motivo})

                # Chama o n8n para reiniciar APENAS o agente culpado
                chamar_n8n(card_id, coluna_destino)
                return {"status": "rejeitado", "agente": "prime", "devolver_para": coluna_destino, "motivo": motivo}

            else:
                atualizar_status_card(card_id, "ERRO_PRIME", {"veredito_bruto": veredito_texto})
                return {"status": "erro", "mensagem": "Prime retornou inválido"}

        else:
            return {"status": "erro", "mensagem": f"Agente desconhecido: {agente_atual}"}

    except agents.AgentError as e:
        log.error(f"Erro no agente {agente_atual}: {e}")
        atualizar_status_card(card_id, f"ERRO_{agente_atual.upper()}")
        return {"status": "erro", "agente": agente_atual, "mensagem": str(e)}
    except Exception as e:
        log.error(f"Erro inesperado no agente {agente_atual}: {e}")
        atualizar_status_card(card_id, f"ERRO_{agente_atual.upper()}")
        return {"status": "erro", "agente": agente_atual, "mensagem": str(e)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--card-id", required=True)
    ap.add_argument("--agente", required=True)
    args = ap.parse_args()

    if SUPABASE_CLIENT is None:
        print(json.dumps({"status": "erro", "mensagem": "Supabase não configurado"}))
        sys.exit(1)

    resposta = SUPABASE_CLIENT.table("cards").select("*").eq("id", args.card_id).execute()
    if not resposta.data:
        print(json.dumps({"status": "erro", "mensagem": "Card não encontrado"}))
        sys.exit(1)

    card = resposta.data[0]
    resultado = processar_pipeline(card, args.agente)

    # Contrato de Saída: JSON de uma linha só no final
    print(json.dumps(resultado, ensure_ascii=False))