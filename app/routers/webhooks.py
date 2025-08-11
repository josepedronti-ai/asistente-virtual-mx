from fastapi import APIRouter, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from datetime import timedelta
from dateutil import parser as dtparser
import unicodedata

from ..database import SessionLocal
from ..config import settings
from .. import models
from ..services.notifications import send_text
from ..services.scheduling import available_slots
from ..services.nlu import analizar

def normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s

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

    raw_text = Body or ""
    text = normalize(raw_text)

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

    # ===== NLU (OpenAI) — interpreta intención y entidades =====
    nlu = analizar(Body or "")
    intent = nlu.get("intent", "fallback")
    entities = nlu.get("entities", {}) or {}
    reply = nlu.get("reply", "")

    # Log útil para ver qué detecta el modelo (Render Logs)
    print(f"[NLU] from={From} intent={intent} entities={entities} text={(Body or '')[:120]}")

    nlu_date = entities.get("date")
    time_pref = entities.get("time_pref")  # "manana"/"tarde"/"noche"
    topic = entities.get("topic")

    if intent == "greet":
        send_text(From, reply or "Hola 👋 ¿En qué te apoyo?")
        return ""

    if intent == "info":
        if topic in ("costos","costo","precio","precios"):
            send_text(From, "Con gusto. Los costos varían según el tipo de consulta. ¿Qué consulta te interesa?")
            return ""
        if topic in ("ubicacion","ubicación","direccion","dirección"):
            send_text(From, "Estamos en Clínica ABC, Av. Ejemplo 123, León, Gto. Hay estacionamiento en sitio 🚗.")
            return ""
        if topic in ("preparacion","preparación"):
            send_text(From, "Preparación general: llega 10 min antes, trae identificación y estudios previos si los tienes.")
            return ""
        send_text(From, reply or "¿Te interesa costos, ubicación o preparación?")
        return ""

    if intent in ("book","reschedule"):
        if nlu_date:
            try:
                d = dtparser.parse(nlu_date).date()
                for db in db_session():
                    slots = available_slots(db, d, settings.TIMEZONE)
                    if not slots:
                        send_text(From, "No veo horarios ese día. ¿Otro día u otro turno (mañana/tarde)?")
                        break
                    sample = "\n".join(s.isoformat() for s in slots[:6])
                    send_text(From, "Estos son algunos horarios que tengo:\n" + sample + "\nSi quieres, dime *No* y te propongo 3 top opciones.")
                return ""
            except Exception:
                pass
        send_text(From, reply or "Perfecto. ¿Qué día te gustaría? Puedes decirlo con tus palabras.")
        return ""

    if intent == "confirm":
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt or appt.status != models.AppointmentStatus.reserved:
                send_text(From, "Para confirmar necesito un horario reservado. Si quieres, escribe *agendar* o *cambiar*.")
                break
            appt.status = models.AppointmentStatus.confirmed
            db.commit()
            send_text(From, f"✅ Confirmé tu cita para {appt.start_at.isoformat()}. ¿Algo más en lo que te ayude?")
        return ""

    if intent == "cancel":
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt:
                send_text(From, "No encontré una cita a tu nombre. ¿Quieres agendar una nueva?")
                break
            appt.status = models.AppointmentStatus.canceled
            db.commit()
            send_text(From, "🗑️ He cancelado tu cita. Si quieres, puedo proponerte nuevos horarios.")
        return ""

    if intent in ("smalltalk","fallback"):
        if reply:
            send_text(From, reply)
            return ""
        # si no trae reply, dejamos que siga al try: de fechas de abajo
        
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
    respuesta = analizar(Body or "")
    send_text(From, respuesta)
    return ""