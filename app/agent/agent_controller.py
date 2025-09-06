# app/agent/agent_controller.py
from __future__ import annotations
import os, json, re, unicodedata, uuid
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import logging

from openai import OpenAI

from ..config import settings
from ..database import SessionLocal
from .. import models
from ..services.scheduling import available_slots, create_event, update_event, delete_event
from ..replygen.core import generate_reply

try:
    from dateparser import parse as dp_parse
except Exception:
    dp_parse = None  # la tool parse_date fallará con mensaje si no está instalado

logger = logging.getLogger(__name__)

# -----------------------
# Memoria simple por contacto
# -----------------------
_AGENT_SESSIONS: dict[str, dict] = {}
TTL_MIN = 20

# 🔹 Memoria auxiliar: último HINT_FECHA resuelto por contacto
_LAST_DATE_HINT: dict[str, str] = {}

def _now():
    return datetime.utcnow()

def _now_local() -> datetime:
    tz = getattr(settings, "TIMEZONE", "America/Monterrey") or "America/Monterrey"
    return datetime.now(ZoneInfo(tz))

def _get_mem(contact: str):
    ctx = _AGENT_SESSIONS.get(contact)
    if not ctx:
        return None
    if (_now() - ctx.get("ts", _now())) > timedelta(minutes=TTL_MIN):
        _AGENT_SESSIONS.pop(contact, None)
        return None
    return ctx

def _save_mem(contact: str, messages: list[dict], greeted: bool | None = None):
    prev = _AGENT_SESSIONS.get(contact) or {}
    state = {"ts": _now(), "messages": messages[-50:], "greeted": prev.get("greeted", False)}
    if greeted is not None:
        state["greeted"] = bool(greeted)
    _AGENT_SESSIONS[contact] = state

# -----------------------
# DB helpers (copiados para evitar dependencias circulares)
# -----------------------
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_patient_by_contact(db, contact: str):
    return db.query(models.Patient).filter(models.Patient.contact == contact).first()

def get_or_create_patient(db, contact: str):
    p = get_patient_by_contact(db, contact)
    if p:
        return p
    p = models.Patient(contact=contact)
    db.add(p); db.commit(); db.refresh(p)
    return p

def find_latest_active_for_contact(db, contact: str):
    return (
        db.query(models.Appointment)
        .join(models.Patient)
        .filter(models.Patient.contact == contact)
        .filter(models.Appointment.status.in_([
            models.AppointmentStatus.reserved,
            models.AppointmentStatus.confirmed
        ]))
        .order_by(models.Appointment.start_at.desc())
        .first()
    )

def move_or_create_appointment(db, patient: models.Patient, start_dt_naive_local: datetime) -> models.Appointment:
    """
    start_dt_naive_local: datetime SIN tzinfo (hora local).
    """
    appt = (
        db.query(models.Appointment)
        .filter(models.Appointment.patient_id == patient.id)
        .filter(models.Appointment.status.in_([
            models.AppointmentStatus.reserved,
            models.AppointmentStatus.confirmed
        ]))
        .order_by(models.Appointment.start_at.desc())
        .first()
    )
    if appt:
        appt.start_at = start_dt_naive_local
    else:
        appt = models.Appointment(
            patient_id=patient.id,
            type="consulta",
            start_at=start_dt_naive_local,
            status=models.AppointmentStatus.reserved,
            channel=models.Channel.whatsapp,
        )
        db.add(appt)
    db.commit(); db.refresh(appt)
    return appt

# -----------------------
# Utilidades horarias (parser compacto)
# -----------------------
def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    return "".join(ch for ch in s if unicodedata.category(ch) != "Mn")

