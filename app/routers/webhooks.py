from fastapi import APIRouter, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from dateutil import parser as dtparser
import unicodedata
from zoneinfo import ZoneInfo

from ..database import SessionLocal
from ..config import settings
from .. import models
from ..services.notifications import send_text
from ..services.scheduling import available_slots
from ..services.nlu import analizar  # <- respeta HYBRID_MODE desde nlu.py

# ========================= Helpers & Consts =========================

MENU_TEXT = (
    "👋 ¡Hola! Soy el asistente virtual del Dr. Ontiveros, cardiólogo intervencionista.\n"
    "Estoy aquí para ayudarte de forma rápida y sencilla.\n\n"
    "¿En qué puedo apoyarte hoy?\n"
    "1) Programar una cita\n"
    "2) Confirmar\n"
    "3) Reprogramar/Cambiar\n"
    "4) Información (costos, ubicación, preparación)\n\n"
    "Escribe el número u opción. (Escribe *menu* para empezar de nuevo)"
)

def send_menu(contact: str):
    send_text(contact, MENU_TEXT)

def normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s

router = APIRouter(prefix="", tags=["webhooks"])

# Contexto simple en memoria: {contact: {"date": date, "slots": [datetime...]}}
PENDING = {}

def set_pending(contact, date_obj, slots_list):
    PENDING[contact] = {"date": date_obj, "slots": slots_list}

def get_pending(contact):
    return PENDING.get(contact) or {}

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

def get_or_create_patient(db: Session, contact: str):
    p = db.query(models.Patient).filter(models.Patient.contact == contact).first()
    if p:
        return p
    p = models.Patient(contact=contact, name=None)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p

def reserve_slot_for_contact(contact: str, match_dt):
    """Crea/actualiza una cita en estado 'reserved' para que luego pueda confirmarse."""
    for db in db_session():
        patient = get_or_create_patient(db, contact)
        appt = find_latest_reserved_for_contact(db, contact)
        if not appt or appt.status == models.AppointmentStatus.confirmed:
            appt = models.Appointment(
                patient_id=patient.id,
                start_at=match_dt,
                status=models.AppointmentStatus.reserved
            )
            db.add(appt)
        else:
            appt.start_at = match_dt
            appt.status = models.AppointmentStatus.reserved
        db.commit()

# ========================= Ruta WhatsApp =========================

