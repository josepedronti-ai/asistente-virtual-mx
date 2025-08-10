from .twilio_client import send_whatsapp

def send_confirmation(contact: str, slot_iso: str) -> None:
    body = ("✅ *Cita reservada*\n"
            f"Fecha y hora: {slot_iso}\n"
            "Responde *Sí* para confirmar o *No* para cambiar.")
    send_whatsapp(contact, body)

def send_reminder(contact: str, slot_iso: str, when: str = "24h") -> None:
    body = (f"⏰ Recordatorio ({when})\n"
            f"Tu cita es: {slot_iso}\n"
            "Responde *Sí* para confirmar o *No* para reagendar.")
    send_whatsapp(contact, body)

def send_text(contact: str, body: str) -> None:
    send_whatsapp(contact, body)
