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

    # ----------------------------
    # Saludo profesional
    # ----------------------------
    if text in ("hola", "buenas", "menu", "buenos dias", "buenas tardes", "buenas noches"):
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

    # ----------------------------
    # 🧠 NLU (cerebro) por intención
    # ----------------------------
    nlu = analizar(raw_text)
    intent = nlu.get("intent", "fallback")
    entities = nlu.get("entities", {}) or {}
    reply = nlu.get("reply", "")

    print(f"[NLU] from={From} intent={intent} entities={entities} text={(raw_text)[:120]}")

    nlu_date = entities.get("date")
    time_pref = entities.get("time_pref")  # "manana"/"tarde"/"noche"
    topic = entities.get("topic")

    # ----------------------------
    # Información general
    # ----------------------------
    if intent == "info":
        if topic in ("costos", "costo", "precio", "precios"):
            send_text(From, "Con gusto. Los costos varían según el tipo de consulta. ¿Qué consulta te interesa?")
            return ""
        if topic in ("ubicacion", "direccion"):
            send_text(From, "Estamos en Clínica ABC, Av. Ejemplo 123, León, Gto. Hay estacionamiento en sitio 🚗.")
            return ""
        if topic in ("preparacion",):
            send_text(From, "Preparación general: llega 10 min antes, trae identificación y estudios previos si los tienes.")
            return ""
        send_text(From, reply or "¿Te interesa costos, ubicación o preparación?")
        return ""

    # ----------------------------
    # Agendar o reprogramar
    # ----------------------------
    if intent in ("book", "reschedule"):
        if nlu_date:
            try:
                d = dtparser.parse(nlu_date).date()
                for db in db_session():
                    slots = available_slots(db, d, settings.TIMEZONE)
                    if not slots:
                        send_text(From, "No veo horarios ese día. ¿Otro día u otro turno (mañana/tarde)?")
                        break
                    sample = "\n".join(s.isoformat() for s in slots[:6])
                    send_text(
                        From,
                        "Estos son algunos horarios que tengo:\n" + sample +
                        "\nSi quieres más opciones, escribe *cambiar*."
                    )
                return ""
            except Exception:
                pass
        # Sin fecha clara → pídela de forma natural
        send_text(From, reply or "Perfecto. ¿Qué día te gustaría? Puedes decirlo con tus palabras (p. ej., '15 de agosto por la tarde').")
        return ""

    # ----------------------------
    # Confirmar cita (solo si está en reserved)
    # ----------------------------
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

    # ----------------------------
    # Cancelar cita
    # ----------------------------
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

    # ----------------------------
    # Pequeña charla o saludo detectado por NLU
    # ----------------------------
    if intent in ("smalltalk", "greet"):
        if reply:
            send_text(From, reply)
            return ""

    # ----------------------------
    # Atajo determinista: elección 1 / 2 / 3
    # ----------------------------
    if text in ("1", "2", "3"):
        idx = int(text) - 1
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt:
                send_text(From, "Por ahora no tengo opciones para elegir. Escribe *agendar* o *cambiar* para continuar.")
                break
            d = appt.start_at.date()
            slots = available_slots(db, d, settings.TIMEZONE) or available_slots(db, d + timedelta(days=1), settings.TIMEZONE)
            options = slots[:3]
            if not options or idx >= len(options):
                send_text(From, "No hay opciones disponibles para elegir ahora. Escribe *cambiar* para proponerte nuevas.")
                break
            new_start = options[idx]
            appt.start_at = new_start
            appt.status = models.AppointmentStatus.reserved
            db.commit()
            send_text(From, f"🔁 Reprogramé tu cita a {new_start.isoformat()}. Escribe *confirmar* para confirmar.")
        return ""

    # ----------------------------
    # Parser de fecha libre (ej. “15 de agosto 5pm”)
    # ----------------------------
    try:
        d = dtparser.parse(text, dayfirst=False, fuzzy=True).date()
        for db in db_session():
            slots = available_slots(db, d, settings.TIMEZONE)
            if not slots:
                send_text(From, "No veo horarios ese día. ¿Quieres intentar con otro día u otro turno (mañana/tarde)?")
                break
            sample = "\n".join(s.isoformat() for s in slots[:6])
            send_text(
                From,
                "Estos son algunos horarios que tengo:\n" + sample +
                "\nSi quieres más opciones, escribe *cambiar*."
            )
        return ""
    except Exception:
        pass

    # ----------------------------
    # Fallback final (respuesta natural del NLU)
    # ----------------------------
    final = analizar(raw_text)  # analizar devuelve dict
    send_text(From, final.get("reply", "¿Buscas agendar, confirmar/reprogramar o información (costos, ubicación, preparación)?"))
    return ""