# app/services/scheduling.py
from __future__ import annotations
import os, json
from datetime import datetime, date, time, timedelta
from typing import List, Optional

import pytz
from googleapiclient.discovery import build
from google.oauth2 import service_account

from ..config import settings

# ====== Config por defecto (usa settings si existen) ======
CALENDAR_ID = getattr(settings, "GCAL_CALENDAR_ID", "")
TIMEZONE = getattr(settings, "TIMEZONE", "America/Mexico_City")

# Horario de consultorio local y tamaño de slot
CLINIC_START_HOUR = getattr(settings, "CLINIC_START_HOUR", 9)   # 9am
CLINIC_END_HOUR   = getattr(settings, "CLINIC_END_HOUR", 18)   # 6pm
SLOT_MINUTES      = getattr(settings, "SLOT_MINUTES", 30)

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
            # asumimos que es el JSON en texto
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
def _localize(dt_naive: datetime) -> datetime:
    """Pone timezone local a un datetime naive."""
    tz = pytz.timezone(TIMEZONE)
    return tz.localize(dt_naive)

def _to_iso(dt_local: datetime) -> str:
    """Convierte datetime aware → ISO8601."""
    if dt_local.tzinfo is None:
        dt_local = _localize(dt_local)
    return dt_local.isoformat()

def _overlaps(a_start, a_end, b_start, b_end):
    return not (a_end <= b_start or b_end <= a_start)

# ====== Free/Busy y slots ======
def _get_busy_windows(day: date) -> List[tuple[datetime, datetime]]:
    """
    Devuelve ventanas ocupadas [(start_local, end_local)] del calendario en ese día (horario completo del día).
    """
    service = _get_service()
    tz = pytz.timezone(TIMEZONE)

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
        # Asegurar tz awareness en local TZ
        if bs.tzinfo is None:
            bs = tz.localize(bs)
        if be.tzinfo is None:
            be = tz.localize(be)
        out.append((bs, be))
    return out

def available_slots(db_session, day: date, timezone_str: Optional[str] = None) -> List[datetime]:
    """
    Genera slots de SLOTS_MINUTES entre CLINIC_START_HOUR y CLINIC_END_HOUR en zona local,
    y elimina los que interfieren con eventos ocupados del Google Calendar.
    Devuelve datetimes *locales* (tz-aware) listos para formateo strftime.
    """
    tz = pytz.timezone(timezone_str or TIMEZONE)

    start_local = tz.localize(datetime.combine(day, time(CLINIC_START_HOUR, 0)))
    end_local   = tz.localize(datetime.combine(day, time(CLINIC_END_HOUR, 0)))

    busy_windows = _get_busy_windows(day)
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

# ====== Operaciones sobre eventos (confirmar/reprogramar/cancelar) ======
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

if __name__ == "__main__":
    import traceback
    from datetime import date

    print(f"=== Diagnóstico Google Calendar ===")
    try:
        # Confirmar carga de settings
        print(f"TIMEZONE: {TIMEZONE}")
        print(f"GCAL_CALENDAR_ID: {CALENDAR_ID}")

        # Probar credenciales y servicio
        svc = _get_service()
        cal = svc.calendars().get(calendarId=CALENDAR_ID).execute()
        print(f"Conectado a calendario: {cal.get('summary', '(sin summary)')}")

        # Probar free/busy y slots para hoy y próximos 2 días
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