# app/services/twilio_client.py
import os
from twilio.rest import Client
from ..config import settings

# Switch de pruebas: no enviamos a Twilio, solo logeamos
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

def _normalize_wa(number: str) -> str:
    if not number:
        return number
    number = number.strip()
    if not number.startswith("whatsapp:"):
        number = f"whatsapp:{number}"
    number = number.replace("whatsapp: ", "whatsapp:")
    prefix, rest = number.split(":", 1)
    rest = rest.strip()
    if not rest.startswith("+"):
        rest = "+" + rest.lstrip("+").replace(" ", "")
    return f"{prefix}:{rest}"

def get_twilio_client() -> Client | None:
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        return None
    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)

def send_whatsapp(to: str, body: str) -> dict:
    """
    Envía un WhatsApp usando Twilio.
    - Si DRY_RUN=true: no envía; imprime en logs y regresa {"dry_run": True, ...}
    - Si faltan credenciales: modo MOCK (no envía) y regresa {"mock": True, ...}
    - Si hay error al enviar: registra y regresa {"error": "..."}
    """
    to_norm = _normalize_wa(to)
    from_norm = _normalize_wa(settings.TWILIO_WHATSAPP_FROM or "")

    # DRY RUN: solo log, no se consume Twilio
    if DRY_RUN:
        print(f"[DRY_RUN WHATSAPP] to={to_norm} body={body.replace('\n', ' | ')}")
        return {"dry_run": True, "to": to_norm, "body": body}

    client = get_twilio_client()

    # MOCK si no hay credenciales/configuración
    if client is None or not from_norm:
        print(f"[WA MOCK] to={to_norm} body={body.replace('\n', ' | ')}")
        return {"mock": True, "to": to_norm, "body": body}

    try:
        msg = client.messages.create(from_=from_norm, to=to_norm, body=body)
        return {"sid": msg.sid, "to": to_norm}
    except Exception as e:
        print(f"[WA ERROR] to={to_norm} err={e}")
        return {"error": str(e), "to": to_norm}
