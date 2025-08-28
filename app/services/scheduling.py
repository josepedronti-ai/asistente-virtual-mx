# app/services/scheduling.py
from __future__ import annotations
import os, json, logging
from datetime import datetime, date, time, timedelta
from typing import List, Optional

import pytz
from googleapiclient.discovery import build
from google.oauth2 import service_account

from ..config import settings
from .. import models  # para leer citas reservadas/confirmadas en BD

logger = logging.getLogger(__name__)

# ====== Config ======
# Compat: aceptamos ambos nombres de env para el mismo propósito
CALENDAR_ID = (
    getattr(settings, "GOOGLE_CALENDAR_ID", "")
    or getattr(settings, "GCAL_CALENDAR_ID", "")
)
if not CALENDAR_ID:
    logger.warning("GOOGLE_CALENDAR_ID/GCAL_CALENDAR_ID no definido: usando 'primary'.")
    CALENDAR_ID = "primary"

# Usa una sola TZ en toda la integración
TIMEZONE = getattr(settings, "TIMEZONE", "America/Monterrey") or "America/Monterrey"

# Horario de consultorio (acepta OPEN/CLOSE o START/END por compatibilidad)
CLINIC_OPEN_HOUR = getattr(settings, "CLINIC_OPEN_HOUR", getattr(settings, "CLINIC_START_HOUR", 16))
CLINIC_CLOSE_HOUR = getattr(settings, "CLINIC_CLOSE_HOUR", getattr(settings, "CLINIC_END_HOUR", 22))
SLOT_MINUTES = getattr(settings, "SLOT_MINUTES", 30)

# Crea eventos sólo al confirmar
DEFAULT_EVENT_DURATION_MIN = getattr(settings, "EVENT_DURATION_MIN", 30)

# ====== Autenticación con Service Account ======
_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_service_cache = None

def _load_credentials():
    """
    Lee credenciales de Service Account desde:
    - settings.GCAL_SA_JSON (puede ser JSON o ruta a archivo),
      y opcionalmente settings.GCAL_IMPERSONATE_EMAIL para 'delegated access'
    """
    sa_json = getattr(settings, "GCAL_SA_JSON", "")
    if not sa_json:
        raise RuntimeError("Falta GCAL_SA_JSON en settings/env.")

    try:
        if os.path.exists(sa_json):
            with open(sa_json, "r", encoding="utf-8") as f:
                info = json.load(f)
        else:
            info = json.loads(sa_json)
    except Exception as e:
        raise RuntimeError(f"GCAL_SA_JSON inválido: {e}")

    creds = service_account.Credentials.from_service_account_info(info, scopes=_SCOPES)
    impersonate = getattr(settings, "GCAL_IMPERSONATE_EMAIL", "")
    if impersonate:
        creds = creds.with_subject(impersonate)
    return creds

def _get_service():
    global _service_cache
    if _service_cache is None:
        creds = _load_credentials()
        _service_cache = build("calendar", "v3", credentials=creds, cache_discovery=False)
        logger.info("Google Calendar client inicializado. CALENDAR_ID=%s TZ=%s", CALENDAR_ID, TIMEZONE)
    return _service_cache

# ====== Utilidades de tiempo ======
def _local_tz():
    return pytz.timezone(TIMEZONE)

def _localize(dt_naive: datetime) -> datetime:
    """Pone timezone local a un datetime naive."""
    return _local_tz().localize(dt_naive)

def _to_iso(dt_local: datetime) -> str:
    """Convierte datetime aware → ISO8601."""
    if dt_local.tzinfo is None:
        dt_local = _localize(dt_local)
    return dt_local.isoformat()

def _overlaps(a_start, a_end, b_start, b_end):
    return not (a_end <= b_start or b_end <= a_start)

