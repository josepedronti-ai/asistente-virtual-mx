from fastapi import APIRouter, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from dateutil import parser as dtparser
import unicodedata, re
from datetime import datetime, timedelta, date

from ..database import SessionLocal
from ..config import settings
from .. import models
from ..services.notifications import send_text
from ..services.scheduling import available_slots
from ..services.nlu import analizar

# =========================
# Memoria corta (contexto)
# =========================
SESSION_CTX: dict[str, dict] = {}
CTX_TTL_MIN = 15

def set_ctx(contact: str, last_date):
    SESSION_CTX[contact] = {
        "last_date": last_date,  # date
        "ts": datetime.utcnow(),
    }

def get_ctx(contact: str):
    d = SESSION_CTX.get(contact)
    if not d:
        return None
    if (datetime.utcnow() - d["ts"]) > timedelta(minutes=CTX_TTL_MIN):
        SESSION_CTX.pop(contact, None)
        return None
    return d

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
    p = models.Patient(contact=contact)  # name se pedirá después
    db.add(p)
    db.commit()
    db.refresh(p)
    return p

def reserve_or_update(db: Session, patient: models.Patient, start_dt: datetime) -> models.Appointment:
    """
    Si ya hay una cita RESERVADA reciente del paciente, la mueve a start_dt.
    Si no, crea una nueva con type='consulta' por defecto.
    """
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
            type="consulta",
            start_at=start_dt,
            status=models.AppointmentStatus.reserved,
            channel=models.Channel.whatsapp,
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
    if not re.fullmatch(r"[A-Za-zÁÉÍÓÚáéíóúÑñ'’\s]{3,60}", t):
        return None
    parts = t.split()
    if len(parts) < 2 or len(parts) > 5:
        return None
    clean = " ".join(p.capitalize() for p in parts)
    return clean

