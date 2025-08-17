# app/services/scheduling.py
from __future__ import annotations
import os, json
from datetime import datetime, date, time, timedelta
from typing import List, Optional

import pytz
from googleapiclient.discovery import build
from google.oauth2 import service_account

from ..config import settings
from .. import models  # para leer citas reservadas/confirmadas en BD

# ====== Config ======
CALENDAR_ID = getattr(settings, "GCAL_CALENDAR_ID", "")
TIMEZONE = getattr(settings, "TIMEZONE", "America/Mexico_City")

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
    resp = service.freebusy().query(body=body).execute()
    busy = resp["calendars"][CALENDAR_ID]["busy"]
    out = []
    for b in busy:
        bs = datetime.fromisoformat(b["start"])
        be = datetime.fromisoformat(b["end"])
        # normaliza a zona local
        if bs.tzinfo is None:
            bs = tz.localize(bs)
        else:
            bs = bs.astimezone(tz)
        if be.tzinfo is None:
            be = tz.localize(be)
        else:
            be = be.astimezone(tz)
        out.append((bs, be))
    return out

def _get_busy_windows_db(db_session, day: date) -> List[tuple[datetime, datetime]]:
    """
    Ventanas ocupadas por reservas en nuestra BD (reserved/confirmed) para ese día.
    Útil para evitar doble booking antes de crear el evento en Calendar.
    """
    if db_session is None:
        return []
    tz = _local_tz()
    day_start = tz.localize(datetime.combine(day, time(0, 0)))
    day_end   = day_start + timedelta(days=1)

    appts = (
        db_session.query(models.Appointment)
        .filter(models.Appointment.start_at >= day_start.replace(tzinfo=None))
        .filter(models.Appointment.start_at <  day_end.replace(tzinfo=None))
        .filter(models.Appointment.status.in_([
            models.AppointmentStatus.reserved,
            models.AppointmentStatus.confirmed,
        ]))
        .all()
    )

    out = []
    for ap in appts:
        # En la BD guardamos naive en hora local; conviértelo a aware local
        start_local = ap.start_at
        if start_local.tzinfo is None:
            start_local = tz.localize(start_local)
        end_local = start_local + timedelta(minutes=DEFAULT_EVENT_DURATION_MIN)
        out.append((start_local, end_local))
    return out

# ====== Slots disponibles ======
def available_slots(db_session, day: date, timezone_str: Optional[str] = None) -> List[datetime]:
    """
    Genera slots de SLOT_MINUTES entre CLINIC_OPEN_HOUR y CLINIC_CLOSE_HOUR en zona local/`timezone_str`,
    y elimina los que interfieren con eventos ocupados del Google Calendar **y** reservas en BD.
    Devuelve datetimes *tz-aware* en zona local listos para strftime.
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

        # si el slot se superpone con algo ocupado → descartar
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
    start_local debe ser tz-aware en TIMEZONE.
    """
    service = _get_service()
    if start_local.tzinfo is None:
        start_local = _localize(start_local)
    end_local = start_local + timedelta(minutes=duration_min)

    body = {
        "summary": summary,
        "start": {"dateTime": start_local.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_local.isoformat(), "timeZone": TIMEZONE},
        "location": location or "",
        "description": description or "",
    }
    ev = service.events().insert(calendarId=CALENDAR_ID, body=body).execute()
    return ev["id"]

def update_event(event_id: str, new_start_local: datetime, duration_min: int = DEFAULT_EVENT_DURATION_MIN) -> str:
    """
    Mueve/actualiza un evento existente. Devuelve el eventId (igual).
    """
    service = _get_service()
    if new_start_local.tzinfo is None:
        new_start_local = _localize(new_start_local)
    new_end = new_start_local + timedelta(minutes=duration_min)

    body = {
        "start": {"dateTime": new_start_local.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": new_end.isoformat(), "timeZone": TIMEZONE},
    }
    ev = service.events().patch(calendarId=CALENDAR_ID, eventId=event_id, body=body).execute()
    return ev["id"]

def delete_event(event_id: str):
    """Elimina un evento por id (idempotente)."""
    service = _get_service()
    try:
        service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
    except Exception:
        # Si ya no existe, lo ignoramos para que sea idempotente
        pass


# ====== Diagnóstico manual ======
if __name__ == "__main__":
    import traceback

    print(f"=== Diagnóstico Google Calendar ===")
    try:
        print(f"TIMEZONE: {TIMEZONE}")
        print(f"GCAL_CALENDAR_ID: {CALENDAR_ID}")

        svc = _get_service()
        cal = svc.calendars().get(calendarId=CALENDAR_ID).execute()
        print(f"Conectado a calendario: {cal.get('summary', '(sin summary)')}")

        today = date.today()
        for offset in range(0, 3):
            d = today + timedelta(days=offset)
            print(f"\n=== Slots para {d.isoformat()} | TZ={TIMEZONE} ===")
            try:
                slots = available_slots(None, d, TIMEZONE)
                if not slots:
                    print("(Sin disponibilidad o fuera de horario)")
                else:
                    for s in slots:
                        print(s.strftime("%H:%M"))
            except Exception as e:
                print(f"ERROR al consultar slots: {e}")
                traceback.print_exc()

        print("\n✅ Prueba terminada.")
    except Exception as e:
        print(f"❌ Error general al inicializar o consultar Calendar: {e}")
        traceback.print_exc()