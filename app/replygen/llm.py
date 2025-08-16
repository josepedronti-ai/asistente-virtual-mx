# app/replygen/llm.py
import os

_USE_LLM = bool(os.getenv("OPENAI_API_KEY"))
_client = None
if _USE_LLM:
    try:
        from openai import OpenAI
        _client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    except Exception:
        _USE_LLM = False

_STYLE = (
    "Eres un asistente médico del Dr. Ontiveros (cardiología intervencionista). "
    "Escribe como humano: cálido, claro, profesional y conciso. Sin emojis salvo que el usuario use alguno; "
    "en ese caso, puedes incluir uno muy ocasional. Evita plantillas repetitivas. Varía ligeramente la redacción. "
    "Nunca inventes disponibilidad; tú solo reescribes el texto que te paso."
)

def polish(message: str, user_text: str = "") -> str:
    if not _USE_LLM or not _client:
        return message
    try:
        resp = _client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.5,
            messages=[
                {"role": "system", "content": _STYLE},
                {"role": "user", "content": f"Usuario escribió: {user_text}\nPulir este mensaje para sonar humano (no cambies los datos):\n{message}"}
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        return out if out else message
    except Exception:
        return message