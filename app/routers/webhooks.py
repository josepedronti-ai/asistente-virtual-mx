# app/routers/webhooks.py
from __future__ import annotations
from fastapi import APIRouter, Form
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session
from dateutil import parser as dtparser
from dateparser import parse as dp_parse
import unicodedata, re
from datetime import datetime, timedelta, date

from ..database import SessionLocal
from ..config import settings
from .. import models
from ..services.notifications import send_text
from ..services.scheduling import (
    available_slots,
    create_event,
    update_event,
    delete_event,
)
from ..services.nlu import analizar
from ..replygen.core import generate_reply
from ..replygen.llm import polish_spanish_mx as polish  # pulido humano si hay OPENAI_API_KEY

# =========================
# Memoria corta (contexto)
# =========================
SESSION_CTX: dict[str, dict] = {}
CTX_TTL_MIN = 15  # minutos

def _now_utc():
    return datetime.utcnow()

def _set_state(contact: str, state: str, **kv):
    ctx = SESSION_CTX.get(contact) or {}
    ctx.update(kv)
    ctx["state"] = state
    ctx["ts"] = _now_utc()
    SESSION_CTX[contact] = ctx
    print(f"[STATE] {contact} -> {state} | { {k:v for k,v in kv.items() if k!='ts'} }")

def _get_ctx(contact: str):
    ctx = SESSION_CTX.get(contact)
    if not ctx:
        return None
    if (_now_utc() - ctx.get("ts", _now_utc())) > timedelta(minutes=CTX_TTL_MIN):
        SESSION_CTX.pop(contact, None)
        return None
    return ctx

def _clear_ctx(contact: str, *keys):
    ctx = SESSION_CTX.get(contact) or {}
    for k in keys:
        ctx.pop(k, None)
    ctx["ts"] = _now_utc()
    SESSION_CTX[contact] = ctx

# ----------------------------
# Utilidades de texto y hora
# ----------------------------
def normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s

def _clean_person_name(raw: str) -> str:
    """Normaliza un nombre propio (letras/espacios/guiones) y lo deja en Title Case."""
    s = unicodedata.normalize("NFKD", raw or "")
    s = "".join(ch for ch in s if unicodedata.category(ch).startswith("L") or ch in (" ", "-", "’", "'"))
    s = re.sub(r"\s+", " ", s).strip()
    return s.title()

# Sí / No humanos
_YES_PAT = re.compile(
    r"(?:^|\b)(?:s[ií]|sí por favor|si por favor|claro(?: que s[ií])?|correcto|ok(?:ay)?|vale|de acuerdo|afirmativo|me parece|está bien|esta bien|perfecto|va|sale|adelante|funciona|me late|confirmo|confirmar|agendar|queda|listo)(?:\b|$)"
)
_NO_PAT = re.compile(
    r"(?:^|\b)(?:no|no gracias|mejor no|no es correcto|no es asi|no es así|prefiero cambiar|otra fecha|cambiar fecha|cancela|cancelar|no puedo|no me queda|no me funciona)(?:\b|$)"
)

def is_yes(s: str) -> bool:
    t = (s or "").strip().lower()
    t = unicodedata.normalize("NFD", t)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    return bool(_YES_PAT.search(t))

def is_no(s: str) -> bool:
    t = (s or "").strip().lower()
    t = unicodedata.normalize("NFD", t)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    return bool(_NO_PAT.search(t))