def _iso_to_dt(s: str) -> datetime:
    """
    Convierte iso de Google (que puede traer 'Z') a datetime aware y lo pasa a TZ local.
    """
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
    except Exception:
        dt = datetime.strptime(s.split(".")[0], "%Y-%m-%dT%H:%M:%S")
        dt = dt.replace(tzinfo=pytz.UTC)
    if dt.tzinfo is None:
        dt = pytz.UTC.localize(dt)
    return dt.astimezone(_local_tz())

# ====== Busy windows: Google Calendar + BD ======
def _get_busy_windows_gcal(day: date) -> List[tuple[datetime, datetime]]:
    """
    Ventanas ocupadas [(start_local, end_local)] del calendario en ese día.
    """
    service = _get_service()
    tz = _local_tz()

    day_start_local = tz.localize(datetime.combine(day, time(0, 0)))
    day_end_local   = day_start_local + timedelta(days=1)

    body = {
        "timeMin": day_start_local.isoformat(),
        "timeMax": day_end_local.isoformat(),
        "timeZone": TIMEZONE,
        "items": [{"id": CALENDAR_ID}],
    }
    logger.debug("GCAL freebusy request: %s", body)
    resp = service.freebusy().query(body=body).execute()
    busy = resp["calendars"][CALENDAR_ID]["busy"]
    out = []
    for b in busy:
        bs = _iso_to_dt(b["start"])
        be = _iso_to_dt(b["end"])
        out.append((bs, be))
    logger.debug("GCAL freebusy busy_windows=%s", [(a.isoformat(), b.isoformat()) for a,b in out])
    return out

def _get_busy_windows_db(db_session, day: date) -> List[tuple[datetime, datetime]]:
    """
    Ventanas ocupadas por reservas en nuestra BD (reserved/confirmed) para ese día.
    """
    if db_session is None:
        return []
    tz = _local_tz()
    day_start = tz.localize(datetime.combine(day, time(0, 0)))
    day_end   = day_start + timedelta(days=1)

    # Si en BD guardas naive-local, compara en naive. Si guardas aware, adapta.
    q_start = day_start.replace(tzinfo=None)
    q_end   = day_end.replace(tzinfo=None)

    appts = (
        db_session.query(models.Appointment)
        .filter(models.Appointment.start_at >= q_start)
        .filter(models.Appointment.start_at <  q_end)
        .filter(models.Appointment.status.in_([
            models.AppointmentStatus.reserved,
            models.AppointmentStatus.confirmed,
        ]))
        .all()
    )

    out = []
    for ap in appts:
        start_local = ap.start_at
        if start_local.tzinfo is None:
            start_local = tz.localize(start_local)
        else:
            start_local = start_local.astimezone(tz)
        end_local = start_local + timedelta(minutes=DEFAULT_EVENT_DURATION_MIN)
        out.append((start_local, end_local))
    logger.debug("DB busy_windows=%s", [(a.isoformat(), b.isoformat()) for a,b in out])
    return out

# ====== Slots disponibles ======
def available_slots(db_session, day: date, timezone_str: Optional[str] = None) -> List[datetime]:
    """
    Genera slots de SLOT_MINUTES entre CLINIC_OPEN_HOUR y CLINIC_CLOSE_HOUR en zona local/`timezone_str`,
    y elimina los que interfieren con eventos ocupados del Google Calendar **y** reservas en BD.
    """
    tz = pytz.timezone(timezone_str or TIMEZONE)

    start_local = tz.localize(datetime.combine(day, time(CLINIC_OPEN_HOUR, 0)))
    end_local   = tz.localize(datetime.combine(day, time(CLINIC_CLOSE_HOUR, 0)))

    busy_gcal = _get_busy_windows_gcal(day)
    busy_db   = _get_busy_windows_db(db_session, day)
    busy_windows = busy_gcal + busy_db

    slots = []
    cur = start_local
    delta = timedelta(minutes=SLOT_MINUTES)

    while cur + delta <= end_local:
        slot_start = cur
        slot_end   = cur + delta
        if any(_overlaps(slot_start, slot_end, b0, b1) for (b0, b1) in busy_windows):
            cur += delta
            continue
        slots.append(slot_start)
        cur += delta

    return slots

