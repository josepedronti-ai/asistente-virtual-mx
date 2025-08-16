# app/routers/webhooks.py
from __future__ import annotations
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
from ..services.scheduling import (
    available_slots,
    create_event,
    update_event,
    delete_event,
)
from ..services.nlu import analizar
from ..replygen.core import generate_reply

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

# --- Afirmaciones/negaciones humanas ---
_YES_PAT = re.compile(r"\b(s[ií]|claro|correcto|ok|vale|de acuerdo|afirmativo|me parece|esta bien|está bien|perfecto)\b")
_NO_PAT  = re.compile(r"\b(no|prefiero cambiar|otra fecha|cambiar fecha|no gracias|no, gracias|mejor no)\b")

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
    # hh (24h)
    m = re.search(r"\b(0?\d|1\d|2[0-3])\s*h?\b", t)
    if m:
        return int(m.group(1)), 0
    return None

def human_list(slots, limit=12, balanced=True):
    """
    Devuelve hasta 'limit' horarios. Si balanced=True, reparte
    a lo largo del día (no solo los primeros de la mañana).
    """
    if not slots:
        return ""
    selected = slots
    if balanced and len(slots) > limit:
        step = max(1, len(slots) // limit)
        selected = [slots[i] for i in range(0, len(slots), step)][:limit]
    return "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in selected)

def looks_like_name(text: str) -> str | None:
    """2–5 palabras, sin dígitos; devuelve nombre capitalizado o None."""
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

# --- Fechas en español básicas ---
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
                msg = generate_reply(
                    intent="ask_confirm_after_name",
                    user_text=raw_text,
                    state={"appt_dt": pending.start_at, "patient_name": patient.name},
                )
                send_text(From, msg)
                return ""
            else:
                send_text(From, generate_reply("ask_missing_date_or_time", raw_text, {}))
                return ""

    # 1) Saludo sencillo (humano, sin emojis)
    if text in ("hola", "buenas", "menu", "menú", "buenos dias", "buenas tardes", "buenas noches"):
        nombre_opt = ""
        for db in db_session():
            p = get_patient_by_contact(db, From)
            if p and p.name:
                nombre_opt = p.name
            break
        send_text(From, generate_reply("greet", raw_text, {"patient_name": nombre_opt}))
        return ""

    # Atajo previo (hora sola + contexto)
    explicit_time_pre = parse_time_hint(raw_text)
    ctx = get_ctx(From) or {}

    # ======= Manejo de “sí/no” para mantener FECHA al reprogramar =======
    awaiting_keep = ctx.get("await_keep_date", False)
    awaiting_new_date = ctx.get("await_new_date", False)

    if awaiting_keep and is_yes(raw_text):
        keep_date = ctx.get("pending_date")
        pending_time = ctx.get("pending_time")  # (h,m) o None
        clear_ctx_flags(From, "await_keep_date")
        if not keep_date:
            send_text(From, generate_reply("ask_missing_date_or_time", raw_text, {}))
            return ""
        if not pending_time:
            update_ctx(From, last_date=keep_date)
            send_text(From, generate_reply("ask_missing_date_or_time", raw_text, {"last_date": keep_date}))
            return ""
        # fecha + hora -> intentar mover/crear
        target_h, target_m = pending_time
        for db in db_session():
            slots = available_slots(db, keep_date, settings.TIMEZONE)
            if not slots:
                send_text(From, generate_reply("no_availability_for_date", raw_text, {"date_dt": datetime.combine(keep_date, datetime.min.time())}))
                break
            match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
            patient = get_or_create_patient(db, From)
            if match:
                appt = move_or_create_appointment(db, patient, match)
                SESSION_CTX.pop(From, None)
                if not patient.name:
                    send_text(From, generate_reply("booked_pending_name", raw_text, {"appt_dt": appt.start_at}))
                else:
                    send_text(From, generate_reply("booked_or_moved_ok", raw_text, {"appt_dt": appt.start_at}))
            else:
                # sugerir cercanos
                sorted_by_diff = sorted(
                    slots,
                    key=lambda x: abs((x.hour*60 + x.minute) - (target_h*60 + target_m))
                )
                sample = human_list(sorted_by_diff, limit=12, balanced=False)
                set_ctx(From, keep_date)
                send_text(From, generate_reply(
                    "time_unavailable_suggest_list",
                    raw_text,
                    {"date_dt": datetime.combine(keep_date, datetime.min.time()), "suggestions": sample.split("\n")}
                ))
        return ""

    if awaiting_keep and is_no(raw_text):
        clear_ctx_flags(From, "await_keep_date")
        update_ctx(From, await_new_date=True)
        send_text(From, generate_reply("ask_missing_date_or_time", raw_text, {}))
        return ""

    if awaiting_new_date:
        today_local = datetime.now().date()
        parsed_new_date = extract_spanish_date(raw_text, today_local)
        if not parsed_new_date:
            send_text(From, generate_reply("ask_missing_date_or_time", raw_text, {}))
            return ""
        pending_time = ctx.get("pending_time")
        if pending_time:
            target_h, target_m = pending_time
            for db in db_session():
                slots = available_slots(db, parsed_new_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, generate_reply("no_availability_for_date", raw_text, {"date_dt": datetime.combine(parsed_new_date, datetime.min.time())}))
                    break
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = move_or_create_appointment(db, patient, match)
                    clear_ctx_flags(From, "await_new_date", "pending_time")
                    SESSION_CTX[From]["last_date"] = parsed_new_date
                    if not patient.name:
                        send_text(From, generate_reply("booked_pending_name", raw_text, {"appt_dt": appt.start_at}))
                    else:
                        send_text(From, generate_reply("booked_or_moved_ok", raw_text, {"appt_dt": appt.start_at}))
                else:
                    sorted_by_diff = sorted(
                        slots,
                        key=lambda x: abs((x.hour*60 + x.minute) - (target_h*60 + target_m))
                    )
                    sample = human_list(sorted_by_diff, limit=12, balanced=False)
                    set_ctx(From, parsed_new_date)
                    clear_ctx_flags(From, "await_new_date")
                    send_text(From, generate_reply(
                        "time_unavailable_suggest_list",
                        raw_text,
                        {"date_dt": datetime.combine(parsed_new_date, datetime.min.time()), "suggestions": sample.split("\n")}
                    ))
            return ""
        else:
            for db in db_session():
                slots = available_slots(db, parsed_new_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, generate_reply("no_availability_for_date", raw_text, {"date_dt": datetime.combine(parsed_new_date, datetime.min.time())}))
                    break
                sample = human_list(slots, limit=12, balanced=True)
                set_ctx(From, parsed_new_date)
                clear_ctx_flags(From, "await_new_date")
                send_text(From, generate_reply(
                    "time_unavailable_suggest_list",  # reutilizamos plantilla con lista
                    raw_text,
                    {"date_dt": datetime.combine(parsed_new_date, datetime.min.time()), "suggestions": sample.split("\n")}
                ))
            return ""

    # 2) NLU
    nlu = analizar(raw_text)
    intent = nlu.get("intent", "fallback")
    entities = nlu.get("entities", {}) or {}
    reply = nlu.get("reply", "")

    print(f"[NLU] from={From} intent={intent} entities={entities} text={(raw_text)[:120]}")

    nlu_date = entities.get("date") or ""
    topic = entities.get("topic") or ""

    # Despedida corta
    if text in ("no", "no gracias", "gracias", "listo", "es todo", "ninguno", "ninguna"):
        send_text(From, generate_reply("goodbye", raw_text, {}))
        return ""

    # 3) Información (precios/ubicación)
    if intent == "info" and not explicit_time_pre:
        if topic in ("costos", "costo", "precio", "precios"):
            send_text(From, generate_reply("prices", raw_text, {}))
            return ""
        if topic in ("ubicacion", "ubicación", "direccion", "dirección"):
            send_text(From, generate_reply("location", raw_text, {}))
            return ""
        send_text(From, reply or generate_reply("fallback", raw_text, {}))
        return ""

    # 4) Confirmar
    if intent == "confirm" and not explicit_time_pre:
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt:
                send_text(From, generate_reply("fallback", raw_text, {}))
                break
            patient = get_patient_by_contact(db, From)
            if patient and (not patient.name or not patient.name.strip()):
                send_text(From, generate_reply("booked_pending_name", raw_text, {"appt_dt": appt.start_at}))
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
            send_text(From, generate_reply("confirm_done", raw_text, {"appt_dt": appt.start_at, "patient_name": (patient.name if patient else "")}))
        return ""

    # 5) Cancelar
    if intent == "cancel" and not explicit_time_pre:
        for db in db_session():
            appt = find_latest_active_for_contact(db, From)
            if not appt:
                send_text(From, generate_reply("fallback", raw_text, {}))
                break
            appt.status = models.AppointmentStatus.canceled
            if appt.event_id:
                try:
                    delete_event(appt.event_id)
                except Exception:
                    pass
                appt.event_id = None
            db.commit()
            send_text(From, generate_reply("canceled_ok", raw_text, {}))
        return ""

    # 6) Agendar / Reprogramar
    if intent in ("book", "reschedule") or explicit_time_pre:
        today_local = datetime.now().date()
        parsed_date = None

        if nlu_date:
            parsed_date = extract_spanish_date(nlu_date, today_local)
        if not parsed_date:
            parsed_date = extract_spanish_date(raw_text, today_local)

        explicit_time = parse_time_hint(raw_text)

        # SOLO HORA y hay cita activa → primero confirmar si mantiene FECHA
        if intent in ("reschedule","book") and explicit_time and not parsed_date:
            for db in db_session():
                appt = find_latest_active_for_contact(db, From)
                if appt:
                    appt_date = appt.start_at.date()
                    update_ctx(From, await_keep_date=True, pending_date=appt_date, pending_time=explicit_time)
                    msg = generate_reply(
                        "ask_confirm_after_name",
                        raw_text,
                        {"appt_dt": datetime.combine(appt_date, datetime.min.time()), "patient_name": (get_patient_by_contact(db, From).name if get_patient_by_contact(db, From) else "")}
                    )
                    send_text(From, msg)
                    return ""
            send_text(From, generate_reply("ask_missing_date_or_time", raw_text, {}))
            return ""

        # Si no hay fecha pero tenemos contexto y el usuario dio hora → usa la del contexto
        ctx = get_ctx(From) or {}
        if not parsed_date and explicit_time and ctx.get("last_date"):
            parsed_date = ctx["last_date"]

        # Caso A: fecha SÍ, hora NO -> pedir hora y guardar contexto (listando horarios)
        if parsed_date and not explicit_time:
            for db in db_session():
                slots = available_slots(db, parsed_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, generate_reply("no_availability_for_date", raw_text, {"date_dt": datetime.combine(parsed_date, datetime.min.time())}))
                    break
                sample = human_list(slots, limit=12, balanced=True)
                set_ctx(From, parsed_date)
                send_text(From, generate_reply(
                    "time_unavailable_suggest_list",  # usamos misma plantilla para mostrar lista
                    raw_text,
                    {"date_dt": datetime.combine(parsed_date, datetime.min.time()), "suggestions": sample.split("\n")}
                ))
            return ""

        # Caso B: hora SÍ, fecha NO
        if explicit_time and not parsed_date:
            send_text(From, generate_reply("ask_missing_date_or_time", raw_text, {}))
            return ""

        # Caso C: fecha SÍ y hora SÍ -> mover/crear y pedir nombre si falta
        if parsed_date and explicit_time:
            target_h, target_m = explicit_time
            for db in db_session():
                slots = available_slots(db, parsed_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, generate_reply("no_availability_for_date", raw_text, {"date_dt": datetime.combine(parsed_date, datetime.min.time())}))
                    break
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = move_or_create_appointment(db, patient, match)
                    SESSION_CTX.pop(From, None)
                    if not patient.name:
                        send_text(From, generate_reply("booked_pending_name", raw_text, {"appt_dt": appt.start_at}))
                    else:
                        send_text(From, generate_reply("booked_or_moved_ok", raw_text, {"appt_dt": appt.start_at}))
                else:
                    sorted_by_diff = sorted(
                        slots,
                        key=lambda x: abs((x.hour*60 + x.minute) - (target_h*60 + target_m))
                    )
                    sample = human_list(sorted_by_diff, limit=12, balanced=False)
                    set_ctx(From, parsed_date)
                    send_text(From, generate_reply(
                        "time_unavailable_suggest_list",
                        raw_text,
                        {"date_dt": datetime.combine(parsed_date, datetime.min.time()), "suggestions": sample.split("\n")}
                    ))
            return ""

        # Caso D: sin suficiente info
        natural = generate_reply(
            intent="ask_missing_date_or_time",
            user_text=raw_text,
            state={"last_date": ctx.get("last_date")}
        )
        send_text(From, natural or "¿Qué fecha te queda mejor?")
        return ""

    # 7) Smalltalk / saludo por NLU
    if intent in ("smalltalk", "greet"):
        if reply:
            send_text(From, reply)
            return ""
        send_text(From, generate_reply("greet", raw_text, {}))
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
                send_text(From, generate_reply("no_availability_for_date", raw_text, {"date_dt": datetime.combine(d, datetime.min.time())}))
                break
            if has_time_hint:
                target_h = dt.hour
                target_m = dt.minute
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = move_or_create_appointment(db, patient, match)
                    SESSION_CTX.pop(From, None)
                    if not patient.name:
                        send_text(From, generate_reply("booked_pending_name", raw_text, {"appt_dt": appt.start_at}))
                    else:
                        send_text(From, generate_reply("booked_or_moved_ok", raw_text, {"appt_dt": appt.start_at}))
                else:
                    sample = human_list(slots, limit=12, balanced=False)
                    set_ctx(From, d)
                    send_text(From, generate_reply(
                        "time_unavailable_suggest_list",
                        raw_text,
                        {"date_dt": datetime.combine(d, datetime.min.time()), "suggestions": sample.split("\n")}
                    ))
            else:
                sample = human_list(slots, limit=12, balanced=True)
                set_ctx(From, d)
                send_text(From, generate_reply(
                    "time_unavailable_suggest_list",
                    raw_text,
                    {"date_dt": datetime.combine(d, datetime.min.time()), "suggestions": sample.split("\n")}
                ))
        return ""
    except Exception:
        pass

    # 9) Fallback final
    send_text(From, generate_reply("fallback", raw_text, {}))
    return ""