from fastapi import APIRouter, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
from dateutil import parser as dtparser
import unicodedata, re
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
# Contexto simple en memoria
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

def ensure_slots_for_date(db: Session, contact: str, d):
    """Carga y guarda en CONTEXT los slots de la fecha d."""
    slots = available_slots(db, d, settings.TIMEZONE)
    if slots:
        CONTEXT[contact]["pending_date"] = d
        CONTEXT[contact]["last_slots"] = slots
    return slots

def filter_by_time_pref(slots, pref: str):
    if not slots or not pref:
        return slots
    p = pref.strip().lower()
    if p in ("manana", "mañana"):
        return [s for s in slots if s.hour < 12]
    if p == "tarde":
        return [s for s in slots if 12 <= s.hour < 18]
    if p == "noche":
        return [s for s in slots if s.hour >= 18]
    # si vino algo tipo "16:00" como time_pref, lo dejamos sin filtrar aquí
    return slots

TIME_TOKEN = re.compile(r"\b([01]?\d|2[0-3])(:|\.)[0-5]\d\b|(\b\d{1,2}\s*(am|pm)\b)", re.IGNORECASE)


# ----------------------------
# Webhook WhatsApp
# ----------------------------
@router.post("/webhooks/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(From: str = Form(None), Body: str = Form(None)):
    if not From:
        return ""

    raw_text = Body or ""
    text = normalize(raw_text)

    # Saludo profesional
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

    # 🧠 NLU
    nlu = analizar(raw_text)
    intent = nlu.get("intent", "fallback")
    entities = nlu.get("entities", {}) or {}
    reply = nlu.get("reply", "")

    print(f"[NLU] from={From} intent={intent} entities={entities} text={(raw_text)[:120]}")

    nlu_date = entities.get("date") or ""
    time_pref = entities.get("time_pref") or ""  # "manana"/"tarde"/"noche" o a veces "16:00"
    topic = entities.get("topic")

    # Información
    if intent == "info":
        # Si el mensaje era del estilo “¿tienes a las 16:00?” capturamos la hora
        if TIME_TOKEN.search(raw_text):
            # Si ya hay fecha pendiente, intentamos con ella; si no, pedimos fecha.
            for db in db_session():
                pending = CONTEXT[From].get("pending_date")
                if not pending:
                    send_text(From, "¿Podrías indicarme la *fecha* para verificar disponibilidad a esa hora?")
                    return ""
                slots = ensure_slots_for_date(db, From, pending) or []
                # Intentamos casar la hora pedida con los slots del día pendiente
                try:
                    dt_tmp = dtparser.parse(raw_text, fuzzy=True)
                    h, m = dt_tmp.hour, dt_tmp.minute
                    match = next((s for s in slots if s.hour == h and s.minute == m), None)
                    if match:
                        reserve_appointment(db, From, match)
                        send_text(
                            From,
                            f"📌 Excelente, tengo {match.strftime('%d/%m/%Y %H:%M')} reservado para ti.\n"
                            "Escribe *confirmar* para confirmar o *cambiar* si necesitas otra opción."
                        )
                        return ""
                    else:
                        sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in slots[:6])
                        send_text(
                            From,
                            "No tengo exactamente esa hora, pero cuento con:\n" + sample +
                            "\nResponde con la *hora exacta* (ej. 10:30) o escribe *cambiar* para más opciones."
                        )
                        return ""
                except Exception:
                    send_text(From, "¿Podrías repetir la hora? (ej. 10:30 o 4 pm)")
                    return ""

        # Info “normal”
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

    # Agendar / Reprogramar
    if intent in ("book", "reschedule"):
        # Si el modelo detectó una fecha (“hoy”, “mañana”, “15 de agosto”, etc.)
        if nlu_date.strip():
            try:
                d = dtparser.parse(nlu_date).date()
                for db in db_session():
                    # Siempre guardamos la fecha pendiente (aunque pidamos hora)
                    slots = ensure_slots_for_date(db, From, d) or []
                    if not slots:
                        send_text(From, "No veo horarios ese día. ¿Otro día u otro turno (mañana/tarde)?")
                        break

                    # Si “time_pref” es una franja (mañana/tarde/noche), filtramos
                    filtered = filter_by_time_pref(slots, time_pref) or slots

                    # Si “time_pref” parece una HORA (e.g. “16:00”), intentamos reservar directo
                    if TIME_TOKEN.search(time_pref or ""):
                        try:
                            tdt = dtparser.parse(time_pref, fuzzy=True)
                            h, m = tdt.hour, tdt.minute
                            match = next((s for s in filtered if s.hour == h and s.minute == m), None)
                            if match:
                                reserve_appointment(db, From, match)
                                send_text(
                                    From,
                                    f"📌 Excelente, tengo {match.strftime('%d/%m/%Y %H:%M')} reservado para ti.\n"
                                    "Escribe *confirmar* para confirmar o *cambiar* si necesitas otra opción."
                                )
                                return ""
                        except Exception:
                            pass  # si no parsea, caemos a listado

                    # Listado de opciones si no se pudo reservar directo
                    CONTEXT[From]["last_slots"] = filtered
                    sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in filtered[:6])
                    send_text(
                        From,
                        "Estos son algunos horarios que tengo:\n" + sample +
                        "\nResponde con la *hora exacta* que prefieras (por ejemplo: 10:30 o 4:15 pm). "
                        "Si quieres más opciones, escribe *cambiar*."
                    )
                return ""
            except Exception:
                pass

        # Si no vino fecha, pedimos fecha sin frases largas.
        send_text(From, "¿Qué día te gustaría?")
        return ""

    # Confirmar (solo si hay reservado)
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

    # Cancelar
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

    # Smalltalk / greet por NLU
    if intent in ("smalltalk", "greet"):
        if reply:
            send_text(From, reply)
            return ""

    # Parser natural de fecha/hora (respuesta libre)
    try:
        dt = dtparser.parse(text, dayfirst=False, fuzzy=True)
        # Si el usuario manda “16:00” y tenemos pending_date, usa esa fecha
        base_date = CONTEXT[From].get("pending_date", dt.date())
        for db in db_session():
            slots = ensure_slots_for_date(db, From, base_date) or []
            if not slots:
                send_text(From, "No veo horarios ese día. ¿Quieres intentar con otro día u otro turno (mañana/tarde)?")
                break

            lowered = raw_text.lower()
            has_time_hint = (":" in lowered) or (" am" in lowered) or (" pm" in lowered)
            if has_time_hint:
                h, m = dt.hour, dt.minute
                match = next((s for s in slots if s.hour == h and s.minute == m and s.date() == base_date), None)
                if match:
                    reserve_appointment(db, From, match)
                    send_text(
                        From,
                        f"📌 Excelente, tengo {match.strftime('%d/%m/%Y %H:%M')} reservado para ti.\n"
                        "Escribe *confirmar* para confirmar o *cambiar* si necesitas otra opción."
                    )
                    return ""
                else:
                    sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in slots[:6])
                    send_text(
                        From,
                        "No tengo exactamente esa hora, pero cuento con:\n" + sample +
                        "\nResponde con la *hora exacta* (ej. 10:30) o escribe *cambiar* para más opciones."
                    )
                    return ""
            else:
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

    # Fallback final
    final = analizar(raw_text)
    send_text(From, final.get("reply", "¿Buscas agendar, confirmar/reprogramar o información (costos, ubicación, preparación)?"))
    return ""