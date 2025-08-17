# app/replygen/llm.py
from __future__ import annotations
import os
from typing import Optional

try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

# -----------------------------
# Config
# -----------------------------
_API_KEY = os.getenv("OPENAI_API_KEY")
_MODEL = os.getenv("OPENAI_LLM_MODEL", "gpt-4o-mini")

_USE_LLM = bool(_API_KEY and OpenAI)
_client: Optional["OpenAI"] = None
if _USE_LLM:
    try:
        _client = OpenAI(api_key=_API_KEY)
    except Exception:
        _client = None
        _USE_LLM = False

# Instrucciones: reescribir sin alterar hechos (fechas/horas/números)
_SYSTEM = (
    "Eres un asistente de consultorio médico en México. "
    "Reescribes mensajes para que suenen naturales, cálidos y profesionales con trato de usted. "
    "Evita sonar robótico, no uses emojis ni signos excesivos. "
    "Usa expresiones comunes en México. Sé breve y claro.\n"
    "MUY IMPORTANTE: NO cambies el sentido ni los datos explícitos del texto original "
    "(no alteres fechas, horas, montos, nombres, direcciones). "
    "NO inventes información nueva ni añadas preguntas extra. "
    "Solo mejora la redacción del texto proporcionado."
)

def polish_spanish_mx(text: str) -> str:
    """
    Pulido opcional con LLM (si OPENAI_API_KEY está presente).
    - Tono: profesional, humano, MX, usted.
    - Sin emojis.
    - No altera datos (fechas/horas/precios/nombres).
    Si no hay API o hay error, devuelve el texto tal cual.
    """
    if not text:
        return text
    if not (_USE_LLM and _client):
        return text
    try:
        resp = _client.chat.completions.create(
            model=_MODEL,
            temperature=0.3,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Reescribe en español de México (usted, sin emojis), sin cambiar datos:\n\n{text}"},
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        return out if out else text
    except Exception:
        return text

# Alias conveniente si prefieres este nombre en el resto del código
def polish_if_enabled(text: str) -> str:
    return polish_spanish_mx(text)