@router.post("/webhooks/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(From: str = Form(None), Body: str = Form(None)):
    if not From:
        return ""

    raw_text = Body or ""
    text = normalize(raw_text)

    # Atajos globales / menú
    if text in ("hola", "buenas", "menu", "menú", "buenos dias", "buenas tardes", "buenas noches"):
        send_menu(From)
        return ""
    if text == "menu":
        send_menu(From)
        return ""

    # ===== Determinista: mapear a intención por opciones/números =====
    intent = None
    entities = {}
    reply = ""

    if text in ("1", "agendar", "programar", "reservar", "sacar cita", "cita"):
        intent = "book"
    elif text in ("2", "confirmar", "confirmo"):
        intent = "confirm"
    elif text in ("3", "reprogramar", "cambiar", "modificar", "mover", "reagendar"):
        intent = "reschedule"
    elif text in ("4", "info", "informacion", "información", "costo", "costos", "precio", "precios", "ubicacion", "ubicación", "direccion", "dirección", "preparacion", "preparación"):
        intent = "info"

    # ===== HÍBRIDO: si no hay intención determinista, llamamos al NLU (que respeta HYBRID_MODE) =====
    if not intent:
        nlu = analizar(raw_text)
        intent = nlu.get("intent") or "fallback"
        entities = nlu.get("entities") or {}
        reply = (nlu.get("reply") or "").strip()
        print(f"[NLU] from={From} intent={intent} entities={entities} text={(raw_text)[:120]}")

    nlu_date = entities.get("date")
    time_pref = entities.get("time_pref")  # "manana"/"tarde"/"noche"
    topic = entities.get("topic")

    # ===== Ramas por intención =====

    # Información
    if intent == "info":
        if topic in ("costos", "costo", "precio", "precios"):
            send_text(From, "Los costos varían según el tipo de consulta. ¿Deseas precio de *consulta inicial* o de *seguimiento*?\n(Escribe *menu* para empezar de nuevo)")
            return ""
        if topic in ("ubicacion", "ubicación", "direccion", "dirección"):
            send_text(From, "📍 Clínica ABC, Av. Ejemplo 123, León, Gto. Hay estacionamiento en sitio 🚗.\n(Escribe *menu* para empezar de nuevo)")
            return ""
        if topic in ("preparacion", "preparación"):
            send_text(From, "Llega 10 min antes, trae identificación y estudios previos si los tienes.\n(Escribe *menu* para empezar de nuevo)")
            return ""
        send_text(From, (reply or "¿Te interesa *costos*, *ubicación* o *preparación*?") + "\n(Escribe *menu* para empezar de nuevo)")
        return ""

    # Agendar / Reprogramar — prioriza turno y guarda contexto
    if intent in ("book", "reschedule"):
        if nlu_date:
            try:
                d = dtparser.parse(nlu_date).date()
                for db in db_session():
                    slots = available_slots(db, d, settings.TIMEZONE)
                    if not slots:
                        send_text(From, "No veo horarios ese día. ¿Prefieres otro día u otro turno (mañana/tarde)?\n(Escribe *menu* para empezar de nuevo)")
                        break

                    # Prioriza según time_pref
                    if time_pref == "manana":
                        filtered = [s for s in slots if 6 <= s.hour < 12]
                    elif time_pref == "tarde":
                        filtered = [s for s in slots if 12 <= s.hour < 18]
                    elif time_pref == "noche":
                        filtered = [s for s in slots if 18 <= s.hour <= 22]
                    else:
                        filtered = slots

                    show = filtered if filtered else slots

                    # Guarda contexto (para hora suelta posterior)
                    set_pending(From, d, slots)

                    sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in show[:6])
                    send_text(
                        From,
                        "Estos son algunos horarios que tengo:\n" + sample +
                        "\nResponde con la *hora exacta* que prefieras (ej. 10:30 o 4:15 pm).\n(Escribe *menu* para empezar de nuevo)"
                    )
                return ""
            except Exception:
                pass

        send_text(From, (reply or "¿Qué día te gustaría?") + "\n(Escribe *menu* para empezar de nuevo)")
        return ""

    # Confirmar (solo si hay un horario reservado pendiente)
    if intent == "confirm":
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt or appt.status != models.AppointmentStatus.reserved:
                send_text(From, "Para confirmar necesito un horario reservado. Elige una hora de la lista o escribe *3* para cambiar.\n(Escribe *menu* para empezar de nuevo)")
                break
            appt.status = models.AppointmentStatus.confirmed
            db.commit()
            send_text(From, f"✅ Tu cita quedó confirmada para {appt.start_at.strftime('%d/%m/%Y a las %H:%M')}.\n(Escribe *menu* para empezar de nuevo)")
        return ""

    # Cancelar
    if intent == "cancel":
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt:
                send_text(From, "No encontré una cita a tu nombre. ¿Quieres agendar una nueva? Responde *1*.\n(Escribe *menu* para empezar de nuevo)")
                break
            appt.status = models.AppointmentStatus.canceled
            db.commit()
            send_text(From, "He cancelado tu cita. Si quieres, puedo proponerte nuevos horarios. Responde *1* para agendar.\n(Escribe *menu* para empezar de nuevo)")
        return ""

    # Smalltalk / Greet detectado por NLU
    if intent in ("smalltalk", "greet"):
        if reply:
            send_text(From, reply + "\n(Escribe *menu* para empezar de nuevo)")
            return ""
        send_menu(From)
        return ""

    # ===== Parser de fecha/hora libre =====
    try:
        tz = ZoneInfo(settings.TIMEZONE)
        pending = get_pending(From)
        pending_date = pending.get("date")
        pending_slots = pending.get("slots")

        dt = dtparser.parse(text, dayfirst=False, fuzzy=True)

        lowered = raw_text.lower()
        has_explicit_date = any(k in lowered for k in ["202", "20/", "/", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"])
        only_time = (":" in lowered or " am" in lowered or " pm" in lowered) and not has_explicit_date

        if only_time and pending_date:
            target_h, target_m = dt.hour, dt.minute

            if pending_slots:
                match = None
                for s in pending_slots:
                    if s.hour == target_h and s.minute == target_m:
                        match = s
                        break
                if match:
                    reserve_slot_for_contact(From, match)
                    send_text(
                        From,
                        f"📌 Aparté {match.strftime('%d/%m/%Y %H:%M')} para ti.\n"
                        "Escribe *2* para confirmar o *3* para ver otras opciones.\n(Escribe *menu* para empezar de nuevo)"
                    )
                    return ""
                else:
                    sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in pending_slots[:6])
                    send_text(
                        From,
                        "No tengo exactamente esa hora, pero cuento con:\n" + sample +
                        "\nResponde con otra *hora exacta* (ej. 11:00), o escribe *3* para más opciones.\n(Escribe *menu* para empezar de nuevo)"
                    )
                    return ""
            else:
                # Recalcular slots del día en contexto si no estaban en memoria
                for db in db_session():
                    slots = available_slots(db, pending_date, settings.TIMEZONE)
                    if not slots:
                        send_text(From, "No veo horarios ese día. ¿Intentamos otra fecha o turno (mañana/tarde)?\n(Escribe *menu* para empezar de nuevo)")
                        break
                    match = None
                    for s in slots:
                        if s.hour == target_h and s.minute == target_m:
                            match = s
                            break
                    if match:
                        reserve_slot_for_contact(From, match)
                        send_text(
                            From,
                            f"📌 Aparté {match.strftime('%d/%m/%Y %H:%M')} para ti.\n"
                            "Escribe *2* para confirmar o *3* para ver otras opciones.\n(Escribe *menu* para empezar de nuevo)"
                        )
                        return ""
                    sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in slots[:6])
                    send_text(
                        From,
                        "No tengo exactamente esa hora, pero cuento con:\n" + sample +
                        "\nResponde con otra *hora exacta* (ej. 11:00), o escribe *3* para más opciones.\n(Escribe *menu* para empezar de nuevo)"
                    )
                return ""

        # Si trajo fecha completa (o no hay contexto)
        d = dt.date()
        for db in db_session():
            slots = available_slots(db, d, settings.TIMEZONE)
            if not slots:
                send_text(From, "No veo horarios ese día. ¿Quieres que busque en otra fecha o turno (mañana/tarde)?\n(Escribe *menu* para empezar de nuevo)")
                break
            set_pending(From, d, slots)
            sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in slots[:6])
            send_text(
                From,
                "Estos son algunos horarios que tengo:\n" + sample +
                "\nResponde con la *hora exacta* que prefieras (ej. 10:30), o escribe *3* para más opciones.\n(Escribe *menu* para empezar de nuevo)"
            )
        return ""
    except Exception:
        pass

    # ===== Fallback =====
    if reply:
        send_text(From, reply + "\n(Escribe *menu* para empezar de nuevo)")
    else:
        send_menu(From)
    return ""