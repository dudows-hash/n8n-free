import time, random
from openai import RateLimitError, APIStatusError, APIConnectionError, APITimeoutError
from llm import chamar_llm

def chamar_com_retry(system, user, tentativas=4, **kw):
    for i in range(tentativas):
        try:
            return chamar_llm(system, user, **kw)
        except (RateLimitError, APIConnectionError, APITimeoutError):
            pass
        except APIStatusError as e:
            if e.status_code < 500:
                raise          # erro do cliente (chave, prompt): não adianta repetir
        time.sleep((2 ** i) + random.random())
    raise RuntimeError("LLM indisponível após várias tentativas")