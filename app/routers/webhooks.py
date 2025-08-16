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
from ..replygen import generate_reply

# =========================
# Memoria corta (contexto)
# =========================
SESSION_CTX: dict[str, dict] = {}
CTX_TTL_MIN = 15

def set_ctx(contact: str, last_date, **extra):
    SESSION_CTX[contact] = {
        "last_date": last_date,
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
# Utilidades
# ----------------------------
def normalize(s: str) -> str:
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s

_YES_PAT = re.compile(r"\b(s[ií]|claro|correcto|ok|vale|de acuerdo|afirmativo|me parece|est(a|á) bien|perfecto|sale)\b")
_NO_PAT  = re.compile(r"\b(no|prefiero cambiar|otra fecha|cambiar fecha|no gracias|mejor no)\b")

def is_yes(s: str) -> bool:
    t = (s or "").lower().strip()
    return bool(_YES_PAT.search(t)) or t.startswith(("si","sí","ok"))

def is_no(s: str) -> bool:
    t = (s or "").lower().strip()
    return bool(_NO_PAT.search(t)) or t.startswith("no")

def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_patient_by_contact(db: Session, contact: str) -> models.Patient | None:
    return db.query(models.Patient).filter(models.Patient.contact == contact).first()

def get_appt_by_id(db: Session, appt_id: int | None) -> models.Appointment | None:
    if not appt_id:
        return None
    try:
        return db.get(models.Appointment, appt_id)
    except Exception:
        return None

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
    p = models.Patient(contact=contact)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p

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
    t = (text or "").lower().strip()
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = re.search(r"\b([1-9]|1[0-2])\s*(am|pm)\b", t)
    if m:
        h = int(m.group(1)); ampm = m.group(2)
        if ampm == "pm" and h != 12: h += 12
        if ampm == "am" and h == 12: h = 0
        return h, 0
    m = re.search(r"\b(0?\d|1\d|2[0-3])\s*h?\b", t)
    if m:
        return int(m.group(1)), 0
    return None

def human_list(slots, limit=12, balanced=True):
    if not slots:
        return ""
    selected = slots
    if balanced and len(slots) > limit:
        step = max(1, len(slots) // limit)
        selected = [slots[i] for i in range(0, len(slots), step)][:limit]
    return "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in selected)

_WEEK_MAP = {
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2,
    "jueves": 3, "viernes": 4, "sabado": 5, "sábado": 5, "domingo": 6
}
def extract_spanish_date(text: str, today: date) -> date | None:
    t = (text or "").lower()
    if "pasado mañana" in t: return today + timedelta(days=2)
    if "mañana" in t: return today + timedelta(days=1)
    if "hoy" in t: return today
    for w, idx in _ WEEK_MAP.items():  # noqa: E275 (spacing in key to avoid accidental pasting mistakes)
        if re.search(rf"\b{w}\b", t):
            delta = (idx - today.weekday()) % 7
            if delta == 0: delta = 7
            return today + timedelta(days=delta)
    try:
        dt = dtparser.parse(t, dayfirst=False, fuzzy=True)
        return dt.date()
    except Exception:
        return None

router = APIRouter(prefix="", tags=["webhooks"])

@router.post("/webhooks/whatsapp", response_class=PlainTextResponse)
async def whatsapp_webhook(From: str = Form(None), Body: str = Form(None)):
    if not From:
        return ""
    raw_text = Body or ""
    text = normalize(raw_text)

    # ========= 0) Nombre pendiente tras RESERVA =========
    for db in db_session():
        patient = get_patient_by_contact(db, From)
        if not patient:
            break
        pending_appt_id = (get_ctx(From) or {}).get("pending_appt_id")
        appt = get_appt_by_id(db, pending_appt_id) if pending_appt_id else find_latest_reserved_for_contact(db, From)
        if appt and (patient.name is None or not patient.name.strip()):
            # ¿mandó un nombre?
            t = raw_text.strip()
            if re.fullmatch(r"[A-Za-zÁÉÍÓÚáéíóúÑñ'’\s]{3,60}", t) and len(t.split()) >= 2:
                patient.name = " ".join(p.capitalize() for p in t.split())
                db.commit()
                # Pedir confirmación explícita y marcar flag
                update_ctx(From, await_final_confirm=True, pending_appt_id=appt.id)
                msg = generate_reply("ask_confirm_after_name", raw_text, {"appt_dt": appt.start_at, "patient_name": patient.name})
                send_text(From, msg)
                return ""
            else:
                send_text(From, "¿A nombre de quién registramos la cita? (Nombre y apellido)")
                return ""

    # ========= 1) Flujos de sí/no en contexto =========
    ctx = get_ctx(From) or {}

    # a) Confirmación final después del nombre
    if ctx.get("await_final_confirm"):
        for db in db_session():
            appt = get_appt_by_id(db, ctx.get("pending_appt_id")) or find_latest_reserved_for_contact(db, From)
            patient = get_patient_by_contact(db, From)
            if not appt:
                break
            if is_yes(raw_text):
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
                clear_ctx_flags(From, "await_final_confirm", "pending_appt_id")
                send_text(From, generate_reply("confirm_done", raw_text, {"appt_dt": appt.start_at, "patient_name": (patient.name if patient else "")}))
                return ""
            if is_no(raw_text):
                clear_ctx_flags(From, "await_final_confirm")
                send_text(From, generate_reply("ask_missing_date_or_time", raw_text, {}))
                return ""
        # si no hubo resolución, seguimos

    # b) Mantener fecha y solo cambiar hora
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
        target_h, target_m = pending_time
        for db in db_session():
            slots = available_slots(db, keep_date, settings.TIMEZONE)
            if not slots:
                send_text(From, generate_reply("no_availability_for_date", raw_text, {"date_dt": keep_date}))
                break
            match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
            patient = get_or_create_patient(db, From)
            if match:
                appt = move_or_create_appointment(db, patient, match)
                SESSION_CTX.pop(From, None)
                if not patient.name:
                    update_ctx(From, pending_appt_id=appt.id, await_final_confirm=True)
                    send_text(From, generate_reply("booked_pending_name", raw_text, {"appt_dt": appt.start_at}))
                else:
                    send_text(From, generate_reply("booked_or_moved_ok", raw_text, {"appt_dt": appt.start_at}))
            else:
                sorted_by_diff = sorted(slots, key=lambda x: abs((x.hour*60 + x.minute) - (target_h*60 + target_m)))
                sample = human_list(sorted_by_diff, limit=12, balanced=False)
                set_ctx(From, keep_date)
                send_text(From, generate_reply("time_unavailable_suggest_list", raw_text, {"date_dt": keep_date, "suggestions": sample.split("\n")}))
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
                    send_text(From, generate_reply("no_availability_for_date", raw_text, {"date_dt": parsed_new_date}))
                    break
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = move_or_create_appointment(db, patient, match)
                    clear_ctx_flags(From, "await_new_date", "pending_time")
                    SESSION_CTX[From]["last_date"] = parsed_new_date
                    if not patient.name:
                        update_ctx(From, pending_appt_id=appt.id, await_final_confirm=True)
                        send_text(From, generate_reply("booked_pending_name", raw_text, {"appt_dt": appt.start_at}))
                    else:
                        send_text(From, generate_reply("booked_or_moved_ok", raw_text, {"appt_dt": appt.start_at}))
                else:
                    sorted_by_diff = sorted(slots, key=lambda x: abs((x.hour*60 + x.minute) - (target_h*60 + target_m)))
                    sample = human_list(sorted_by_diff, limit=12, balanced=False)
                    set_ctx(From, parsed_new_date)
                    clear_ctx_flags(From, "await_new_date")
                    send_text(From, generate_reply("time_unavailable_suggest_list", raw_text, {"date_dt": parsed_new_date, "suggestions": sample.split("\n")}))
            return ""
        else:
            for db in db_session():
                slots = available_slots(db, parsed_new_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, generate_reply("no_availability_for_date", raw_text, {"date_dt": parsed_new_date}))
                    break
                sample = human_list(slots, limit=12, balanced=True)
                set_ctx(From, parsed_new_date)
                clear_ctx_flags(From, "await_new_date")
                send_text(From, generate_reply("propose_slots_date", raw_text, {"date_dt": parsed_new_date, "suggestions": sample.split("\n")}))
            return ""

    # ========= 2) Saludo rápido =========
    if text in ("hola","buenas","menu","menú","buenos dias","buenas tardes","buenas noches"):
        nombre_opt = ""
        for db in db_session():
            p = get_patient_by_contact(db, From)
            if p and p.name:
                nombre_opt = p.name
            break
        send_text(From, generate_reply("greet", raw_text, {"patient_name": nombre_opt}))
        return ""

    # ========= 3) NLU base =========
    nlu = analizar(raw_text)
    intent = nlu.get("intent", "fallback")
    entities = nlu.get("entities", {}) or {}
    reply = nlu.get("reply", "")

    print(f"[NLU] from={From} intent={intent} entities={entities} text={(raw_text)[:120]}")

    nlu_date = entities.get("date") or ""
    topic = entities.get("topic") or ""

    # Despedida corta
    if text in ("no","no gracias","gracias","listo","es todo","ninguno","ninguna"):
        send_text(From, generate_reply("goodbye", raw_text, {}))
        return ""

    # Info
    if intent == "info":
        if topic in ("costos","costo","precio","precios"):
            send_text(From, generate_reply("prices", raw_text, {})); return ""
        if topic in ("ubicacion","ubicación","direccion","dirección"):
            send_text(From, generate_reply("location", raw_text, {})); return ""
        send_text(From, reply or generate_reply("fallback", raw_text, {})); return ""

    # Confirmar directa
    if intent == "confirm":
        for db in db_session():
            appt = find_latest_reserved_for_contact(db, From)
            if not appt:
                send_text(From, generate_reply("fallback", raw_text, {})); break
            patient = get_patient_by_contact(db, From)
            if patient and (not patient.name or not patient.name.strip()):
                update_ctx(From, pending_appt_id=appt.id, await_final_confirm=True)
                send_text(From, generate_reply("booked_pending_name", raw_text, {"appt_dt": appt.start_at})); break
            appt.status = models.AppointmentStatus.confirmed
            if not appt.event_id:
                try:
                    ev_id = create_event(
                        summary=f"Consulta — {patient.name or 'Paciente'}",
                        start_local=appt.start_at,
                        duration_min=getattr(settings, "EVENT_DURATION_MIN", 30),
                        location="CLIEMED, Av. Prof. Moisés Sáenz 1500, Monterrey, N.L.",
                        description=f"Canal: WhatsApp\nPaciente: {patient.name or patient.contact}"
                    ); appt.event_id = ev_id
                except Exception:
                    pass
            db.commit()
            send_text(From, generate_reply("confirm_done", raw_text, {"appt_dt": appt.start_at, "patient_name": (patient.name if patient else "")}))
        return ""

    # Cancelar
    if intent == "cancel":
        for db in db_session():
            appt = find_latest_active_for_contact(db, From)
            if not appt:
                send_text(From, generate_reply("fallback", raw_text, {})); break
            appt.status = models.AppointmentStatus.canceled
            if appt.event_id:
                try: delete_event(appt.event_id)
                except Exception: pass
                appt.event_id = None
            db.commit()
            send_text(From, generate_reply("canceled_ok", raw_text, {}))
        return ""

    # Agendar / Reprogramar
    if intent in ("book","reschedule") or parse_time_hint(raw_text):
        today_local = datetime.now().date()
        parsed_date = None
        if entities.get("date"): parsed_date = extract_spanish_date(entities["date"], today_local)
        if not parsed_date: parsed_date = extract_spanish_date(raw_text, today_local)
        explicit_time = parse_time_hint(raw_text)

        if intent in ("reschedule","book") and explicit_time and not parsed_date:
            for db in db_session():
                appt = find_latest_active_for_contact(db, From)
                if appt:
                    appt_date = appt.start_at.date()
                    update_ctx(From, await_keep_date=True, pending_date=appt_date, pending_time=explicit_time)
                    msg = f"¿Mantenemos la fecha del {appt_date.strftime('%d/%m/%Y')} y cambiamos sólo la hora?"
                    send_text(From, msg); return ""
            send_text(From, generate_reply("ask_missing_date_or_time", raw_text, {})); return ""

        ctx = get_ctx(From) or {}
        if not parsed_date and explicit_time and ctx.get("last_date"):
            parsed_date = ctx["last_date"]

        if parsed_date and not explicit_time:
            for db in db_session():
                slots = available_slots(db, parsed_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, generate_reply("no_availability_for_date", raw_text, {"date_dt": parsed_date})); break
                sample = human_list(slots, limit=12, balanced=True)
                set_ctx(From, parsed_date)
                send_text(From, generate_reply("propose_slots_date", raw_text, {"date_dt": parsed_date, "suggestions": sample.split("\n")}))
            return ""

        if explicit_time and not parsed_date:
            send_text(From, generate_reply("ask_missing_date_or_time", raw_text, {})); return ""

        if parsed_date and explicit_time:
            target_h, target_m = explicit_time
            for db in db_session():
                slots = available_slots(db, parsed_date, settings.TIMEZONE)
                if not slots:
                    send_text(From, generate_reply("no_availability_for_date", raw_text, {"date_dt": parsed_date})); break
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = move_or_create_appointment(db, patient, match)
                    SESSION_CTX.pop(From, None)
                    if not patient.name:
                        update_ctx(From, pending_appt_id=appt.id, await_final_confirm=True)
                        send_text(From, generate_reply("booked_pending_name", raw_text, {"appt_dt": appt.start_at}))
                    else:
                        send_text(From, generate_reply("booked_or_moved_ok", raw_text, {"appt_dt": appt.start_at}))
                else:
                    sorted_by_diff = sorted(slots, key=lambda x: abs((x.hour*60 + x.minute) - (target_h*60 + target_m)))
                    sample = human_list(sorted_by_diff, limit=12, balanced=False)
                    set_ctx(From, parsed_date)
                    send_text(From, generate_reply("time_unavailable_suggest_list", raw_text, {"date_dt": parsed_date, "suggestions": sample.split("\n")}))
            return ""

    # Últimos recursos
    try:
        dt = dtparser.parse(text, dayfirst=False, fuzzy=True)
        d = dt.date()
        lowered = text.lower()
        has_time_hint = (":" in lowered) or (" am" in lowered) or (" pm" in lowered)
        for db in db_session():
            slots = available_slots(db, d, settings.TIMEZONE)
            if not slots:
                send_text(From, generate_reply("no_availability_for_date", raw_text, {"date_dt": d})); break
            if has_time_hint:
                target_h, target_m = dt.hour, dt.minute
                match = next((s for s in slots if s.hour == target_h and s.minute == target_m), None)
                patient = get_or_create_patient(db, From)
                if match:
                    appt = move_or_create_appointment(db, patient, match)
                    SESSION_CTX.pop(From, None)
                    if not patient.name:
                        update_ctx(From, pending_appt_id=appt.id, await_final_confirm=True)
                        send_text(From, generate_reply("booked_pending_name", raw_text, {"appt_dt": appt.start_at}))
                    else:
                        send_text(From, generate_reply("booked_or_moved_ok", raw_text, {"appt_dt": appt.start_at}))
                else:
                    sample = human_list(slots, limit=12, balanced=False)
                    set_ctx(From, d)
                    send_text(From, generate_reply("time_unavailable_suggest_list", raw_text, {"date_dt": d, "suggestions": sample.split("\n")}))
            else:
                sample = human_list(slots, limit=12, balanced=True)
                set_ctx(From, d)
                send_text(From, generate_reply("propose_slots_date", raw_text, {"date_dt": d, "suggestions": sample.split("\n")}))
        return ""
    except Exception:
        pass

    send_text(From, generate_reply("fallback", raw_text, {}))
    return ""