from fastapi import APIRouter, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from dateutil import parser as dtparser
import unicodedata
from collections import defaultdict

from ..database import SessionLocal
from ..config import settings
from .. import models
from ..services.notifications import send_text
from ..services.scheduling import available_slots
from ..services.nlu import analizar


# ----------------------------
# Utilidades
# ----------------------------
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

# ----------------------------
# Contexto en memoria (simple para piloto)
# Guarda fecha ofrecida y últimos slots listados por contacto
# ----------------------------
CONTEXT = defaultdict(dict)

def get_or_create_patient(db: Session, contact: str) -> models.Patient:
    p = db.query(models.Patient).filter(models.Patient.contact == contact).first()
    if p:
        return p
    p = models.Patient(name=None, contact=contact)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p

def reserve_appointment(db: Session, contact: str, start_dt: datetime) -> models.Appointment:
    """Crea o actualiza la cita más reciente de este contacto como 'reserved' con start_dt."""
    patient = get_or_create_patient(db, contact)
    appt = (
        db.query(models.Appointment)
        .join(models.Patient)
        .filter(models.Patient.id == patient.id)
        .filter(models.Appointment.status != models.AppointmentStatus.canceled)
        .order_by(models.Appointment.start_at.desc())
        .first()
    )
    if appt is None:
        appt = models.Appointment(
            patient_id=patient.id,
            start_at=start_dt,
            status=models.AppointmentStatus.reserved,
        )
        db.add(appt)
    else:
        appt.start_at = start_dt
        appt.status = models.AppointmentStatus.reserved
    db.commit()
    db.refresh(appt)
    return appt


# ----------------------------
# Webhook WhatsApp
# ----------------------------
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
            send_text(From, "Con gusto. Los costos varían según el tipo de consulta. ¿Te interesa consulta inicial o de seguimiento?")
            return ""
        if topic in ("ubicacion", "direccion"):
            send_text(From, "📍 Estamos en Clínica ABC, Av. Ejemplo 123, León, Gto. Hay estacionamiento en sitio 🚗.")
            return ""
        if topic in ("preparacion",):
            send_text(From, "Recomendación general: llega 10 min antes, lleva identificación y estudios previos si los tienes.")
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

                    # Guarda contexto para entender la hora que elija el paciente
                    CONTEXT[From]["pending_date"] = d
                    CONTEXT[From]["last_slots"] = slots

                    sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in slots[:6])
                    send_text(
                        From,
                        "Estos son algunos horarios que tengo:\n" + sample +
                        "\nResponde con la *hora exacta* que prefieras (por ejemplo: 10:30 o 4:15 pm). "
                        "Si quieres más opciones, escribe *cambiar*."
                    )
                return ""
            except Exception:
                pass
        # Sin fecha clara → pídela de forma natural
        send_text(From, reply or "¿Qué día te gustaría?")
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
            send_text(From, f"✅ Tu cita quedó confirmada para {appt.start_at.strftime('%d/%m/%Y %H:%M')}. ¿Algo más en lo que te ayude?")
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
            send_text(From, "He cancelado tu cita. Si quieres, puedo proponerte nuevos horarios.")
        return ""

    # ----------------------------
    # Pequeña charla o saludo detectado por NLU
    # ----------------------------
    if intent in ("smalltalk", "greet"):
        if reply:
            send_text(From, reply)
            return ""

    # ----------------------------
    # Parser de fecha y hora naturales (ej. “15 de agosto 10:30 am”)
    # - Si el usuario responde solo con la hora, reservamos en BD
    # ----------------------------
    try:
        # Detecta fecha y (si viene) hora
        dt = dtparser.parse(text, dayfirst=False, fuzzy=True)
        d = dt.date()

        for db in db_session():
            slots = available_slots(db, d, settings.TIMEZONE)
            if not slots:
                send_text(From, "No veo horarios ese día. ¿Quieres intentar con otro día u otro turno (mañana/tarde)?")
                break

            lowered = raw_text.lower()
            has_time_hint = (":" in lowered) or (" am" in lowered) or (" pm" in lowered)

            if has_time_hint:
                # 1) Fecha base (de contexto si existe)
                base_date = CONTEXT[From].get("pending_date", d)

                # 2) Hora elegida
                target_h = dt.hour
                target_m = dt.minute

                # 3) Buscar match exacto en los últimos slots ofrecidos (o en los actuales)
                last_slots = CONTEXT[From].get("last_slots") or slots
                match = None
                for s in last_slots:
                    if s.date() == base_date and s.hour == target_h and s.minute == target_m:
                        match = s
                        break

                if match:
                    # ⚠️ Reservar en BD para que 'confirmar' funcione
                    reserve_appointment(db, From, match)

                    send_text(
                        From,
                        f"📌 Excelente, tengo {match.strftime('%d/%m/%Y %H:%M')} reservado para ti.\n"
                        "Escribe *confirmar* para confirmar o *cambiar* si necesitas otra opción."
                    )
                    return ""
                else:
                    sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in last_slots[:6])
                    send_text(
                        From,
                        "No tengo exactamente esa hora, pero cuento con:\n" + sample +
                        "\nResponde con la *hora exacta* (ej. 10:30) o escribe *cambiar* para más opciones."
                    )
                    return ""
            else:
                # Solo fecha → listamos opciones y guardamos contexto
                CONTEXT[From]["pending_date"] = d
                CONTEXT[From]["last_slots"] = slots

                sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in slots[:6])
                send_text(
                    From,
                    "Estos son algunos horarios que tengo:\n" + sample +
                    "\nResponde con la *hora exacta* que prefieras (ej. 10:30), o escribe *cambiar* para más opciones."
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