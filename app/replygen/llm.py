# app/replygen/llm.py
import os
from typing import Optional
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

_OPENAI_KEY = os.getenv("OPENAI_API_KEY")
_CLIENT = OpenAI(api_key=_OPENAI_KEY) if (OpenAI and _OPENAI_KEY) else None

HOUSE_STYLE = (
    "Actúa como asistente del Dr. Ontiveros (cardiología intervencionista) en México. "
    "Reescribe el mensaje para que suene 100% humano: cálido, claro, profesional y cercano, sin emojis. "
    "Usa expresiones naturales en México (p. ej., '¿qué fecha te acomoda?', '¿te queda?', '¿te late?'). "
    "Sé breve y directo; evita muletillas y tono robótico. Mantén el mismo contenido factual."
)

def rewrite_like_human(text: str) -> str:
    if not _CLIENT:
        return text
    try:
        resp = _CLIENT.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.5,
            messages=[
                {"role": "system", "content": HOUSE_STYLE},
                {"role": "user", "content": f"Pulir este mensaje sin cambiar el sentido:\n{text}"}
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        return out if out else text
    except Exception:
        return text