# --- Extractor de fechas en español ---
_WEEK_MAP = {
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2,
    "jueves": 3, "viernes": 4, "sabado": 5, "sábado": 5, "domingo": 6
}
def extract_spanish_date(text: str, today: date) -> date | None:
    t = (text or "").lower()
    if "pasado mañana" in t:
        return today + timedelta(days=2)
    if "mañana" in t:
        return today + timedelta(days=1)
    if "hoy" in t:
        return today
    for w, idx in _WEEK_MAP.items():
        if re.search(rf"\b{w}\b", t):
            delta = (idx - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + timedelta(days=delta)
    try:
        dt = dtparser.parse(t, dayfirst=False, fuzzy=True)
        return dt.date()
    except Exception:
        return None

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
                    f"🧾 Gracias, *{patient.name}*. ¿Deseas confirmar la cita para "
                    f"{pending.start_at.strftime('%d/%m/%Y a las %H:%M')}? "
                    "Escribe **confirmar** o **cambiar**."
                )
                return ""
            else:
                send_text(From, "🧾 ¿A nombre de quién agendamos la cita? *(Nombre y apellido)*")
                return ""

    # 1) Saludo profesional
    if text in ("hola", "buenas", "menu", "menú", "buenos dias", "buenas tardes", "buenas noches"):
        nombre_opt = ""
        for db in db_session():
            p = get_patient_by_contact(db, From)
            if p and p.name:
                nombre_opt = f" {p.name}"
            break
        send_text(
            From,
            f"👋 ¡Hola{nombre_opt}! Soy el asistente del **Dr. Ontiveros** (Cardiólogo intervencionista 🫀).\n"
            "Cuéntame, ¿en qué puedo apoyarte hoy?\n\n"
            "• 📅 **Agendar** una cita\n"
            "• 🔄 **Confirmar** o **reprogramar**\n"
            "• 💳 **Costos** y 📍 **ubicación**\n"
            "• ❓ **Otras dudas** o información general."
        )
        return ""

    # Atajo previo (hora sola + contexto)
    explicit_time_pre = parse_time_hint(raw_text)
    ctx = get_ctx(From)

    # 2) NLU
    nlu = analizar(raw_text)
    intent = nlu.get("intent", "fallback")
    entities = nlu.get("entities", {}) or {}
    reply = nlu.get("reply", "")

    print(f"[NLU] from={From} intent={intent} entities={entities} text={(raw_text)[:120]}")

    nlu_date = entities.get("date") or ""
    topic = entities.get("topic") or ""

    # 2.b) “Otros horarios / más tarde / más opciones”
    if re.search(r"\b(otro|otra|otros|otras|más|mas)\b.*\b(horario|hora|opcion|opciones|turno|turnos)\b", raw_text.lower()):
        d = ctx.get("last_date") if ctx else None
        if d:
            for db in db_session():
                slots = available_slots(db, d, settings.TIMEZONE)
                if not slots:
                    send_text(
                        From,
                        "😔 **Vaya… parece que ese día ya está lleno.**\n"
                        "Pero no te preocupes 😊, puedo buscarte otros días cercanos para que no tengas que esperar demasiado.\n"
                        "¿Cuál sería tu **siguiente opción**?"
                    )
                    break
                sample = human_list(slots, limit=10)
                send_text(
                    From,
                    f"🕘 Te comparto **más horarios** del *{d.strftime('%d/%m/%Y')}*:\n{sample}\n"
                    "¿Cuál te viene mejor?"
                )
            return ""
        # Si no hay fecha en contexto, pedirla
        send_text(From, "📅 ¿De qué **día** te muestro más horarios?")
        return ""

    # Farewell rápido
    if text in ("no", "no gracias", "gracias", "listo", "es todo", "ninguno", "ninguna"):
        send_text(From, "💙 **¡Un gusto ayudarte!**\nCuando lo necesites, aquí estaré para apoyarte.")
        return ""

    # 3) Info general
    if intent == "info" and not explicit_time_pre:
        if topic in ("costos", "costo", "precio", "precios"):
            send_text(
                From,
                "💵 *Costos de consulta y estudios:*\n"
                "• **Consulta de primera vez:** $1,200\n"
                "• **Consulta subsecuente:** $1,200\n"
                "• **Valoración Preoperatoria:** $1,500\n"
                "• **Ecocardiograma transtorácico:** $3,000\n"
                "• **Prueba de esfuerzo:** $2,800\n"
                "• **Holter 24 horas:** $2,800\n"
                "• **Monitoreo ambulatorio de presión arterial (MAPA):** $2,800"
            )
            return ""
        if topic in ("ubicacion", "ubicación", "direccion", "dirección"):
            send_text(
                From,
                "📍 *Ubicación*\n"
                "**CLIEMED**, Av. Prof. Moisés Sáenz 1500, Leones, 64600, Monterrey, N.L. 🚗"
            )
            return ""
        send_text(From, "Con gusto, ¿te interesa **costos** o **ubicación**?")
        return ""

    # 4) Confirmar
    if intent == "confirm" and not explicit_time_pre:
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt:
                send_text(From, "Para confirmar necesito un horario reservado. Si quieres, escribe *agendar* o *cambiar*.")
                break
            patient = get_patient_by_contact(db, From)
            if patient and (not patient.name or not patient.name.strip()):
                send_text(From, "🧾 Antes de confirmar, ¿a nombre de quién la agendamos? *(Nombre y apellido)*")
                break
            appt.status = models.AppointmentStatus.confirmed
            db.commit()
            name_txt = f" de *{patient.name}*" if patient and patient.name else ""
            send_text(From, f"✅ Tu cita{name_txt} quedó confirmada para {appt.start_at.strftime('%d/%m/%Y a las %H:%M')}.\n💬 **¿Te ayudo en algo más?**")
        return ""

    # 5) Cancelar
    if intent == "cancel" and not explicit_time_pre:
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt:
                send_text(From, "No encontré una cita reservada. ¿Quieres agendar una nueva?")
                break
            appt.status = models.AppointmentStatus.canceled
            db.commit()
            send_text(From, "🗓️ He cancelado tu cita. Si quieres, puedo proponerte nuevos horarios.")
        return ""

    # 6) Agendar / Reprogramar (incluye atajo de hora+contexto)
    if intent in ("book", "reschedule") or explicit_time_pre:
        today_local = datetime.now().date()
        parsed_date = None

        # 1) del NLU (hoy/mañana/pasado/día semana)
        if nlu_date:
            parsed_date = extract_spanish_date(nlu_date, today_local)

        # 2) si no, intenta extraer desde el propio texto
        if not parsed_date:
            parsed_date = extract_spanish_date(raw_text, today_local)

        # 3) si no hay fecha pero tenemos contexto y hay hora → usa la fecha en contexto
        explicit_time = parse_time_hint(raw_text)
        if not parsed_date and explicit_time and ctx and ctx.get("last_date"):
            parsed_date = ctx["last_date"]

        # Caso A: fecha SÍ, hora NO -> pedir hora y guardar contexto
        if parsed_date and not explicit_time:
            for db in db_session():
                slots = available_slots(db, parsed_date, settings.TIMEZONE)
                if not slots:
                    send_text(
                        From,
                        "😔 **Vaya… parece que ese día ya está lleno.**\n"
                        "Pero no te preocupes 😊, puedo buscarte otros días cercanos para que no tengas que esperar demasiado.\n"
                        "¿Cuál sería tu **siguiente opción**?"
                    )
                    break
                sample = human_list(slots, limit=6)
                set_ctx(From, parsed_date)
                send_text(
                    From,
                    f"🕘 Estos son algunos horarios disponibles el *{parsed_date.strftime('%d/%m/%Y')}*:\n{sample}\n"
                    "¿A qué **hora exacta** te gustaría agendar?"
                )
            return ""

        # Caso B: hora SÍ, fecha NO y SIN contexto → pedir fecha
        if explicit_time and not parsed_date:
            send_text(From, "📅 ¡Perfecto! ¿Qué **día** te gustaría?")
            return ""

        # Caso C: fecha SÍ y hora SÍ -> reservar slot (y pedir nombre si falta)
        if parsed_date and explicit_time:
            target_h, target_m = explicit_time
            for db in db_session():
                slots = available_slots(db, parsed_date, settings.TIMEZONE)
                if not slots:
                    send_text(
                        From,
                        "😔 **Vaya… parece que ese día ya está lleno.**\n"
                        "Pero no te preocupes 😊, puedo buscarte otros días cercanos para que no tengas que esperar demasiado.\n"
                        "¿Cuál sería tu **siguiente opción**?"
                    )
                    break
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = reserve_or_update(db, patient, match)
                    SESSION_CTX.pop(From, None)
                    if not patient.name:
                        send_text(
                            From,
                            f"📌 Reservé *{appt.start_at.strftime('%d/%m/%Y %H:%M')}*.\n"
                            "🧾 ¿A nombre de quién agendamos la cita? *(Nombre y apellido)*"
                        )
                    else:
                        send_text(
                            From,
                            f"📌 Reservé *{appt.start_at.strftime('%d/%m/%Y %H:%M')}* a nombre de *{patient.name}*.\n"
                            "Escribe **confirmar** para confirmar o **cambiar** si prefieres otra hora."
                        )
                else:
                    # no exacto → sugerir cercanos
                    sorted_by_diff = sorted(
                        slots,
                        key=lambda x: abs((x.hour*60 + x.minute) - (target_h*60 + target_m))
                    )
                    sample = human_list(sorted_by_diff, limit=6)
                    set_ctx(From, parsed_date)
                    send_text(
                        From,
                        "⏰ **Esa hora ya no está libre**, pero encontré estos horarios cercanos que podrían servirte:\n"
                        f"{sample}\n"
                        "✨ **Dime si alguno te funciona o si prefieres que te proponga otra fecha.**"
                    )
            return ""

        # Caso D: sin suficiente info
        send_text(From, reply or "📅 ¿Qué **día** te gustaría?")
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
                send_text(
                    From,
                    "😔 **Vaya… parece que ese día ya está lleno.**\n"
                    "Pero no te preocupes 😊, puedo buscarte otros días cercanos para que no tengas que esperar demasiado.\n"
                    "¿Cuál sería tu **siguiente opción**?"
                )
                break
            if has_time_hint:
                target_h = dt.hour
                target_m = dt.minute
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = reserve_or_update(db, patient, match)
                    SESSION_CTX.pop(From, None)
                    if not patient.name:
                        send_text(
                            From,
                            f"📌 Reservé *{appt.start_at.strftime('%d/%m/%Y %H:%M')}*.\n"
                            "🧾 ¿A nombre de quién agendamos la cita? *(Nombre y apellido)*"
                        )
                    else:
                        send_text(
                            From,
                            f"📌 Reservé *{appt.start_at.strftime('%d/%m/%Y %H:%M')}* a nombre de *{patient.name}*.\n"
                            "Escribe **confirmar** para confirmar o **cambiar** si prefieres otra hora."
                        )
                else:
                    sample = human_list(slots, limit=6)
                    set_ctx(From, d)
                    send_text(
                        From,
                        "⏰ **Esa hora ya no está libre**, pero encontré estos horarios cercanos que podrían servirte:\n"
                        f"{sample}\n"
                        "✨ **Dime si alguno te funciona o si prefieres que te proponga otra fecha.**"
                    )
            else:
                sample = human_list(slots, limit=6)
                set_ctx(From, d)
                send_text(
                    From,
                    "🕘 Estos son algunos horarios que tengo:\n" + sample +
                    "\n¿A qué **hora exacta** te gustaría agendar?"
                )
        return ""
    except Exception:
        pass

    # 9) Fallback final (amable)
    send_text(
        From,
        "🤔 **No estoy seguro de haber entendido.**\n"
        "¿Quieres **agendar**, **confirmar/reprogramar** o saber sobre **costos** y **ubicación**?"
    )
    return ""