def parse_time_hint_basic(text: str) -> tuple[int,int] | None:
    t = _norm(text)
    if re.search(r"\bmedianoche\b", t): return (0,0)
    if re.search(r"\bmediodia|medio dia\b", t): return (12,0)

    period = None
    if re.search(r"\b(tarde|noche)\b", t): period = "pm"
    if re.search(r"\bmanana\b", t): period = "am"
    if re.search(r"\bmadrugada\b", t): period = "am"

    m = re.search(r"\b([01]?\d|2[0-3])\s*[:\.]\s*([0-5]\d)\s*(am|pm)?\b", t)
    if m:
        h = int(m.group(1)); mm = int(m.group(2)); ap = (m.group(3) or "")
        if ap == "pm" and h != 12: h += 12
        if ap == "am" and h == 12: h = 0
        if not ap and period == "pm" and 1 <= h <= 11: h += 12
        if not ap and period == "am" and h == 12: h = 0
        return (h, mm)

    m = re.search(r"\b([1-9]|1[0-2])\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1)); ap = m.group(2)
        if ap == "pm" and h != 12: h += 12
        if ap == "am" and h == 12: h = 0
        return (h, 0)

    m = re.search(r"\b([1-9]|1[0-2])\s*(?:de\s+la\s+)?(manana|tarde|noche|madrugada)\b", t)
    if m:
        h = int(m.group(1)); per = m.group(2)
        if per in ("tarde","noche") and h != 12: h += 12
        if per in ("manana","madrugada") and h == 12: h = 0
        return (h, 0)

    PAL = {"una":1, "uno":1, "dos":2, "tres":3, "cuatro":4, "cinco":5, "seis":6, "siete":7, "ocho":8, "nueve":9, "diez":10, "once":11, "doce":12}
    m = re.search(r"\b(una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)\s+y\s+(media|cuarto)\b", t)
    if m:
        h = PAL[m.group(1)]; mm = 30 if m.group(2) == "media" else 15
        if period == "pm" and h != 12: h += 12
        if period == "am" and h == 12: h = 0
        return (h, mm)

    m = re.search(r"\b(una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)\s+menos\s+cuarto\b", t)
    if m:
        h = PAL[m.group(1)] - 1
        if h <= 0: h = 12
        if period == "pm" and h != 12: h += 12
        if period == "am" and h == 12: h = 0
        return (h, 45)

    m = re.search(r"\b(0?\d|1\d|2[0-3])\s*(h|hrs|horas?)\b", t)
    if m:
        return (int(m.group(1)), 0)

    m = re.search(r"\b(0?\d|1\d|2[0-3])\b", t)
    if m:
        h = int(m.group(1))
        if period == "pm" and 1 <= h <= 11: h += 12
        if period == "am" and h == 12: h = 0
        return (h, 0)

    return None

def hhmm_from_text_or_none(text: str) -> str | None:
    t = parse_time_hint_basic(text)
    return f"{t[0]:02d}:{t[1]:02d}" if t else None

# -----------------------
# Herramientas (llamadas por el Agente)
# -----------------------
def tool_check_slots(contact: str, date_iso: str):
    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    for db in db_session():
        slots = available_slots(db, d, getattr(settings, "TIMEZONE", "America/Monterrey") or "America/Monterrey") or []
        return {"date_iso": date_iso, "slots": [s.strftime("%H:%M") for s in slots]}

def tool_book_appointment(contact: str, date_iso: str, time_hhmm: str, patient_name: str, channel: str, client_request_id: str):
    # Validación básica
    if not (patient_name and patient_name.strip() and len(patient_name.strip()) >= 3):
        return {"ok": False, "reason": "need_name"}

    d = datetime.strptime(date_iso, "%Y-%m-%d").date()
    h, m = map(int, time_hhmm.split(":"))
    tzname = getattr(settings, "TIMEZONE", "America/Monterrey") or "America/Monterrey"
    tz = ZoneInfo(tzname)

    # Aware local solo para cálculo; guardamos NAIVE LOCAL en BD
    start_dt_local_aware = datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
    start_dt_local_naive = start_dt_local_aware.replace(tzinfo=None)

    for db in db_session():
        # validar slot contra GCAL + BD
        slots = available_slots(db, d, tzname) or []
        allowed = any(s.hour == h and s.minute == m for s in slots)
        if not allowed:
            logger.info("Slot no disponible: %s %s (contact=%s) alternatives=%s", date_iso, time_hhmm, contact, [s.strftime("%H:%M") for s in slots])
            return {"ok": False, "reason": "slot_unavailable", "alternatives": [s.strftime("%H:%M") for s in slots]}

        patient = get_or_create_patient(db, contact)
        patient.name = patient_name.strip().title()
        db.commit()

        # crea o mueve en BD (SIEMPRE NAIVE LOCAL)
        appt = move_or_create_appointment(db, patient, start_dt_local_naive)
        appt.status = models.AppointmentStatus.confirmed

        duration = getattr(settings, "EVENT_DURATION_MIN", 30)

        # 👉 SINCRONIZAR GCAL
        try:
            if appt.event_id:
                # Si ya hay evento, intentar moverlo
                try:
                    logger.info("Intentando update_event en GCAL: event_id=%s -> %s %s (local)", appt.event_id, date_iso, time_hhmm)
                    update_event(appt.event_id, start_dt_local_naive, duration_min=duration)
                    logger.info("GCAL update_event OK: event_id=%s", appt.event_id)
                except Exception as e_upd:
                    logger.warning("GCAL update_event falló; recreando. appt_id=%s err=%s", getattr(appt, "id", None), e_upd)
                    # Fallback: borra y crea
                    try:
                        delete_event(appt.event_id)
                    except Exception as e_del:
                        logger.warning("GCAL delete_event falló durante fallback: %s", e_del)
                    appt.event_id = None

            if not appt.event_id:
                # Crear desde cero
                logger.info("Creando evento en GCAL: contact=%s patient=%s start_local_naive=%s tz=%s",
                            contact, patient.name, appt.start_at.isoformat(), tzname)
                new_id = create_event(
                    summary=f"Consulta — {patient.name or 'Paciente'}",
                    start_local=appt.start_at,  # NAIVE LOCAL; scheduling.create_event localiza TZ
                    duration_min=duration,
                    location="CLIEMED, Av. Prof. Moisés Sáenz 1500, Monterrey, N.L.",
                    description=f"Canal: WhatsApp\nPaciente: {patient.name or patient.contact}",
                )
                appt.event_id = new_id
                logger.info("GCAL create_event OK: event_id=%s appt_id=%s", new_id, getattr(appt, "id", None))
        except Exception as e:
            logger.exception("Sincronización GCAL falló (book): contact=%s appt_id=%s err=%s", contact, getattr(appt, "id", None), e)

        db.commit()
        logger.info("Cita confirmada en DB: appt_id=%s contact=%s start_at_naive_local=%s event_id=%s",
                    getattr(appt, "id", None), contact, appt.start_at.isoformat(), appt.event_id)

        return {
            "ok": True,
            "patient_name": patient.name or "",
            "date_iso": date_iso,
            "time_hhmm": time_hhmm,
            "event_id": appt.event_id or None
        }

def tool_reschedule_appointment(contact: str, date_iso: str, time_hhmm: str, client_request_id: str):
    # --- sanity: si la fecha viene en pasado (años atrás), clámpeala a HOY local ---
    tzname = getattr(settings, "TIMEZONE", "America/Monterrey") or "America/Monterrey"
    tz = ZoneInfo(tzname)
    today_local = datetime.now(tz).date()

    try:
        d_req = datetime.strptime(date_iso, "%Y-%m-%d").date()
    except Exception:
        d_req = today_local  # si viene mal, usa hoy

    # si la fecha pedida está >30 días en el pasado, usa HOY
    if d_req < (today_local - timedelta(days=30)):
        logger.warning("Fecha de reschedule en pasado (%s). Ajustando a hoy %s (contact=%s)", d_req, today_local, contact)
        d_req = today_local

    # parse hora
    try:
        h, m = map(int, time_hhmm.split(":"))
    except Exception:
        return {"ok": False, "reason": "bad_time"}

    start_dt_local_aware = datetime(d_req.year, d_req.month, d_req.day, h, m, tzinfo=tz)
    start_dt_local_naive = start_dt_local_aware.replace(tzinfo=None)

    for db in db_session():
        appt = find_latest_active_for_contact(db, contact)
        if not appt:
            logger.info("Reagendar pero sin cita activa: contact=%s", contact)
            return {"ok": False, "reason": "no_active"}

        # validar disponibilidad
        slots = available_slots(db, d_req, tzname) or []
        allowed = any(s.hour == h and s.minute == m for s in slots)
        if not allowed:
            return {"ok": False, "reason": "slot_unavailable", "alternatives": [s.strftime("%H:%M") for s in slots]}

        # actualiza BD (naive local)
        appt.start_at = start_dt_local_naive

        # sincroniza Calendar (update → fallback delete+create)
        try:
            if appt.event_id:
                try:
                    logger.info("Intentando update_event GCAL: event_id=%s → %s %s (local)", appt.event_id, d_req, time_hhmm)
                    update_event(appt.event_id, start_dt_local_naive, duration_min=getattr(settings, "EVENT_DURATION_MIN", 30))
                    logger.info("GCAL update_event OK: event_id=%s", appt.event_id)
                except Exception as e_upd:
                    logger.warning("GCAL update_event falló; creando nuevo. appt_id=%s err=%s", getattr(appt, "id", None), e_upd)
                    try:
                        delete_event(appt.event_id)
                    except Exception as e_del:
                        logger.warning("GCAL delete_event falló durante fallback: %s", e_del)
                    appt.event_id = None

            if not appt.event_id:
                patient = db.query(models.Patient).filter(models.Patient.id == appt.patient_id).first()
                pname = getattr(patient, "name", None) or "Paciente"
                new_event_id = create_event(
                    summary=f"Consulta — {pname}",
                    start_local=appt.start_at,  # naive local
                    duration_min=getattr(settings, "EVENT_DURATION_MIN", 30),
                    location="CLIEMED, Av. Prof. Moisés Sáenz 1500, Monterrey, N.L.",
                    description=f"Canal: WhatsApp\nPaciente: {pname}",
                )
                appt.event_id = new_event_id
                logger.info("Evento creado (reschedule) OK: event_id=%s appt_id=%s", new_event_id, getattr(appt, "id", None))
        except Exception as e:
            logger.exception("Sincronización GCAL falló (reschedule): contact=%s appt_id=%s err=%s", contact, getattr(appt, "id", None), e)
            # aún si falla calendar, guarda la BD para no perder el intento

        db.commit()
        return {"ok": True, "date_iso": d_req.isoformat(), "time_hhmm": time_hhmm, "event_id": appt.event_id or None}

def tool_cancel_appointment(contact: str):
    for db in db_session():
        appt = find_latest_active_for_contact(db, contact)
        if not appt:
            return {"ok": False, "reason": "no_active"}
        appt.status = models.AppointmentStatus.canceled
        if appt.event_id:
            try:
                delete_event(appt.event_id)
                logger.info("Evento eliminado OK: event_id=%s appt_id=%s", appt.event_id, getattr(appt, "id", None))
            except Exception as e:
                logger.exception("delete_event falló: %s", e)
            appt.event_id = None
        db.commit()
        return {"ok": True}

def tool_get_prices(contact: str):
    return {"text": generate_reply("prices", {})}

def tool_get_location(contact: str):
    return {"text": generate_reply("location", {})}

def tool_parse_time(contact: str, text: str):
    hhmm = hhmm_from_text_or_none(text)
    return {"hhmm": hhmm}

def tool_parse_date(contact: str, text: str, today_iso: str | None = None):
    """
    Normaliza fechas en español a YYYY-MM-DD (preferir futuro).
    """
    if not dp_parse:
        return {"date_iso": None, "error": "dateparser_not_installed"}
    base = datetime.strptime(today_iso, "%Y-%m-%d") if today_iso else datetime.utcnow()
    dt = dp_parse(
        text,
        languages=["es"],
        settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": base, "DATE_ORDER": "DMY"},
    )
    return {"date_iso": dt.date().isoformat() if dt else None}

# -----------------------
# Definición del Agente (prompt + tools schema)
# -----------------------
SYSTEM_PROMPT = (
    """
    Eres el asistente virtual del Dr. José Pedro Ontiveros Beltrán (cardiólogo clínico e intervencionista en Monterrey, México).
    Objetivo: agendar, reagendar o cancelar citas; y responder precios/ubicación.

    TONO Y ESTILO
    - Español de México, trato de “usted”.
    - Humano, amable, formal y profesional; claro y sin tecnicismos innecesarios; nada intrusivo.
    - Frases breves y bien estructuradas.
    - Presentación inicial (solo si es el primer mensaje del contacto o si el usuario saluda sin contexto, y sin haberla dado antes):
      “Hola, buenas (días/tardes/noches). Soy el asistente del Dr. Ontiveros. ¿En qué puedo ayudarle hoy?”
      • Escoge “días/tardes/noches” según la hora local de Monterrey (America/Monterrey).
      • Si el usuario ya expresa una intención clara (agendar/reagendar/cancelar/precios/ubicación), responde directo sin repetir la presentación.
    - Emojis permitidos únicamente para claridad visual en citas:
      📅 (fecha) y ⏰ (hora). Evita cualquier otro emoji y separadores raros (no uses “|”).

    REGLAS CRÍTICAS
    1) Jamás inventes disponibilidad: usa siempre herramientas para consultar horarios.
    2) Normalización de fechas:
       - Si el mensaje del usuario incluye un tag con el siguiente formato: [HINT_FECHA:YYYY-MM-DD],
         debes usar **esa** fecha como interpretación de términos relativos (“hoy”, “mañana”, “próximo lunes”, etc.).
       - En caso contrario, y si la fecha está ambigua, puedes llamar a la tool `parse_date`.
    3) Normalización de horas:
       - Si el usuario escribe “8 pm”/“ocho y media”, normaliza a HH:MM 24h (puedes usar `parse_time` si lo necesitas).
    4) Flujo no intrusivo al agendar:
       - Paso 1: pide **solo la fecha** (si no está clara).
       - Paso 2: luego pide **la hora**.
       - Paso 3: por último pide **nombre y apellido** del paciente para confirmar.
       - **Nunca** confirmes sin eco explícito de **FECHA + HORA + NOMBRE**.
       - Frase sugerida para el nombre: “Para confirmar, ¿me comparte el nombre y apellido del paciente, por favor?”.
       - Confirmación: “Quedó para el 📅 DD/MM/AAAA a las ⏰ HH:MM a nombre de NOMBRE.”
    5) Horario no disponible:
       - Si el servidor indica `slot_unavailable`, ofrece 4–8 alternativas del mismo día.
    6) Reprogramación/cancelación:
       - Antes de mover o cancelar, verifica que exista cita activa (usa herramientas). Si no hay, explícalo con cortesía.
    7) Mensajes concisos; sin insistir si el paciente no responde.
    8) Mantén idempotencia: cuando reserves o muevas, pasa un `client_request_id` único.

    FORMATO DE RESPUESTA
    - Fechas como “📅 27/08/2025” y horas “⏰ 19:30”.
    - No uses separadores “|”.
    - Si listaras horarios: “⏰ 16:00 · 16:30 · 17:00 …”.
    """
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_slots",
            "description": "Lista horarios disponibles para una fecha local (YYYY-MM-DD).",
            "parameters": {
                "type": "object",
                "properties": {"date_iso": {"type": "string"}},
                "required": ["date_iso"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_appointment",
            "description": "Reserva/actualiza una cita. Valida disponibilidad del lado servidor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_iso": {"type": "string"},
                    "time_hhmm": {"type": "string"},
                    "patient_name": {"type": "string"},
                    "channel": {"type": "string", "enum": ["whatsapp"]},
                    "client_request_id": {"type": "string"},
                },
                "required": ["date_iso","time_hhmm","patient_name","channel","client_request_id"]
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": "Mueve la última cita activa a una nueva fecha/hora.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_iso": {"type": "string"},
                    "time_hhmm": {"type": "string"},
                    "client_request_id": {"type": "string"},
                },
                "required": ["date_iso","time_hhmm","client_request_id"]
            },
        },
    },
    {
        "type": "function",
        "function": {"name": "cancel_appointment","description": "Cancela la última cita activa.","parameters": {"type":"object","properties":{}}}
    },
    {
        "type": "function",
        "function": {"name": "get_prices","description": "Tabla de precios vigente.","parameters": {"type":"object","properties":{}}}
    },
    {
        "type": "function",
        "function": {"name": "get_location","description": "Dirección y referencias.","parameters": {"type":"object","properties":{}}}
    },
    {
        "type": "function",
        "function": {
            "name": "parse_time",
            "description": "Normaliza hora libre a formato HH:MM (24h).",
            "parameters": {"type":"object","properties":{"text":{"type":"string"}},"required":["text"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "parse_date",
            "description": "Normaliza fecha libre en español a formato YYYY-MM-DD.",
            "parameters": {"type":"object","properties":{"text":{"type":"string"},"today_iso":{"type":"string"}},"required":["text"]}
        }
    },
]

def _dispatch_tool(contact: str, name: str, args: dict):
    if name == "check_slots":
        return tool_check_slots(contact, **args)
    if name == "book_appointment":
        return tool_book_appointment(contact, **args)
    if name == "reschedule_appointment":
        return tool_reschedule_appointment(contact, **args)
    if name == "cancel_appointment":
        return tool_cancel_appointment(contact)
    if name == "get_prices":
        return tool_get_prices(contact)
    if name == "get_location":
        return tool_get_location(contact)
    if name == "parse_time":
        return tool_parse_time(contact, **args)
    if name == "parse_date":
        return tool_parse_date(contact, **args)
    return {"error": f"unknown_tool:{name}"}

# -----------------------
# UX helpers
# -----------------------
_GREETING_WORDS = (
    "hola", "buenos dias", "buenos días", "buenas", "buenas tardes",
    "buenas noches", "qué tal", "que tal", "saludos"
)

_INTENT_HINTS = (
    "cita", "agendar", "agendarme", "reagendar", "cambiar", "mover", "cancelar",
    "precio", "costos", "costo", "tarifa", "ubicacion", "ubicación", "direccion",
    "dirección", "como llegar", "cómo llegar", "horario", "disponibilidad",
    "mañana", "manana", "hoy", "pasado", "lunes", "martes", "miercoles", "miércoles",
    "jueves", "viernes", "sabado", "sábado", "domingo"
)

def _is_pure_greeting(user_text: str) -> bool:
    t = _norm(user_text)
    has_greeting = any(g in t for g in _GREETING_WORDS)
    has_intent = any(k in t for k in _INTENT_HINTS)
    return has_greeting and not has_intent and len(t) <= 40

def _daypart_label(hour: int) -> str:
    # días 06–11, tardes 12–18, noches 19–05
    if 6 <= hour < 12:
        return "días"
    if 12 <= hour < 19:
        return "tardes"
    return "noches"

def _build_greeting() -> str:
    h = _now_local().hour
    tramo = _daypart_label(h)
    if tramo == "días":
        return "Hola, buenos días. Soy el asistente del Dr. Ontiveros. ¿En qué puedo ayudarle hoy?"
    return f"Hola, buenas {tramo}. Soy el asistente del Dr. Ontiveros. ¿En qué puedo ayudarle hoy?"

def _server_normalize_date_hint(text: str, today_iso: str | None = None) -> str | None:
    """
    Resuelve términos relativos tipo “hoy/mañana/próximo lunes…” a YYYY-MM-DD usando dateparser.
    No hace tool_calls; retorna solo la fecha si se detecta y puede resolverse.
    """
    if not dp_parse:
        return None
    t = _norm(text)
    patrones_relativos = [
        "hoy", "mañana", "manana", "el dia de manana", "el día de mañana", "para mañana", "para manana",
        "pasado mañana", "pasado manana",
        "próximo", "proximo", "próxima", "proxima",
        "esta semana", "la siguiente semana", "siguiente semana",
        "este", "siguiente",
        "el lunes", "el martes", "el miercoles", "el miércoles", "el jueves",
        "el viernes", "el sabado", "el sábado", "el domingo"
    ]
    if not any(p in t for p in patrones_relativos):
        return None
    base = datetime.strptime(today_iso, "%Y-%m-%d") if today_iso else datetime.utcnow()
    dt = dp_parse(
        text,
        languages=["es"],
        settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": base, "DATE_ORDER": "DMY"},
    )
    return dt.date().isoformat() if dt else None

# -----------------------
# Loop del Agente
# -----------------------
def _coerce_json(obj):
    if isinstance(obj, dict):
        return obj
    if isinstance(obj, str) and obj.strip():
        try:
            return json.loads(obj)
        except Exception:
            return {}
    return {}

def run_agent(contact: str, user_text: str) -> str:
    """
    Orquesta la conversación con el modelo y ejecuta herramientas locales.
    Devuelve el texto final que hay que enviar por WhatsApp.
    """
    # Garantiza OPENAI_API_KEY en entorno (Render lee de env)
    if settings.OPENAI_API_KEY and not os.getenv("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = settings.OPENAI_API_KEY

    # Instanciar cliente SIN kwargs (evita errores de 'proxies' u otros)
    client = OpenAI()

    mem = _get_mem(contact) or {"messages": [], "greeted": False}
    messages = mem.get("messages", [])
    greeted = bool(mem.get("greeted", False))

    # 🔹 Interceptor de saludo "puro" para presentación única
    if not greeted and _is_pure_greeting(user_text):
        greeting_text = _build_greeting()
        if not any(m.get("role") == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})
        messages.append({"role": "user", "content": user_text})
        messages.append({"role": "assistant", "content": greeting_text})
        _save_mem(contact, messages, greeted=True)
        return greeting_text

    # Inyectar prompt del sistema si hace falta
    if not any(m.get("role") == "system" for m in messages):
        messages.insert(0, {"role": "system", "content": SYSTEM_PROMPT})

    # Pre-normaliza fecha relativa del lado servidor (sin tool_calls)
    today_iso = _now_local().date().isoformat()
    date_hint = _server_normalize_date_hint(user_text, today_iso)
    user_payload = user_text
    if date_hint:
        user_payload = f"{user_text}\n\n[HINT_FECHA:{date_hint}]"
        # 🔹 guarda el hint para forzarlo en tool-calls
        _LAST_DATE_HINT[contact] = date_hint

    # Nuevo mensaje del usuario (posible payload con HINT_FECHA)
    messages.append({"role": "user", "content": user_payload})

    max_tool_hops = 8
    for _ in range(max_tool_hops):
        try:
            resp = client.chat.completions.create(
                model=getattr(settings, "OPENAI_AGENT_MODEL", "gpt-4o-mini"),
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.2,
                timeout=20,  # evita timeouts del webhook
            )
        except Exception as e:
            logger.exception("OpenAI falló: %s", e)
            return "Tuve un problema con el servicio de IA. ¿Desea que lo intente de nuevo o prefiere hablar con recepción?"

        msg = resp.choices[0].message

        tool_calls = getattr(msg, "tool_calls", None) or []
        if tool_calls:
            messages.append({
                "role": "assistant",
                "tool_calls": tool_calls,
                "content": msg.content or ""
            })
            for call in tool_calls:
                name = call.function.name
                args = _coerce_json(call.function.arguments)

                # Autorrellenos útiles previos a ejecutar la tool
                if name in ("book_appointment", "reschedule_appointment"):
                    # Normaliza hora si viene "7 pm"
                    if args.get("time_hhmm") and re.search(r"[ap]m\b", str(args["time_hhmm"]).lower()):
                        norm = hhmm_from_text_or_none(args["time_hhmm"])
                        if norm:
                            args["time_hhmm"] = norm

                    # Fuerza el último HINT_FECHA si lo tenemos:
                    if _LAST_DATE_HINT.get(contact):
                        args["date_iso"] = _LAST_DATE_HINT[contact]

                    if name == "book_appointment":
                        args.setdefault("channel", "whatsapp")
                        args.setdefault("client_request_id", f"{contact}-{uuid.uuid4().hex[:8]}")
                    if name == "reschedule_appointment":
                        args.setdefault("client_request_id", f"{contact}-{uuid.uuid4().hex[:8]}")

                # Ejecuta tool y captura resultado
                try:
                    result = _dispatch_tool(contact, name, args)
                except Exception as e:
                    logger.exception("Tool %s lanzó excepción: %s", name, e)
                    result = {"ok": False, "error": f"tool_exception:{name}"}

                # Si se concretó agendar o reagendar → limpia el hint
                if name in ("book_appointment", "reschedule_appointment") and isinstance(result, dict) and result.get("ok"):
                    _LAST_DATE_HINT.pop(contact, None)

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": name,
                    "content": json.dumps(result, ensure_ascii=False)
                })
            continue  # deja que el modelo procese los resultados

        # Respuesta final del modelo (sin tools)
        final_text = (msg.content or "").strip()
        if not final_text:
            final_text = "Por ahora no pude completar la acción. ¿Desea que intentemos nuevamente o prefiere hablar con recepción?"

        # Normalizaciones menores de UX
        try:
            final_text = re.sub(r"\s*\|\s*", " ", final_text)
            final_text = re.sub(r"\s{2,}", " ", final_text).strip()
            final_text = re.sub(r"(·\s*){2,}", "· ", final_text)
        except Exception:
            pass

        messages.append({"role": "assistant", "content": final_text})
        _save_mem(contact, messages, greeted=True)
        return final_text

    _save_mem(contact, messages, greeted=True)
    return "Tuve un problema para cerrar la operación. ¿Desea que lo intente de nuevo o prefiere hablar con recepción?"