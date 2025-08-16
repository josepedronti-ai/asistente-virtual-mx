# app/replygen/llm.py
import os

_USE_LLM = bool(os.getenv("OPENAI_API_KEY")) and os.getenv("REPLYGEN_POLISH", "1") not in ("0","false","False")
_client = None

if _USE_LLM:
    try:
        from openai import OpenAI
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        _USE_LLM = False

_STYLE = (
    "Eres un asistente médico del Dr. Ontiveros (cardiología intervencionista). "
    "Escribe como humano: cálido, claro, conciso y natural. Sin robotismos. "
    "Variación sutil entre respuestas, máximo 1 emoji si realmente aporta. "
    "Mantén la intención original del texto y NO inventes datos. "
    "Respeta la regla: confirma primero fecha y luego hora si faltan."
)

def polish(text: str) -> str:
    if not text or not _USE_LLM or _client is None:
        return text
    try:
        resp = _client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.6,
            messages=[
                {"role": "system", "content": _STYLE},
                {"role": "user", "content": f"Pulir para sonar humano sin cambiar el sentido:\n{text}"}
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        return out if out else text
    except Exception:
        return text