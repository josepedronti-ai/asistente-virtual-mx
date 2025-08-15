from fastapi import APIRouter, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from dateutil import parser as dtparser
import unicodedata, re
from datetime import datetime

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

def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_patient_by_contact(db: Session, contact: str) -> models.Patient | None:
    return db.query(models.Patient).filter(models.Patient.contact == contact).first()

def find_latest_reserved_for_contact(db: Session, contact: str):
    return (
        db.query(models.Appointment)
        .join(models.Patient)
        .filter(models.Patient.contact == contact)
        .filter(models.Appointment.status == models.AppointmentStatus.reserved)
        .order_by(models.Appointment.start_at.desc())
        .first()
    )

def get_or_create_patient(db: Session, contact: str) -> models.Patient:
    p = get_patient_by_contact(db, contact)
    if p:
        return p
    p = models.Patient(contact=contact)  # name puede ir después
    db.add(p)
    db.commit()
    db.refresh(p)
    return p

def reserve_or_update(db: Session, patient: models.Patient, start_dt: datetime) -> models.Appointment:
    appt = (
        db.query(models.Appointment)
        .filter(models.Appointment.patient_id == patient.id)
        .filter(models.Appointment.status == models.AppointmentStatus.reserved)
        .order_by(models.Appointment.start_at.desc())
        .first()
    )
    if appt:
        appt.start_at = start_dt
    else:
        appt = models.Appointment(
            patient_id=patient.id,
            start_at=start_dt,
            status=models.AppointmentStatus.reserved
        )
        db.add(appt)
    db.commit()
    db.refresh(appt)
    return appt

