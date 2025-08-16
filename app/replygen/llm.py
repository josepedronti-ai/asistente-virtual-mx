# app/replygen/llm.py
from __future__ import annotations
import os, re
from typing import Optional
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# Usa LLM solo si hay API key
_API_KEY = os.getenv("OPENAI_API_KEY")
_client = OpenAI(api_key=_API_KEY) if (_API_KEY and OpenAI) else None

# Estilo conversacional:
# - humano, cálido, directo, sin jerga, sin plantillas rígidas
# - no uses emojis salvo que el usuario ya los emplee
# - evita repetir frases de cierre (“¿algo más?”) en todos los mensajes
# - confirma primero FECHA y después HORA si falta info (esto lo decide la lógica upstream;
#   aquí solo “pule” el texto final)
_SYSTEM = (
    "Eres un asistente médico del Dr. Ontiveros (cardiología intervencionista). "
    "Escribe como humano: cálido pero profesional, claro y conciso. "
    "Evita sonar robótico o ceremonial. Sin muletillas. "
    "No uses listas a menos que el texto original las traiga. "
    "No agregues contenido que el texto original no comunica. "
    "No uses emojis salvo que el usuario ya los haya usado en su último mensaje. "
    "Usa español neutro de México. Mantén un trato cercano y respetuoso."
)

def _user_uses_emoji(user_text: str) -> bool:
    if not user_text:
        return False
    # “Emoji-like” detección sencilla
    return bool(re.search(r"[\U0001F300-\U0001FAFF\u2600-\u26FF]", user_text))

def polish(text: str, user_text: str = "", intent: Optional[str] = None) -> str:
    """
    Pulidor de estilo. Si no hay API key, devuelve el mismo texto.
    - Evita emojis salvo que el usuario ya usó.
    - Mantiene el mensaje breve y natural.
    - No inventa datos.
    """
    if not text:
        return ""

    if not _client:
        # Sin LLM: limpieza mínima (quitar dobles espacios)
        return re.sub(r"\s{2,}", " ", text).strip()

    wants_emoji = _user_uses_emoji(user_text)

    # Instrucciones dinámicas según si el usuario usó emoji
    style_hint = "Puedes usar un emoji si te parece muy natural." if wants_emoji else "No uses emojis en esta respuesta."

    try:
        resp = _client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.5,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"{style_hint}\n"
                        "Reescribe el siguiente mensaje para que suene humano, cálido y profesional, "
                        "sin alargarlo innecesariamente y sin perder información:\n\n"
                        f"{text}"
                    ),
                },
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        # Fallback por si viniera vacío
        return out or text
    except Exception:
        return text