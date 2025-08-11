# app/services/notifications.py
import os
from .twilio_client import send_whatsapp

# Activa el modo “seco” vía variable de entorno: TWILIO_DRY_RUN=true
DRY_RUN = os.getenv("TWILIO_DRY_RUN", "").lower() in ("1", "true", "yes")

def send_confirmation(contact: str, slot_iso: str) -> None:
    """Mensaje de reserva con nueva redacción (confirmar/cambiar)."""
    body = (
        "✅ *Cita reservada*\n"
        f"Fecha y hora: {slot_iso}\n"
        "Escribe *confirmar* para confirmar o *cambiar* para ver otras opciones."
    )
    _send(contact, body)

def send_reminder(contact: str, slot_iso: str, when: str = "24h") -> None:
    """Recordatorio (D-1, D-3, etc.)."""
    body = (
        f"⏰ Recordatorio ({when})\n"
        f"Tu cita es: {slot_iso}\n"
        "Si necesitas, escribe *cambiar* para reprogramar o *confirmar* para confirmar."
    )
    _send(contact, body)

def send_text(contact: str, body: str) -> None:
    """Mensaje libre usado por el webhook."""
    _send(contact, body)

# ------------------ internos ------------------

def _send(contact: str, body: str) -> None:
    """Respeta el modo seco: imprime en logs en lugar de enviar por Twilio."""
    if DRY_RUN:
        print(f"[DRY_RUN WHATSAPP] to={contact} body={body.replace(chr(10), ' | ')}")
        return
    send_whatsapp(contact, body)
