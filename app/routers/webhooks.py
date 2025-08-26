# app/routers/webhooks.py
from __future__ import annotations
from fastapi import APIRouter, Form
from fastapi.responses import PlainTextResponse

from ..services.notifications import send_text
from ..agent.agent_controller import run_agent

router = APIRouter(prefix="", tags=["webhooks"])

@router.post("/webhooks/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(From: str = Form(None), Body: str = Form(None)) -> str:
    if not From:
        return ""
    raw_text = Body or ""
    print(f"[WHATSAPP IN] from={From} body={raw_text}")

    # Delegar al Agente (con fallback seguro)
    try:
        reply = run_agent(From, raw_text)
    except Exception as e:
        print(f"[AGENT ERROR] {e}")
        reply = "Tuve un problema para procesar su solicitud. ¿Desea que lo intente de nuevo o prefiere hablar con recepción?"

    send_text(From, reply)
    return ""