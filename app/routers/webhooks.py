from fastapi import APIRouter, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from dateutil import parser as dtparser
import unicodedata
from zoneinfo import ZoneInfo  # NUEVO

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

# ---- Contexto volátil por contacto (guarda día/slots entre mensajes) ----
PENDING = {}  # { contact: {"date": date, "slots": [datetime,...]} }

def set_pending(contact, date_obj, slots_list):
    PENDING[contact] = {"date": date_obj, "slots": slots_list}

def get_pending(contact):
    return PENDING.get(contact) or {}
# -------------------------------------------------------------------------

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

    # Saludo inicial (humano)
    if text in ("hola", "buenas", "menu", "menú", "buenos dias", "buenas tardes", "buenas noches"):
        send_text(
            From,
            "👋 ¡Hola! Soy el asistente virtual del Dr. Ontiveros, cardiólogo intervencionista.\n"
            "Estoy aquí para ayudarte de forma rápida y sencilla.\n\n"
            "¿En qué puedo apoyarte hoy?\n"
            "• Programar una cita\n"
            "• Confirmar o reprogramar\n"
            "• Información sobre costos, ubicación o preparación\n\n"
            "Cuéntame y te ayudo enseguida."
        )
        return ""

    # 🧠 NLU (intenciones + entidades)
    nlu = analizar(raw_text)
    intent = nlu.get("intent", "fallback")
    entities = nlu.get("entities", {}) or {}
    reply = nlu.get("reply", "")

    print(f"[NLU] from={From} intent={intent} entities={entities} text={(raw_text)[:120]}")

    nlu_date = entities.get("date")
    time_pref = entities.get("time_pref")  # "manana"/"tarde"/"noche"
    topic = entities.get("topic")

    # Información
    if intent == "info":
        if topic in ("costos", "costo", "precio", "precios"):
            send_text(From, "Los costos varían según el tipo de consulta. ¿Quieres saber el precio de consulta inicial o de seguimiento?")
            return ""
        if topic in ("ubicacion", "direccion"):
            send_text(From, "📍 Estamos en Clínica ABC, Av. Ejemplo 123, León, Gto. Hay estacionamiento en sitio 🚗.")
            return ""
        if topic in ("preparacion",):
            send_text(From, "Te recomiendo llegar 10 min antes, llevar identificación y estudios previos si los tienes.")
            return ""
        send_text(From, reply or "¿Buscas costos, ubicación o preparación?")
        return ""

    # Agendar / Reprogramar — con preferencia por turno + guardado de contexto
    if intent in ("book", "reschedule"):
        if nlu_date:
            try:
                d = dtparser.parse(nlu_date).date()
                for db in db_session():
                    slots = available_slots(db, d, settings.TIMEZONE)
                    if not slots:
                        send_text(From, "No veo horarios ese día. ¿Prefieres otro día u otro turno (mañana/tarde)?")
                        break

                    # Prioriza según time_pref si viene (manana/tarde/noche)
                    if time_pref == "manana":
                        filtered = [s for s in slots if 6 <= s.hour < 12]
                    elif time_pref == "tarde":
                        filtered = [s for s in slots if 12 <= s.hour < 18]
                    elif time_pref == "noche":
                        filtered = [s for s in slots if 18 <= s.hour <= 22]
                    else:
                        filtered = slots

                    show = filtered if filtered else slots  # si no hay del turno, muestra generales

                    # Guarda contexto para la siguiente respuesta (hora suelta)
                    set_pending(From, d, slots)

                    sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in show[:6])
                    send_text(
                        From,
                        "Estos son algunos horarios que tengo:\n" + sample +
                        "\nResponde con la *hora exacta* que prefieras (por ejemplo: 10:30 o 4:15 pm)."
                    )
                return ""
            except Exception:
                pass
        send_text(From, reply or "¿Qué día te gustaría?")
        return ""

    # Confirmar (solo si hay un horario reservado pendiente)
    if intent == "confirm":
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt or appt.status != models.AppointmentStatus.reserved:
                send_text(From, "Para confirmar necesito un horario reservado. Si quieres, escribe *agendar* o *cambiar*.")
                break
            appt.status = models.AppointmentStatus.confirmed
            db.commit()
            send_text(From, f"✅ Tu cita quedó confirmada para {appt.start_at.strftime('%d/%m/%Y a las %H:%M')}.\n¿Algo más en lo que te ayude?")
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

    # Smalltalk / Greet detectado por NLU
    if intent in ("smalltalk", "greet"):
        if reply:
            send_text(From, reply)
            return ""

    # Parser natural de fecha y HORA (p. ej. “15 de agosto 10:30 am”)
    try:
        tz = ZoneInfo(settings.TIMEZONE)
        pending = get_pending(From)
        pending_date = pending.get("date")
        pending_slots = pending.get("slots")  # lista completa del día, sin filtrar

        # Intentamos parsear lo que mandó (puede ser solo hora)
        dt = dtparser.parse(text, dayfirst=False, fuzzy=True)

        # ¿El usuario mandó solo hora? Heurística simple:
        lowered = raw_text.lower()
        has_explicit_date = any(k in lowered for k in ["202", "20/", "/", "ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"])
        only_time = (":" in lowered or " am" in lowered or " pm" in lowered) and not has_explicit_date

        if only_time and pending_date:
            # Combina la hora con el día en contexto
            target_h, target_m = dt.hour, dt.minute

            # Si ya teníamos los slots del día, busca coincidencia exacta
            if pending_slots:
                match = None
                for s in pending_slots:
                    if s.hour == target_h and s.minute == target_m:
                        match = s
                        break
                if match:
                    send_text(
                        From,
                        f"📌 Tengo {match.strftime('%d/%m/%Y %H:%M')} disponible.\n"
                        "Escribe *confirmar* para confirmar o *cambiar* para ver otras opciones."
                    )
                    return ""
                else:
                    sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in pending_slots[:6])
                    send_text(
                        From,
                        "No tengo exactamente esa hora, pero cuento con:\n" + sample +
                        "\nResponde con otra *hora exacta* (ej. 11:00), o escribe *cambiar* para más opciones."
                    )
                    return ""
            else:
                # No teníamos slots guardados (p.ej. reinicio). Los recalculamos.
                for db in db_session():
                    slots = available_slots(db, pending_date, settings.TIMEZONE)
                    if not slots:
                        send_text(From, "No veo horarios ese día. ¿Quieres intentar con otro día u otro turno (mañana/tarde)?")
                        break
                    match = None
                    for s in slots:
                        if s.hour == target_h and s.minute == target_m:
                            match = s
                            break
                    if match:
                        send_text(
                            From,
                            f"📌 Tengo {match.strftime('%d/%m/%Y %H:%M')} disponible.\n"
                            "Escribe *confirmar* para confirmar o *cambiar* para ver otras opciones."
                        )
                        return ""
                    sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in slots[:6])
                    send_text(
                        From,
                        "No tengo exactamente esa hora, pero cuento con:\n" + sample +
                        "\nResponde con otra *hora exacta* (ej. 11:00), o escribe *cambiar* para más opciones."
                    )
                return ""

        # Si trajo fecha completa (o no hay contexto), comportamiento normal
        d = dt.date()
        for db in db_session():
            slots = available_slots(db, d, settings.TIMEZONE)
            if not slots:
                send_text(From, "No veo horarios ese día. ¿Quieres que busque en otra fecha o turno (mañana/tarde)?")
                break
            sample = "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in slots[:6])
            # Actualiza contexto al nuevo día sugerido
            set_pending(From, d, slots)
            send_text(
                From,
                "Estos son algunos horarios que tengo:\n" + sample +
                "\nResponde con la *hora exacta* que prefieras (ej. 10:30), o escribe *cambiar* para más opciones."
            )
        return ""
    except Exception:
        pass

    # Fallback final (respuesta natural del NLU)
    final = analizar(raw_text)  # devuelve dict
    send_text(From, final.get("reply", "¿Buscas agendar, confirmar/reprogramar o información (costos, ubicación, preparación)?"))
    return ""