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
CTX_TTL_MIN = 15

def set_ctx(contact: str, last_date, **extra):
    SESSION_CTX[contact] = {
        "last_date": last_date,  # date
        "ts": datetime.utcnow(),
        **extra
    }

def update_ctx(contact: str, **kv):
    ctx = SESSION_CTX.get(contact) or {}
    ctx.update(kv)
    ctx["ts"] = datetime.utcnow()
    SESSION_CTX[contact] = ctx

def clear_ctx_flags(contact: str, *flags):
    ctx = SESSION_CTX.get(contact) or {}
    for f in flags:
        ctx.pop(f, None)
    ctx["ts"] = datetime.utcnow()
    SESSION_CTX[contact] = ctx

def get_ctx(contact: str):
    d = SESSION_CTX.get(contact)
    if not d:
        return None
    if (datetime.utcnow() - d["ts"]) > timedelta(minutes=CTX_TTL_MIN):
        SESSION_CTX.pop(contact, None)
        return None
    return d


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
_YES_PAT = re.compile(r"\b(s[ií]|claro|correcto|ok|vale|de acuerdo|afirmativo|me parece|est(a|á) bien|perfecto)\b")
_NO_PAT  = re.compile(r"\b(no|prefiero cambiar|otra fecha|cambiar fecha|no gracias|mejor no)\b")
def is_yes(s: str) -> bool:
    t = (s or "").lower()
    return bool(_YES_PAT.search(t)) or t.strip().startswith(("si", "sí", "ok"))
def is_no(s: str) -> bool:
    t = (s or "").lower()
    return bool(_NO_PAT.search(t)) or t.strip().startswith("no")

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

def find_latest_active_for_contact(db: Session, contact: str):
    """Activa = reservada o confirmada."""
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

def get_or_create_patient(db: Session, contact: str) -> models.Patient:
    p = get_patient_by_contact(db, contact)
    if p:
        return p
    p = models.Patient(contact=contact)  # nombre se pedirá después
    db.add(p)
    db.commit()
    db.refresh(p)
    return p

