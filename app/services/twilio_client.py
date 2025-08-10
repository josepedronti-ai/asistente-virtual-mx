from twilio.rest import Client
from ..config import settings

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
    client = get_twilio_client()
    to = _normalize_wa(to)
    from_ = _normalize_wa(settings.TWILIO_WHATSAPP_FROM or "")
    if client is None or not from_:
        print(f"[WA MOCK] to={to} body={body}")
        return {"mock": True}
    msg = client.messages.create(from_=from_, to=to, body=body)
    return {"sid": msg.sid}