def parse_time_hint(text: str):
    """
    Extrae hora explícita de expresiones comunes:
      - "20:00", "20.00", "20 00"
      - "8 pm", "8pm", "8:00pm", "8.00 pm"
      - "20 h", "20 hrs", "20 horas"
      - "ocho de la noche/tarde/mañana/madrugada"
      - "mediodía", "medianoche"
    Devuelve (hour, minute) o None.
    """
    t = (text or "").strip().lower()
    # normaliza diacríticos y espacios raros
    t_norm = unicodedata.normalize("NFD", t)
    t_norm = "".join(ch for ch in t_norm if unicodedata.category(ch) != "Mn")
    t_norm = re.sub(r"\s+", " ", t_norm)

    # atajos especiales
    if re.search(r"\bmediodia\b", t_norm):
        return 12, 0
    if re.search(r"\bmedianoche\b", t_norm):
        return 0, 0

    # 1) hh:mm (opcional am/pm) — permite ":" "." o espacio como separador
    m = re.search(r"\b([01]?\d|2[0-3])\s*[:\. ]\s*([0-5]\d)\s*(am|pm)?\b", t_norm)
    if m:
        h = int(m.group(1)); mnt = int(m.group(2)); ampm = (m.group(3) or "").lower()
        if ampm == "pm" and h != 12: h += 12
        if ampm == "am" and h == 12: h = 0
        return h, mnt

    # 2) h am/pm (sin minutos)
    m = re.search(r"\b([1-9]|1[0-2])\s*(am|pm)\b", t_norm)
    if m:
        h = int(m.group(1)); ampm = m.group(2)
        if ampm == "pm" and h != 12: h += 12
        if ampm == "am" and h == 12: h = 0
        return h, 0

    # 3) h[.:]mm con am/pm pegado (ej. 8:00pm, 8.00pm)
    m = re.search(r"\b([1-9]|1[0-2])\s*[:\.]\s*([0-5]\d)\s*(?:pm|am)\b", t_norm)
    if m:
        h = int(m.group(1)); mnt = int(m.group(2))
        ampm = "pm" if "pm" in t_norm[m.end()-2:m.end()] else "am"
        if ampm == "pm" and h != 12: h += 12
        if ampm == "am" and h == 12: h = 0
        return h, mnt

    # 4) solo hora 0-23 con sufijos (h/hrs/horas)
    m = re.search(r"\b(0?\d|1\d|2[0-3])\s*(?:h|hrs|horas?)\b", t_norm)
    if m:
        return int(m.group(1)), 0

    # 5) solo hora 0-23 "limpia" (ej. "a las 20", "20")
    m = re.search(r"\b(?:a las\s+)?(0?\d|1\d|2[0-3])\b", t_norm)
    if m:
        return int(m.group(1)), 0

    # 6) hora en palabras + momento del día (ocho de la tarde/noche/mañana/madrugada)
    palabras = {
        "una":1, "uno":1,
        "dos":2, "tres":3, "cuatro":4, "cinco":5, "seis":6,
        "siete":7, "ocho":8, "nueve":9, "diez":10, "once":11, "doce":12
    }
    m = re.search(r"\b(una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)\b(?:.*?\bde la\b\s+(manana|tarde|noche|madrugada))?", t_norm)
    if m:
        h = palabras[m.group(1)]
        franja = m.group(2) or ""
        # interpreta franja
        if franja == "manana":
            if h == 12: h = 0
        elif franja in ("tarde", "noche"):
            if h != 12: h += 12
        elif franja == "madrugada":
            if h == 12: h = 0  # 12 de la madrugada -> 00:00
            # 1-5 se quedan tal cual (01-05)
        # sin franja: dejamos 1-12 tal cual (ambigua, pero el FSM luego valida contra disponibilidad)
        return h, 0

    # 7) “y media” / “y cuarto” / “menos cuarto” (opcionales) — versión simple
    #    Si el usuario pone “ocho y media (de la tarde)”
    m = re.search(
        r"\b(una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)\s+y\s+(media|cuarto)\b(?:.*?\bde la\b\s+(manana|tarde|noche|madrugada))?",
        t_norm
    )
    if m:
        h = palabras[m.group(1)]
        parte = m.group(2)
        franja = m.group(3) or ""
        minutes = 30 if parte == "media" else 15
        if franja == "manana":
            if h == 12: h = 0
        elif franja in ("tarde", "noche"):
            if h != 12: h += 12
        elif franja == "madrugada":
            if h == 12: h = 0
        return h, minutes

    m = re.search(
        r"\b(una|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)\s+menos\s+cuarto\b(?:.*?\bde la\b\s+(manana|tarde|noche|madrugada))?",
        t_norm
    )
    if m:
        h = palabras[m.group(1)]
        franja = m.group(2) or ""
        # menos cuarto => xx:45 de la hora anterior
        h = (h - 1) if h > 1 else 12
        if franja == "manana":
            if h == 12: h = 0
        elif franja in ("tarde", "noche"):
            if h != 12: h += 12
        elif franja == "madrugada":
            if h == 12: h = 0
        return h, 45

    return None

