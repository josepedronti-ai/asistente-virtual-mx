# app/replygen/llm.py
from __future__ import annotations
import os
from typing import Optional

try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

_API_KEY = os.getenv("OPENAI_API_KEY")
_USE_LLM = bool(_API_KEY and OpenAI)

_client: Optional["OpenAI"] = None
if _USE_LLM:
    try:
        _client = OpenAI(api_key=_API_KEY)
    except Exception:
        _client = None
        _USE_LLM = False

_SYSTEM = (
    "Eres un asistente de consultorio médico en México. Redactas como humano, cálido, claro y profesional. "
    "Trato de usted. Sin emojis. Frases naturales mexicanas. Breve, directo, amable. "
    "Evitas sonar robótico. No inventes datos de agenda: solo reescribe el texto que te doy."
)

def polish_spanish_mx(text: str) -> str:
    """
    Pulido opcional con LLM (si OPENAI_API_KEY está presente).
    Reescribe en tono humano MX (usted), sin emojis.
    """
    if not (_USE_LLM and _client and text):
        return text
    try:
        resp = _client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.4,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Reescribe de forma natural y profesional (usted, MX, sin emojis):\n{text}"}
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        return out if out else text
    except Exception:
        return text