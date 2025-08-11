from fastapi import APIRouter, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from datetime import timedelta
from dateutil import parser as dtparser

from ..database import SessionLocal
from ..config import settings
from .. import models
from ..services.notifications import send_text
from ..services.scheduling import available_slots
from ..services.nlu import analizar_mensaje

router = APIRouter(prefix="", tags=["webhooks"])

def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def find_latest_reserved_for_contact(db: Session, contact: str):
    return (
        db.query(models.Appointment)
        .join(models.Patient)
        .filter(models.Patient.contact == contact)
        .filter(models.Appointment.status != models.AppointmentStatus.canceled)
        .order_by(models.Appointment.start_at.desc())
        .first()
    )

@router.post("/webhooks/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(From: str = Form(None), Body: str = Form(None)):
    if not From:
        return ""

    text = (Body or "").strip().lower()

    if text in ("hola", "buenas", "menu", "menú", "buenos días", "buenas tardes", "buenas noches"):
        send_text(
            From,
            "👋 ¡Hola! Soy el asistente virtual del Dr. Ontiveros, cardiólogo intervencionista.\n"
            "Estoy aquí para ayudarte de forma rápida y sencilla.\n\n"
            "¿En qué puedo apoyarte hoy?\n"
            "• Programar una cita\n"
            "• Confirmar o reprogramar\n"
            "• Información sobre costos, ubicación o preparación\n\n"
            "Escríbeme lo que necesitas y me encargaré de ayudarte de inmediato."
        )
        return ""

    if text in ("si", "sí"):
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt:
                send_text(From, "Por ahora no veo una cita pendiente a tu nombre. ¿Quieres que agendemos una?")
                break
            appt.status = models.AppointmentStatus.confirmed
            db.commit()
            send_text(From, f"✅ Listo, confirmé tu cita para {appt.start_at.isoformat()}. ¿Necesitas algo más?")
        return ""

    if text == "no":
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt:
                send_text(From, "Sin problema. ¿Qué día te gustaría agendar? Puedes decirme la fecha con tus palabras.")
                break
            d = appt.start_at.date()
            slots = available_slots(db, d, settings.TIMEZONE) or available_slots(db, d + timedelta(days=1), settings.TIMEZONE)
            options = slots[:3]
            if not options:
                send_text(From, "No tengo opciones cercanas. ¿Me das otra fecha? (por ejemplo: 15 de agosto por la tarde)")
                break
            lines = [f"{i+1}) {s.isoformat()}" for i, s in enumerate(options)]
            send_text(From, "Estas son mis mejores opciones:\n" + "\n".join(lines) + "\nResponde con 1, 2 o 3 para elegir.")
        return ""

    if text in ("1", "2", "3"):
        idx = int(text) - 1
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt:
                send_text(From, "No encuentro una cita pendiente. Si quieres, dime la fecha para proponerte horarios.")
                break
            d = appt.start_at.date()
            slots = available_slots(db, d, settings.TIMEZONE) or available_slots(db, d + timedelta(days=1), settings.TIMEZONE)
            options = slots[:3]
            if idx >= len(options):
                send_text(From, "Esa opción ya no está disponible. Intenta con 1, 2 o 3.")
                break
            new_start = options[idx]
            appt.start_at = new_start
            appt.status = models.AppointmentStatus.reserved
            db.commit()
            send_text(From, f"🔁 Listo, cambié tu cita a {new_start.isoformat()}. ¿La confirmamos con *Sí*?")
        return ""

    try:
        # Permite textos tipo “15 de agosto 5pm”
        d = dtparser.parse(text, dayfirst=False, fuzzy=True).date()
        for db in db_session():
            slots = available_slots(db, d, settings.TIMEZONE)
            if not slots:
                send_text(From, "No veo horarios ese día. ¿Quieres intentar con otro día u otro turno (mañana/tarde)?")
                break
            sample = "\n".join(s.isoformat() for s in slots[:6])
            send_text(From, "Estos son algunos horarios que tengo:\n" + sample + "\nSi quieres, dime *No* y te propongo 3 top opciones.")
        return ""
    except Exception:
        pass

    if any(p in text for p in ("costo", "costos", "precio", "precios", "ubicación", "direccion", "dirección", "cómo llegar", "preparación")):
        send_text(
            From,
            "Con gusto 😊\n• *Ubicación*: Clínica ABC, Av. Ejemplo 123, León, Gto. (estacionamiento en sitio).\n"
            "• *Costos*: varían según el tipo de consulta; si me dices cuál buscas te doy el monto y el tiempo estimado.\n"
            "¿Sobre qué te gustaría saber exactamente?"
        )
        return ""

    # Fallback con OpenAI (respuesta natural)
    respuesta = analizar_mensaje(Body or "")
    send_text(From, respuesta)
    return ""