def human_slot_strings(slots, limit=12, balanced=True):
    """
    Devuelve hasta 'limit' horarios distribuidos y solo **horas** (HH:MM),
    ya que la plantilla ya incluye la fecha.
    """
    if not slots:
        return []
    selected = slots
    if balanced and len(slots) > limit:
        step = max(1, len(slots) // limit)
        selected = [slots[i] for i in range(0, len(slots), step)][:limit]
    return [s.strftime("%H:%M") for s in selected]

# --- Fechas: parser natural con fallback a extractor propio ---
def extract_spanish_date(text: str, today: date) -> date | None:
    if not text:
        return None
    t_raw = (text or "").strip().lower()
    t = unicodedata.normalize("NFD", t_raw)
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")

    if "pasado manana" in t:
        return today + timedelta(days=2)
    if "manana" in t:
        return today + timedelta(days=1)
    if "hoy" in t:
        return today

    _WEEK_MAP = {
        "lunes": 0, "martes": 1, "miercoles": 2,
        "jueves": 3, "viernes": 4, "sabado": 5, "domingo": 6
    }
    for w, idx in _WEEK_MAP.items():
        if re.search(rf"\b{w}\b", t):
            delta = (idx - today.weekday()) % 7
            if delta == 0:
                delta = 7
            return today + timedelta(days=delta)

    m = re.search(r"\b([0-3]?\d)[/\-\.]([01]?\d)[/\-\.](\d{4})\b", t)
    if m:
        d_, mth, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mth, d_)
        except ValueError:
            return None

    meses = {
        "enero":1, "febrero":2, "marzo":3, "abril":4, "mayo":5, "junio":6,
        "julio":7, "agosto":8, "septiembre":9, "setiembre":9,
        "octubre":10, "noviembre":11, "diciembre":12
    }
    m2 = re.search(r"\b([0-3]?\d)\s*(?:de\s+)?([a-z]+)\s*(?:de\s+)?(\d{4})\b", t)
    if m2:
        d__ = int(m2.group(1)); mes_txt = m2.group(2); y = int(m2.group(3))
        if mes_txt in meses:
            try:
                return date(y, meses[mes_txt], d__)
            except ValueError:
                return None

    try:
        dt = dtparser.parse(t_raw, dayfirst=True, fuzzy=True)
        return dt.date()
    except Exception:
        return None

def parse_natural_date(text: str, today: date) -> date | None:
    try:
        dt = dp_parse(
            text or "",
            languages=["es"],
            settings={
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": datetime.combine(today, datetime.min.time()),
                "DATE_ORDER": "DMY",
            },
        )
        if dt:
            return dt.date()
    except Exception:
        pass
    return extract_spanish_date(text, today)

# ----------------------------
# DB helpers
# ----------------------------
def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_patient_by_contact(db: Session, contact: str) -> models.Patient | None:
    return db.query(models.Patient).filter(models.Patient.contact == contact).first()

def get_or_create_patient(db: Session, contact: str) -> models.Patient:
    p = get_patient_by_contact(db, contact)
    if p:
        return p
    p = models.Patient(contact=contact)  # nombre se pedirá después
    db.add(p)
    db.commit()
    db.refresh(p)
    return p

def find_latest_active_for_contact(db: Session, contact: str):
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

def move_or_create_appointment(db: Session, patient: models.Patient, start_dt: datetime) -> models.Appointment:
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

router = APIRouter(prefix="", tags=["webhooks"])