def parse_time_hint(text: str):
    """
    Extrae hora explícita (10:30, 4 pm, 16:00). Devuelve (hour, minute) o None.
    """
    t = (text or "").lower().strip()
    # hh:mm
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    # h am/pm
    m = re.search(r"\b([1-9]|1[0-2])\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1))
        ampm = m.group(2)
        if ampm == "pm" and h != 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        return h, 0
    # hh (24h) conservador
    m = re.search(r"\b(0?\d|1\d|2[0-3])\s*h?\b", t)
    if m:
        return int(m.group(1)), 0
    return None

def filter_by_time_pref(slots, time_pref: str):
    if not time_pref:
        return slots
    if time_pref == "manana":
        return [s for s in slots if 6 <= s.hour < 12]
    if time_pref == "tarde":
        return [s for s in slots if 12 <= s.hour < 18]
    if time_pref == "noche":
        return [s for s in slots if 18 <= s.hour <= 22]
    return slots

def human_list(slots, limit=6):
    return "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in slots[:limit])

def looks_like_name(text: str) -> str | None:
    """
    Heurística simple: 2-5 palabras, sin dígitos, solo letras/espacios/acentos y longitud 3-60.
    Devuelve el nombre "limpio" o None.
    """
    if not text:
        return None
    t = text.strip()
    if any(ch.isdigit() for ch in t):
        return None
    # permitir letras, espacios y acentos/apóstrofos básicos
    if not re.fullmatch(r"[A-Za-zÁÉÍÓÚáéíóúÑñ'’\s]{3,60}", t):
        return None
    parts = t.split()
    if len(parts) < 2 or len(parts) > 5:
        return None
    # Capitalizar cada palabra
    clean = " ".join(p.capitalize() for p in parts)
    return clean


router = APIRouter(prefix="", tags=["webhooks"])


# ----------------------------
# Webhook principal
# ----------------------------
@router.post("/webhooks/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(From: str = Form(None), Body: str = Form(None)):
    if not From:
        return ""

    raw_text = Body or ""
    text = normalize(raw_text)

    # 0) Si hay una cita RESERVADA y falta nombre → capturar nombre o pedirlo
    for db in db_session():
        patient = get_patient_by_contact(db, From)
        pending = find_latest_reserved_for_contact(db, From) if patient else None
        if patient and pending and (patient.name is None or not patient.name.strip()):
            maybe_name = looks_like_name(raw_text)
            if maybe_name:
                patient.name = maybe_name
                db.commit()
                send_text(
                    From,
                    f"Gracias, *{patient.name}*. ¿Deseas confirmar la cita para "
                    f"{pending.start_at.strftime('%d/%m/%Y a las %H:%M')}? "
                    "Escribe *confirmar* o *cambiar*."
                )
                return ""
            else:
                send_text(From, "¿A nombre de quién agendamos la cita? (Nombre y apellido)")
                return ""

    # 1) Saludo profesional
    if text in ("hola", "buenas", "menu", "buenos dias", "buenas tardes", "buenas noches", "menú"):
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

    # 2) 🧠 NLU (intención + entidades)
    nlu = analizar(raw_text)
    intent = nlu.get("intent", "fallback")
    entities = nlu.get("entities", {}) or {}
    reply = nlu.get("reply", "")

    print(f"[NLU] from={From} intent={intent} entities={entities} text={(raw_text)[:120]}")

    nlu_date = entities.get("date") or ""
    time_pref = entities.get("time_pref") or ""   # "manana"/"tarde"/"noche"
    topic = entities.get("topic") or ""

    # 3) Info general
    if intent == "info":
        if topic in ("costos", "costo", "precio", "precios"):
            send_text(From, "Los costos varían según el tipo de consulta. ¿Deseas el precio de consulta inicial o de seguimiento?")
            return ""
        if topic in ("ubicacion", "direccion", "ubicación", "dirección"):
            send_text(From, "📍 Estamos en Clínica ABC, Av. Ejemplo 123, León, Gto. Hay estacionamiento en sitio 🚗.")
            return ""
        if topic in ("preparacion", "preparación"):
            send_text(From, "Te recomiendo llegar 10 min antes, llevar identificación y estudios previos si los tienes.")
            return ""
        send_text(From, reply or "¿Buscas costos, ubicación o preparación?")
        return ""

    # 4) Confirmar (requiere que ya exista una reservada)
    if intent == "confirm":
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt:
                send_text(From, "No encuentro una cita reservada. Si quieres, escribe *agendar* para elegir horario.")
                break
            # Si aún no hay nombre, pídeselo primero
            patient = get_patient_by_contact(db, From)
            if patient and (patient.name is None or not patient.name.strip()):
                send_text(From, "Antes de confirmar, ¿a nombre de quién la agendamos? (Nombre y apellido)")
                break
            appt.status = models.AppointmentStatus.confirmed
            db.commit()
            name_txt = f" de {patient.name}" if patient and patient.name else ""
            send_text(From, f"✅ Tu cita{name_txt} quedó confirmada para {appt.start_at.strftime('%d/%m/%Y a las %H:%M')}.\n¿Algo más en lo que te ayude?")
        return ""

    # 5) Cancelar
    if intent == "cancel":
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt:
                send_text(From, "No encontré una cita reservada. ¿Quieres agendar una nueva?")
                break
            appt.status = models.AppointmentStatus.canceled
            db.commit()
            send_text(From, "He cancelado tu cita. Si quieres, puedo proponerte nuevos horarios.")
        return ""

    # 6) Agendar / Reprogramar con lógica robusta fecha/hora
    if intent in ("book", "reschedule"):
        explicit_time = parse_time_hint(raw_text)  # (h,m) o None

        # Fecha desde NLU (p.ej. 'mañana', '2025-08-15'…)
        parsed_date = None
        if nlu_date:
            try:
                parsed_date = dtparser.parse(nlu_date, dayfirst=False, fuzzy=True).date()
            except Exception:
                parsed_date = None

        # A: fecha SÍ, hora NO -> pedir hora (y mostrar opciones del turno)
        if parsed_date and not explicit_time:
            for db in db_session():
                slots = available_slots(db, parsed_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, "No veo horarios ese día. ¿Prefieres otro día u otro turno (mañana/tarde)?")
                    break
                filt = filter_by_time_pref(slots, time_pref) or slots
                sample = human_list(filt, limit=6)
                pref_txt = " por la mañana" if time_pref == "manana" else (" por la tarde" if time_pref == "tarde" else (" por la noche" if time_pref == "noche" else ""))
                send_text(
                    From,
                    f"Estos son algunos horarios disponibles{pref_txt} el {parsed_date.strftime('%d/%m/%Y')}:\n{sample}\n"
                    "¿A qué *hora exacta* te gustaría agendar?"
                )
            return ""

        # B: hora SÍ, fecha NO -> pedir fecha
        if explicit_time and not parsed_date:
            send_text(From, "Perfecto. ¿Qué *día* te gustaría?")
            return ""

        # C: fecha SÍ y hora SÍ -> reservar slot y pedir nombre si falta
        if parsed_date and explicit_time:
            target_h, target_m = explicit_time
            for db in db_session():
                slots = available_slots(db, parsed_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, "No veo horarios ese día. ¿Quieres que te proponga alternativas?")
                    break
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = reserve_or_update(db, patient, match)
                    if not patient.name:
                        send_text(
                            From,
                            f"📌 Reservé {appt.start_at.strftime('%d/%m/%Y %H:%M')}.\n"
                            "¿A nombre de quién agendamos la cita? (Nombre y apellido)"
                        )
                    else:
                        send_text(
                            From,
                            f"📌 Reservé {appt.start_at.strftime('%d/%m/%Y %H:%M')} a nombre de *{patient.name}*.\n"
                            "Escribe *confirmar* para confirmar o *cambiar* si prefieres otra hora."
                        )
                else:
                    # No exacto → sugerir cercanos
                    sorted_by_diff = sorted(
                        slots,
                        key=lambda x: abs((x.hour*60 + x.minute) - (target_h*60 + target_m))
                    )
                    sample = human_list(sorted_by_diff, limit=6)
                    send_text(
                        From,
                        "No tengo exactamente esa hora, pero cuento con estas opciones cercanas:\n" + sample +
                        "\n¿Te funciona alguna? Escribe la *hora exacta* o dime *cambiar* para más opciones."
                    )
            return ""

        # D: no hay suficiente info → pedir mínimo la fecha
        send_text(From, reply or "¿Qué día te gustaría?")
        return ""

    # 7) Smalltalk / saludo por NLU
    if intent in ("smalltalk", "greet"):
        if reply:
            send_text(From, reply)
            return ""

    # 8) Parser natural (último recurso)
    try:
        dt = dtparser.parse(text, dayfirst=False, fuzzy=True)
        d = dt.date()
        lowered = text.lower()
        has_time_hint = (":" in lowered) or (" am" in lowered) or (" pm" in lowered)
        for db in db_session():
            slots = available_slots(db, d, settings.TIMEZONE)
            if not slots:
                send_text(From, "No veo horarios ese día. ¿Quieres intentar con otro día u otro turno (mañana/tarde)?")
                break
            if has_time_hint:
                target_h = dt.hour
                target_m = dt.minute
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = reserve_or_update(db, patient, match)
                    if not patient.name:
                        send_text(
                            From,
                            f"📌 Reservé {appt.start_at.strftime('%d/%m/%Y %H:%M')}.\n"
                            "¿A nombre de quién agendamos la cita? (Nombre y apellido)"
                        )
                    else:
                        send_text(
                            From,
                            f"📌 Reservé {appt.start_at.strftime('%d/%m/%Y %H:%M')} a nombre de *{patient.name}*.\n"
                            "Escribe *confirmar* para confirmar o *cambiar* si prefieres otra hora."
                        )
                else:
                    sample = human_list(slots, limit=6)
                    send_text(
                        From,
                        "No tengo exactamente esa hora, pero cuento con:\n" + sample +
                        "\nResponde con la *hora exacta* que prefieras (ej. 10:30), o escribe *cambiar* para más opciones."
                    )
            else:
                sample = human_list(slots, limit=6)
                send_text(
                    From,
                    "Estos son algunos horarios que tengo:\n" + sample +
                    "\n¿A qué *hora exacta* te gustaría agendar?"
                )
        return ""
    except Exception:
        pass

    # 9) Fallback final (respuesta natural del NLU)
    final = analizar(raw_text)
    send_text(From, final.get("reply", "¿Buscas agendar, confirmar/reprogramar o información (costos, ubicación, preparación)?"))
    return ""