# ====== Operaciones sobre eventos ======
def create_event(summary: str, start_local: datetime, duration_min: int = DEFAULT_EVENT_DURATION_MIN,
                 location: str = "", description: str = "") -> str:
    """
    Crea un evento en el calendario y devuelve su eventId.
    start_local puede venir naive local o aware; se normaliza a TZ local.
    """
    service = _get_service()
    tz = _local_tz()

    if start_local.tzinfo is None:
        start_local = tz.localize(start_local)
    else:
        start_local = start_local.astimezone(tz)

    end_local = start_local + timedelta(minutes=duration_min)

    body = {
        "summary": summary,
        "start": {"dateTime": start_local.isoformat(), "timeZone": TIMEZONE},
        "end":   {"dateTime": end_local.isoformat(),   "timeZone": TIMEZONE},
        "location": location or "",
        "description": description or "",
    }

    logger.info("GCAL create_event request: calendar_id=%s start=%s end=%s tz=%s body=%s",
                CALENDAR_ID, start_local.isoformat(), end_local.isoformat(), TIMEZONE, body)

    ev = service.events().insert(calendarId=CALENDAR_ID, body=body, supportsAttachments=False).execute()
    event_id = ev.get("id")
    html_link = ev.get("htmlLink")
    logger.info("GCAL create_event OK: event_id=%s htmlLink=%s", event_id, html_link)
    return event_id

def update_event(event_id: str, new_start_local: datetime, duration_min: int = DEFAULT_EVENT_DURATION_MIN) -> str:
    """
    Mueve/actualiza un evento existente. Devuelve el eventId (igual).
    """
    service = _get_service()
    tz = _local_tz()

    if new_start_local.tzinfo is None:
        new_start_local = tz.localize(new_start_local)
    else:
        new_start_local = new_start_local.astimezone(tz)

    new_end = new_start_local + timedelta(minutes=duration_min)

    body = {
        "start": {"dateTime": new_start_local.isoformat(), "timeZone": TIMEZONE},
        "end":   {"dateTime": new_end.isoformat(),         "timeZone": TIMEZONE},
    }

    logger.info("GCAL update_event request: calendar_id=%s event_id=%s body=%s",
                CALENDAR_ID, event_id, body)

    ev = service.events().patch(calendarId=CALENDAR_ID, eventId=event_id, body=body).execute()
    logger.info("GCAL update_event OK: event_id=%s", ev.get("id"))
    return ev["id"]

def delete_event(event_id: str):
    """Elimina un evento por id (idempotente)."""
    service = _get_service()
    logger.info("GCAL delete_event: calendar_id=%s event_id=%s", CALENDAR_ID, event_id)
    try:
        service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
        logger.info("GCAL delete_event OK: event_id=%s", event_id)
    except Exception as e:
        logger.warning("GCAL delete_event WARN (puede no existir): event_id=%s err=%s", event_id, e)

# ====== Helpers de diagnóstico para admin router ======
def list_upcoming_events(limit: int = 10):
    svc = _get_service()
    tz = _local_tz()
    now = datetime.now(tz).isoformat()
    resp = svc.events().list(
        calendarId=CALENDAR_ID,
        timeMin=now,
        maxResults=limit,
        singleEvents=True,
        orderBy="startTime"
    ).execute()
    items = resp.get("items", [])
    out = []
    for ev in items:
        start = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
        out.append({
            "id": ev.get("id"),
            "summary": ev.get("summary"),
            "start": start,
            "htmlLink": ev.get("htmlLink"),
        })
    return out

def freebusy_for_date(day: date):
    tz = _local_tz()
    start = tz.localize(datetime.combine(day, time(0,0)))
    end   = start + timedelta(days=1)
    svc = _get_service()
    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": TIMEZONE,
        "items": [{"id": CALENDAR_ID}],
    }
    resp = svc.freebusy().query(body=body).execute()
    return resp.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", [])