# ----------------------------
# Webhook principal (FSM primero, NLU después)
# ----------------------------
@router.post("/webhooks/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(From: str = Form(None), Body: str = Form(None)) -> str:
    if not From:
        return ""
    raw_text = Body or ""
    text = normalize(raw_text)
    print(f"[WHATSAPP IN] from={From} body={raw_text}")

    # Saludo amplio
    if any(k in text for k in ("hola","buenos dias","buenas tardes","buenas noches","menu","menú")):
        send_text(From, polish(generate_reply("greet", {"now": datetime.now()})))
        _set_state(From, "idle")
        return ""

    # =======================
    # Máquina de estados (PRIORIDAD)
    # =======================
    ctx = _get_ctx(From) or {"state": "idle"}
    state = ctx.get("state", "idle")
    print(f"[STATE BEFORE] {From} -> {state} | {ctx}")

    # --------- AWAIT_NAME: guardar nombre y AGENDAR en Calendar ---------
    if state == "await_name":
        cleaned = _clean_person_name(raw_text)
        if len(cleaned) < 3:
            send_text(From, polish(generate_reply("need_name", {})))
            return ""
        last_date = ctx.get("last_date")
        last_time = ctx.get("last_time")  # (h, m)
        if not last_date or not last_time:
            send_text(From, polish(generate_reply("ask_date_strict", {})))
            _set_state(From, "await_date")
            return ""
        h, m = last_time
        start_dt = datetime.combine(last_date, datetime.min.time()).replace(hour=h, minute=m)
        for db in db_session():
            patient = get_or_create_patient(db, From)
            patient.name = cleaned
            db.commit()
            appt = move_or_create_appointment(db, patient, start_dt)
            appt.status = models.AppointmentStatus.confirmed
            if not appt.event_id:
                try:
                    ev_id = create_event(
                        summary=f"Consulta — {patient.name or 'Paciente'}",
                        start_local=appt.start_at,
                        duration_min=getattr(settings, "EVENT_DURATION_MIN", 30),
                        location="CLIEMED, Av. Prof. Moisés Sáenz 1500, Monterrey, N.L.",
                        description=f"Canal: WhatsApp\nPaciente: {patient.name or patient.contact}"
                    )
                    appt.event_id = ev_id
                except Exception:
                    pass
            db.commit()
            send_text(From, polish(generate_reply("confirm_done", {"appt_dt": appt.start_at, "patient_name": patient.name})))
        _set_state(From, "idle")
        return ""

    # --------- AWAIT_CONFIRM: sí/no de confirmación ---------
    if state == "await_confirm":
        if is_yes(raw_text) or text in ("agendar","agendar cita","confirmar","confirmo","ok"):
            # Confirmó → ahora pedimos nombre
            send_text(From, polish(generate_reply("need_name", {})))
            _set_state(From, "await_name")
            return ""
        if is_no(raw_text):
            send_text(From, polish(generate_reply("ask_date_strict", {})))
            _set_state(From, "await_date")
            return ""
        # Re-preguntar confirmación con eco de fecha y hora
        last_date = ctx.get("last_date")
        last_time = ctx.get("last_time")
        if last_date and last_time:
            h, m = last_time
            dummy_dt = datetime.combine(last_date, datetime.min.time()).replace(hour=h, minute=m)
            send_text(From, polish(generate_reply("confirm_q", {"appt_dt": dummy_dt})))
        return ""

    # --------- AWAIT_TIME: esperar hora ---------
    if state == "await_time":
        time_hint = parse_time_hint(raw_text)
        if not time_hint:
            send_text(
                From,
                polish(
                    "Gracias. Para continuar, ¿me indica la hora que prefiere? "
                    "Puede escribirla como 16:00, 4 pm u “ocho de la tarde”."
                ),
            )
            return ""

        target_h, target_m = time_hint
        parsed_date = ctx.get("last_date")
        if not parsed_date:
            send_text(From, polish(generate_reply("ask_date_strict", {})))
            _set_state(From, "await_date")
            return ""

        for db in db_session():
            slots = available_slots(db, parsed_date, settings.TIMEZONE)
            if not slots:
                send_text(
                    From,
                    polish(
                        generate_reply(
                            "day_full",
                            {"date_dt": datetime.combine(parsed_date, datetime.min.time())},
                        )
                    ),
                )
                _set_state(From, "await_date")
                break

            match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
            if match:
                # Pedimos confirmación (no calendar aún) con fallback si falta la plantilla
                try:
                    send_text(From, polish(generate_reply("confirm_q", {"appt_dt": match})))
                except Exception:
                    when = f"{match.strftime('%d/%m/%Y')} a las {match.strftime('%H:%M')}"
                    send_text(From, polish(f"Para confirmar, sería el {when}. ¿Es correcto?"))

                _set_state(From, "await_confirm", last_date=parsed_date, last_time=(target_h, target_m))
            else:
                alts = human_slot_strings(slots, limit=12, balanced=False)
                send_text(
                    From,
                    polish(
                        generate_reply(
                            "time_unavailable",
                            {"date_dt": datetime.combine(parsed_date, datetime.min.time()), "slots_list": alts},
                        )
                    ),
                )
        return ""

    # --------- AWAIT_DATE: esperar fecha ---------
    if state == "await_date":
        today = datetime.now().date()
        parsed_date = parse_natural_date(raw_text, today)
        if not parsed_date:
            # tono+
            send_text(From, polish("Gracias. Para continuar, compártame la fecha en formato Día/Mes/Año (ej. 18/08/2025)."))
            return ""
        for db in db_session():
            slots = available_slots(db, parsed_date, settings.TIMEZONE)
            if not slots:
                send_text(From, polish(generate_reply("day_full", {"date_dt": datetime.combine(parsed_date, datetime.min.time())})))
                break
            alts = human_slot_strings(slots, limit=12, balanced=True)
            send_text(From, polish(generate_reply(
                "list_slots_for_date",
                {"date_dt": datetime.combine(parsed_date, datetime.min.time()), "slots_list": alts}
            )))
        _set_state(From, "await_time", last_date=parsed_date)
        return ""

    # --------- IDLE: iniciar booking si el usuario lo pide ---------
    if state == "idle":
        # Si llega algo que parece fecha espontánea, encaminar
        today = datetime.now().date()
        parsed_date = parse_natural_date(raw_text, today)
        if parsed_date:
            for db in db_session():
                slots = available_slots(db, parsed_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, polish(generate_reply("day_full", {"date_dt": datetime.combine(parsed_date, datetime.min.time())})))
                    break
                alts = human_slot_strings(slots, limit=12, balanced=True)
                send_text(From, polish(generate_reply(
                    "list_slots_for_date",
                    {"date_dt": datetime.combine(parsed_date, datetime.min.time()), "slots_list": alts}
                )))
            _set_state(From, "await_time", last_date=parsed_date)
            return ""
        # Si no, seguimos con NLU (solo en idle)

    # =======================
    # NLU (solo si no hay estado activo distinto de idle)
    # =======================
    nlu = analizar(raw_text)
    intent = nlu.get("intent", "fallback")
    entities = nlu.get("entities", {}) or {}
    reply = nlu.get("reply", "")

    print(f"[NLU] from={From} intent={intent} entities={entities} text={(raw_text)[:120]}")

    # Cortesía / despedidas
    if text in ("no","no gracias","gracias","listo","es todo","ninguno","ninguna"):
        send_text(From, polish(generate_reply("goodbye", {})))
        _set_state(From, "idle")
        return ""

    # Info (precios / ubicación) — permitido en idle
    if intent == "info" and state == "idle":
        topic = entities.get("topic") or ""
        if topic in ("costos","costo","precio","precios"):
            send_text(From, polish(generate_reply("prices", {})))
            return ""
        if topic in ("ubicacion","ubicación","direccion","dirección"):
            send_text(From, polish(generate_reply("location", {})))
            return ""
        send_text(From, polish(reply or "¿Desea costos o ubicación?"))
        return ""

    # Cancelar cita — permitido siempre
    if intent == "cancel":
        for db in db_session():
            appt = find_latest_active_for_contact(db, From)
            if not appt:
                send_text(From, polish("No encuentro una cita activa. Si gusta, puedo ayudarle a agendar una nueva."))
                break
            appt.status = models.AppointmentStatus.canceled
            if appt.event_id:
                try:
                    delete_event(appt.event_id)
                except Exception:
                    pass
                appt.event_id = None
            db.commit()
            send_text(From, polish(generate_reply("canceled_ok", {})))
        _set_state(From, "idle")
        return ""

    # Book / Reschedule (en idle) → pedir fecha estricta
    if intent in ("book","reschedule") and state == "idle":
        send_text(From, polish(generate_reply("ask_date_strict", {})))
        _set_state(From, "await_date")
        return ""

    # Fallback final
    send_text(From, polish(generate_reply("fallback", {})))
    return ""