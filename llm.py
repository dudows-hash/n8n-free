import os
from openai import OpenAI

_client = None

def _cliente():
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.environ["LLM_API_KEY"],
            base_url=os.environ.get("LLM_BASE_URL", "https://api.deepseek.com"),
            timeout=float(os.environ.get("LLM_TIMEOUT", "120")),
            max_retries=0,  # Desativado para não conflitar com o retry do adaptador
        )
    return _client

MODELO = os.environ.get("LLM_MODEL", "deepseek-flash")
# preços em R$ por 1M de tokens, configuráveis
PRECO_IN = float(os.environ.get("LLM_PRECO_IN", "0"))
PRECO_OUT = float(os.environ.get("LLM_PRECO_OUT", "0"))

def chamar_llm(system, user, *, json_mode=False, max_tokens=4000, temperature=0.2):
    kwargs = dict(
        model=MODELO,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    resp = _cliente().chat.completions.create(**kwargs)
    uso = resp.usage
    return {
        "texto": resp.choices[0].message.content or "",
        "modelo": MODELO,
        "tokens_in": uso.prompt_tokens,
        "tokens_out": uso.completion_tokens,
        "custo_brl": (uso.prompt_tokens * PRECO_IN + uso.completion_tokens * PRECO_OUT) / 1_000_000,
    }