def move_or_create_appointment(db: Session, patient: models.Patient, start_dt: datetime) -> models.Appointment:
    """
    - Si hay cita ACTIVA (reservada/confirmada), la mueve a start_dt.
    - Si estaba confirmada:
        - con event_id → update_event
        - sin event_id → create_event
    - Si no hay activa, crea una nueva RESERVADA.
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
        appt.start_at = start_dt
        if appt.status == models.AppointmentStatus.confirmed:
            if appt.event_id:
                try:
                    update_event(appt.event_id, start_dt, duration_min=getattr(settings, "EVENT_DURATION_MIN", 30))
                except Exception:
                    ev_id = create_event(
                        summary=f"Consulta — {patient.name or 'Paciente'}",
                        start_local=start_dt,
                        duration_min=getattr(settings, "EVENT_DURATION_MIN", 30),
                        location="CLIEMED, Av. Prof. Moisés Sáenz 1500, Monterrey, N.L.",
                        description=f"Canal: WhatsApp\nPaciente: {patient.name or patient.contact}"
                    )
                    appt.event_id = ev_id
            else:
                ev_id = create_event(
                    summary=f"Consulta — {patient.name or 'Paciente'}",
                    start_local=start_dt,
                    duration_min=getattr(settings, "EVENT_DURATION_MIN", 30),
                    location="CLIEMED, Av. Prof. Moisés Sáenz 1500, Monterrey, N.L.",
                    description=f"Canal: WhatsApp\nPaciente: {patient.name or patient.contact}"
                )
                appt.event_id = ev_id
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
    # hh:mm (acepta "4:00 pm")
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\s*(am|pm)?\b", t)
    if m:
        h = int(m.group(1)); mnt = int(m.group(2)); ampm = (m.group(3) or "").lower()
        if ampm == "pm" and h != 12: h += 12
        if ampm == "am" and h == 12: h = 0
        return h, mnt
    # h am/pm
    m = re.search(r"\b([1-9]|1[0-2])\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1)); ampm = m.group(2)
        if ampm == "pm" and h != 12: h += 12
        if ampm == "am" and h == 12: h = 0
        return h, 0
    # hh (24h)
    m = re.search(r"\b(0?\d|1\d|2[0-3])\s*h?\b", t)
    if m:
        return int(m.group(1)), 0
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
    """
    Fallback simple:
      - "hoy", "mañana", "pasado mañana"
      - días de la semana → próxima ocurrencia
      - dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy
      - "dd mes yyyy" o "dd de mes de yyyy" (mes en español)
    """
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
    """
    Intenta entender fechas naturales en español con dateparser.
    Prefiere fechas futuras; si falla, usa el extractor fallback.
    """
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
    print(f"[WHATSAPP IN] from={From} body={raw_text}")
    print(f"[CTX BEFORE] {SESSION_CTX.get(From)}")

    # === 0) ¿YA ESTÁBAMOS ESPERANDO EL NOMBRE?  (manejar ANTES que nada) ===
    ctx = get_ctx(From) or {}
    if ctx.get("await_name"):
        cleaned = _clean_person_name(raw_text)
        if len(cleaned) >= 3:
            for db in db_session():
                patient = get_patient_by_contact(db, From) or get_or_create_patient(db, From)
                patient.name = cleaned
                db.commit()
                appt = find_latest_reserved_for_contact(db, From)
                clear_ctx_flags(From, "await_name")
                # Si había una reserva pendiente, pregunta confirmación;
                # si no, agradece y ofrece agendar.
                if appt:
                    try:
                        send_text(From, polish(generate_reply("confirm_q", {"appt_dt": appt.start_at})))
                    except Exception:
                        when = f"{appt.start_at.strftime('%d/%m/%Y')} a las {appt.start_at.strftime('%H:%M')}"
                        send_text(From, polish(f"Para confirmar, sería el {when}. ¿Es correcto?"))
                else:
                    send_text(From, polish(f"Gracias, {cleaned}. ¿Desea agendar una cita?"))
            print(f"[CTX AFTER NAME] {SESSION_CTX.get(From)}")
            return ""
        # Nombre demasiado corto o inválido → vuelve a pedirlo
        send_text(From, polish(generate_reply("need_name", {})))
        return ""

    # === 0.1) Si hay RESERVA y falta nombre → pedirlo (y marcar flag) ===
    for db in db_session():
        patient = get_patient_by_contact(db, From)
        pending = find_latest_reserved_for_contact(db, From) if patient else None
        if patient and pending and (patient.name is None or not patient.name.strip()):
            update_ctx(From, await_name=True)
            send_text(From, polish(generate_reply("need_name", {})))
            print(f"[CTX ASK NAME] {SESSION_CTX.get(From)}")
            return ""

    # 1) Saludo tolerante (aunque vengan más palabras)
    if any(k in text for k in ("hola", "buenos dias", "buenas tardes", "buenas noches", "menu", "menú")):
        msg = generate_reply("greet", {"now": datetime.now()})
        send_text(From, polish(msg))
        return ""

    # Atajo: usuario manda sólo la hora
    explicit_time_pre = parse_time_hint(raw_text)
    ctx = get_ctx(From) or {}
    awaiting_keep = ctx.get("await_keep_date", False)
    awaiting_new_date = ctx.get("await_new_date", False)

    # ===== Reprogramar: mantener misma fecha (sí/no) =====
    if awaiting_keep:
        if is_yes(raw_text):
            keep_date = ctx.get("pending_date")
            pending_time = ctx.get("pending_time")  # (h,m) o None
            clear_ctx_flags(From, "await_keep_date")
            if not keep_date:
                send_text(From, polish(generate_reply("ask_date_strict", {})))
                return ""
            if not pending_time:
                update_ctx(From, last_date=keep_date)
                for db in db_session():
                    slots = available_slots(db, keep_date, settings.TIMEZONE)
                    if not slots:
                        send_text(From, polish(generate_reply("day_full", {"date_dt": datetime.combine(keep_date, datetime.min.time())})))
                        break
                    alts = human_slot_strings(slots, limit=12, balanced=True)
                    send_text(From, polish(generate_reply("list_slots_for_date", {
                        "date_dt": datetime.combine(keep_date, datetime.min.time()),
                        "slots_list": alts
                    })))
                return ""
            # ya tenemos fecha+hora
            target_h, target_m = pending_time
            for db in db_session():
                slots = available_slots(db, keep_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, polish(generate_reply("day_full", {"date_dt": datetime.combine(keep_date, datetime.min.time())})))
                    break
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = move_or_create_appointment(db, patient, match)
                    SESSION_CTX.pop(From, None)
                    send_text(From, polish(generate_reply("reserved_ok", {"appt_dt": appt.start_at})))
                    if not (patient.name and patient.name.strip()):
                        update_ctx(From, await_name=True)
                        send_text(From, polish(generate_reply("need_name", {})))
                else:
                    alts = human_slot_strings(slots, limit=12, balanced=False)
                    set_ctx(From, keep_date)
                    send_text(From, polish(generate_reply(
                        "time_unavailable",
                        {"date_dt": datetime.combine(keep_date, datetime.min.time()), "slots_list": alts}
                    )))
            return ""
        if is_no(raw_text):
            clear_ctx_flags(From, "await_keep_date")
            update_ctx(From, await_new_date=True)
            send_text(From, polish(generate_reply("ask_date_strict", {})))
            return ""

    # ===== Reprogramar: esperando nueva fecha =====
    if awaiting_new_date:
        today_local = datetime.now().date()
        parsed_new_date = parse_natural_date(raw_text, today_local)
        if not parsed_new_date:
            send_text(From, polish(generate_reply("ask_date_strict", {})))
            return ""
        pending_time = ctx.get("pending_time")
        if pending_time:
            target_h, target_m = pending_time
            for db in db_session():
                slots = available_slots(db, parsed_new_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, polish(generate_reply("day_full", {"date_dt": datetime.combine(parsed_new_date, datetime.min.time())})))
                    break
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = move_or_create_appointment(db, patient, match)
                    clear_ctx_flags(From, "await_new_date", "pending_time")
                    SESSION_CTX[From]["last_date"] = parsed_new_date
                    send_text(From, polish(generate_reply("reserved_ok", {"appt_dt": appt.start_at})))
                    if not (patient.name and patient.name.strip()):
                        update_ctx(From, await_name=True)
                        send_text(From, polish(generate_reply("need_name", {})))
                else:
                    alts = human_slot_strings(slots, limit=12, balanced=False)
                    set_ctx(From, parsed_new_date)
                    clear_ctx_flags(From, "await_new_date")
                    send_text(From, polish(generate_reply(
                        "time_unavailable",
                        {"date_dt": datetime.combine(parsed_new_date, datetime.min.time()), "slots_list": alts}
                    )))
            return ""
        # sin hora → lista de horarios
        for db in db_session():
            slots = available_slots(db, parsed_new_date, settings.TIMEZONE)
            if not slots:
                send_text(From, polish(generate_reply("day_full", {"date_dt": datetime.combine(parsed_new_date, datetime.min.time())})))
                break
            alts = human_slot_strings(slots, limit=12, balanced=True)
            set_ctx(From, parsed_new_date)
            clear_ctx_flags(From, "await_new_date")
            send_text(From, polish(generate_reply(
                "list_slots_for_date",
                {"date_dt": datetime.combine(parsed_new_date, datetime.min.time()), "slots_list": alts}
            )))
        return ""

    # 2) NLU
    nlu = analizar(raw_text)
    intent = nlu.get("intent", "fallback")
    entities = nlu.get("entities", {}) or {}
    reply = nlu.get("reply", "")
    print(f"[NLU] from={From} intent={intent} entities={entities} text={(raw_text)[:120]}")

    nlu_date = entities.get("date") or ""
    topic = entities.get("topic") or ""

    # Despedidas cortas
    if text in ("no", "no gracias", "gracias", "listo", "es todo", "ninguno", "ninguna"):
        send_text(From, polish(generate_reply("goodbye", {})))
        return ""

    # Info (precios/ubicación)
    if intent == "info" and not explicit_time_pre:
        if topic in ("costos", "costo", "precio", "precios"):
            send_text(From, polish(generate_reply("prices", {}))); return ""
        if topic in ("ubicacion", "ubicación", "direccion", "dirección"):
            send_text(From, polish(generate_reply("location", {}))); return ""
        send_text(From, polish(reply or "¿Desea costos o ubicación?")); return ""

    # Confirmar
    if intent == "confirm" and not explicit_time_pre:
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt:
                send_text(From, polish("Para confirmar necesito un horario reservado. ¿Desea que le proponga horarios?"))
                break
            patient = get_patient_by_contact(db, From)
            if patient and (not patient.name or not patient.name.strip()):
                update_ctx(From, await_name=True)
                send_text(From, polish(generate_reply("need_name", {})))
                break
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
            msg = generate_reply("confirm_done", {"appt_dt": appt.start_at, "patient_name": (patient.name if patient else "")})
            send_text(From, polish(msg))
        return ""

    # Cancelar
    if intent == "cancel" and not explicit_time_pre:
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
        return ""

    # Agendar / Reprogramar
    if intent in ("book", "reschedule") or explicit_time_pre:
        today_local = datetime.now().date()
        parsed_date = parse_natural_date(entities.get("date") or raw_text, today_local)
        explicit_time = parse_time_hint(raw_text)

        # Solo hora sin fecha → preguntar si mantener fecha actual
        if (intent in ("reschedule", "book")) and explicit_time and not parsed_date:
            for db in db_session():
                appt = find_latest_active_for_contact(db, From)
                if appt:
                    appt_date = appt.start_at.date()
                    update_ctx(From, await_keep_date=True, pending_date=appt_date, pending_time=explicit_time)
                    msg = generate_reply("keep_same_date_q", {"date_dt": datetime.combine(appt_date, datetime.min.time())})
                    send_text(From, polish(msg)); return ""
            send_text(From, polish(generate_reply("ask_date_strict", {}))); return ""

        # Si hay hora y tenemos fecha en contexto → usarla
        ctx = get_ctx(From) or {}
        if not parsed_date and explicit_time and ctx.get("last_date"):
            parsed_date = ctx["last_date"]

        # Fecha sí, hora no → lista y pide hora
        if parsed_date and not explicit_time:
            for db in db_session():
                slots = available_slots(db, parsed_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, polish(generate_reply("day_full", {"date_dt": datetime.combine(parsed_date, datetime.min.time())})))
                    break
                alts = human_slot_strings(slots, limit=12, balanced=True)
                set_ctx(From, parsed_date)
                send_text(From, polish(generate_reply(
                    "list_slots_for_date",
                    {"date_dt": datetime.combine(parsed_date, datetime.min.time()), "slots_list": alts}
                )))
            return ""

        # Hora sí, fecha no → pedir fecha (estricto)
        if explicit_time and not parsed_date:
            send_text(From, polish(generate_reply("ask_date_strict", {}))); return ""

        # Fecha y hora → reservar
        if parsed_date and explicit_time:
            target_h, target_m = explicit_time
            for db in db_session():
                slots = available_slots(db, parsed_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, polish(generate_reply("day_full", {"date_dt": datetime.combine(parsed_date, datetime.min.time())}))); break
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = move_or_create_appointment(db, patient, match)
                    SESSION_CTX.pop(From, None)
                    send_text(From, polish(generate_reply("reserved_ok", {"appt_dt": appt.start_at})))
                    if not (patient.name and patient.name.strip()):
                        update_ctx(From, await_name=True)
                        send_text(From, polish(generate_reply("need_name", {})))
                else:
                    alts = human_slot_strings(slots, limit=12, balanced=False)
                    set_ctx(From, parsed_date)
                    send_text(From, polish(generate_reply(
                        "time_unavailable",
                        {"date_dt": datetime.combine(parsed_date, datetime.min.time()), "slots_list": alts}
                    )))
            return ""

        # Falta info → pedir fecha (estricto)
        send_text(From, polish(generate_reply("ask_date_strict", {})))
        return ""

    # Smalltalk / greet por NLU
    if intent in ("smalltalk", "greet"):
        send_text(From, polish(reply or generate_reply("greet", {"now": datetime.now()})))
        return ""

    # Parser natural (último recurso)
    try:
        dt = dtparser.parse(text, dayfirst=True, fuzzy=True)
        d = dt.date()
        lowered = text.lower()
        has_time_hint = (":" in lowered) or (" am" in lowered) or (" pm" in lowered)
        for db in db_session():
            slots = available_slots(db, d, settings.TIMEZONE)
            if not slots:
                send_text(From, polish(generate_reply("day_full", {"date_dt": datetime.combine(d, datetime.min.time())})))
                break
            if has_time_hint:
                target_h = dt.hour; target_m = dt.minute
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = move_or_create_appointment(db, patient, match)
                    SESSION_CTX.pop(From, None)
                    send_text(From, polish(generate_reply("reserved_ok", {"appt_dt": appt.start_at})))
                    if not (patient.name and patient.name.strip()):
                        update_ctx(From, await_name=True)
                        send_text(From, polish(generate_reply("need_name", {})))
                else:
                    alts = human_slot_strings(slots, limit=12, balanced=False)
                    set_ctx(From, d)
                    send_text(From, polish(generate_reply(
                        "time_unavailable",
                        {"date_dt": datetime.combine(d, datetime.min.time()), "slots_list": alts}
                    )))
            else:
                alts = human_slot_strings(slots, limit=12, balanced=True)
                set_ctx(From, d)
                send_text(From, polish(generate_reply(
                    "list_slots_for_date",
                    {"date_dt": datetime.combine(d, datetime.min.time()), "slots_list": alts}
                )))
        return ""
    except Exception:
        pass

    # Fallback final
    send_text(From, polish(generate_reply("fallback", {})))
    return ""