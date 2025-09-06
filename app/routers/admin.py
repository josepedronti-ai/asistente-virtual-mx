# app/routers/admin.py
from __future__ import annotations
from fastapi import APIRouter, Header, HTTPException, Query
from datetime import datetime, timedelta
from typing import Optional, List

from ..config import settings

# Memoria del agente
try:
    from ..agent.agent_controller import _AGENT_SESSIONS  # type: ignore
except Exception:
    _AGENT_SESSIONS = {}

# BD
from ..database import SessionLocal
from .. import models

# Herramientas de Calendar
from ..services.scheduling import (
    _get_service,
    TIMEZONE,
    CALENDAR_ID,
    create_event,
    delete_event,
)

router = APIRouter(tags=["admin"])

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _require_admin(x_admin_token: str | None) -> None:
    expected = (settings.ADMIN_TOKEN or "").strip()
    provided = (x_admin_token or "").strip()
    if not expected:
        raise HTTPException(status_code=403, detail="ADMIN_TOKEN no configurado")
    if provided != expected:
        raise HTTPException(status_code=401, detail="Token inválido")

def _db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _parse_date(s: str) -> datetime.date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        raise HTTPException(status_code=400, detail="Formato de fecha inválido. Use YYYY-MM-DD.")

# ──────────────────────────────────────────────────────────────────────────────
# Básicos
# (recuerda: main.py monta este router con prefix="/admin")
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/ping")
def admin_ping():
    return {"ok": True, "ts": datetime.utcnow().isoformat()}

@router.get("/health")
def admin_health():
    return {
        "ok": True,
        "app": settings.APP_NAME,
        "env": settings.ENV,
        "tz": settings.TIMEZONE,
        "calendar_id": CALENDAR_ID,
        "agent_sessions": len(_AGENT_SESSIONS) if isinstance(_AGENT_SESSIONS, dict) else "n/a",
        "ts": datetime.utcnow().isoformat(),
    }

@router.post("/mem/clear")
def admin_clear_memory(x_admin_token: str | None = Header(default=None)):
    _require_admin(x_admin_token)
    try:
        if isinstance(_AGENT_SESSIONS, dict):
            _AGENT_SESSIONS.clear()
    except Exception:
        pass
    return {"ok": True, "message": "Memoria del agente limpiada."}

# ──────────────────────────────────────────────────────────────────────────────
# Calendar: diagnóstico
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/calendar/list")
def admin_calendar_list(
    x_admin_token: str | None = Header(default=None),
    limit: int = Query(default=10, ge=1, le=50),
):
    """
    Lista próximos eventos del calendario (singleEvents, orderBy=startTime).
    """
    _require_admin(x_admin_token)
    svc = _get_service()
    time_min = datetime.utcnow().isoformat() + "Z"
    resp = svc.events().list(
        calendarId=CALENDAR_ID,
        timeMin=time_min,
        maxResults=limit,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    items = resp.get("items", [])
    out = [{
        "id": ev.get("id"),
        "summary": ev.get("summary"),
        "start": ev.get("start"),
        "end": ev.get("end"),
        "htmlLink": ev.get("htmlLink"),
    } for ev in items]

    return {"ok": True, "calendar_id": CALENDAR_ID, "tz": TIMEZONE, "events": out}

@router.get("/calendar/freebusy")
def admin_calendar_freebusy(
    x_admin_token: str | None = Header(default=None),
    date_str: str = Query(alias="date", description="YYYY-MM-DD"),
):
    """
    Devuelve ventanas ocupadas de GCAL para la fecha dada (YYYY-MM-DD).
    """
    _require_admin(x_admin_token)
    try:
        from datetime import time as _time
        import pytz
        d = _parse_date(date_str)
        tz = pytz.timezone(TIMEZONE)
        day_start = tz.localize(datetime.combine(d, _time(0, 0)))
        day_end = day_start + timedelta(days=1)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Parámetro 'date' inválido. Use YYYY-MM-DD.")

    svc = _get_service()
    body = {
        "timeMin": day_start.isoformat(),
        "timeMax": day_end.isoformat(),
        "timeZone": TIMEZONE,
        "items": [{"id": CALENDAR_ID}],
    }
    resp = svc.freebusy().query(body=body).execute()
    busy = resp.get("calendars", {}).get(CALENDAR_ID, {}).get("busy", [])
    return {"ok": True, "calendar_id": CALENDAR_ID, "tz": TIMEZONE, "date": date_str, "busy": busy}

@router.post("/calendar/test-create")
def admin_calendar_test_create(
    x_admin_token: str | None = Header(default=None),
    minutes_from_now: int = Query(default=2, ge=1, le=240),
    summary: str = Query(default="Ping de prueba"),
):
    """
    Crea un evento de prueba a N minutos desde ahora (en TZ local configurada).
    """
    _require_admin(x_admin_token)
    import pytz
    tz = pytz.timezone(TIMEZONE)
    now_local = datetime.now(tz)
    start_local = now_local + timedelta(minutes=minutes_from_now)
    start_local = start_local.replace(second=0, microsecond=0)

    ev_id = create_event(
        summary=summary,
        start_local=start_local,
        duration_min=30,
        location="(prueba)",
        description="Evento de prueba creado desde /admin/calendar/test-create",
    )
    return {
        "ok": True,
        "calendar_id": CALENDAR_ID,
        "tz": TIMEZONE,
        "event_id": ev_id,
        "start_local": start_local.isoformat(),
    }

# ──────────────────────────────────────────────────────────────────────────────
# Calendar: borrar por rango (recomendado en dev)
# ──────────────────────────────────────────────────────────────────────────────
@router.post("/calendar/clear_range")
def admin_calendar_clear_range(
    x_admin_token: str | None = Header(default=None),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD (por defecto: hoy-30d)"),
    end_date: Optional[str]   = Query(None, description="YYYY-MM-DD (por defecto: hoy+90d)"),
):
    """
    Elimina TODOS los eventos del calendario en el rango [start_date, end_date).
    Por defecto, borra del día (hoy-30) al (hoy+90).
    ⚠️ Úselo con cuidado. Solo para desarrollo.
    """
    _require_admin(x_admin_token)
    import pytz
    from datetime import time as _time

    today = datetime.utcnow().date()
    s_d = _parse_date(start_date) if start_date else (today - timedelta(days=30))
    e_d = _parse_date(end_date)   if end_date   else (today + timedelta(days=90))
    if e_d <= s_d:
        raise HTTPException(status_code=400, detail="end_date debe ser mayor que start_date.")

    tz = pytz.timezone(TIMEZONE)
    t_min = tz.localize(datetime.combine(s_d, _time(0, 0)))
    t_max = tz.localize(datetime.combine(e_d, _time(0, 0)))

    svc = _get_service()
    deleted_ids: List[str] = []
    page_token = None
    while True:
        resp = svc.events().list(
            calendarId=CALENDAR_ID,
            timeMin=t_min.isoformat(),
            timeMax=t_max.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=2500,
            pageToken=page_token
        ).execute()

        for ev in resp.get("items", []):
            ev_id = ev.get("id")
            try:
                delete_event(ev_id)
                deleted_ids.append(ev_id)
            except Exception:
                # Si falla delete_event, intentamos vía API directa
                try:
                    svc.events().delete(calendarId=CALENDAR_ID, eventId=ev_id).execute()
                    deleted_ids.append(ev_id)
                except Exception:
                    pass

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return {
        "ok": True,
        "calendar_id": CALENDAR_ID,
        "tz": TIMEZONE,
        "start_date": s_d.isoformat(),
        "end_date": e_d.isoformat(),
        "deleted_count": len(deleted_ids),
        "deleted_ids": deleted_ids,
    }

# ──────────────────────────────────────────────────────────────────────────────
# BD: utilidades (día específico y rango)
# ──────────────────────────────────────────────────────────────────────────────
@router.get("/db/appointments")
def admin_db_appointments(
    x_admin_token: str | None = Header(default=None),
    date: str = Query(..., description="YYYY-MM-DD"),
):
    """
    Lista las citas en BD para la fecha dada (horas guardadas en NAIVE LOCAL).
    Útil para explicar por qué un slot sale ocupado aunque GCAL esté libre.
    """
    _require_admin(x_admin_token)
    d = _parse_date(date)
    start = datetime(d.year, d.month, d.day, 0, 0, 0)
    end   = start + timedelta(days=1)

    items = []
    for db in _db():
        q = (
            db.query(models.Appointment, models.Patient)
            .join(models.Patient, models.Patient.id == models.Appointment.patient_id)
            .filter(models.Appointment.start_at >= start)
            .filter(models.Appointment.start_at < end)
            .order_by(models.Appointment.start_at.asc())
        )
        for ap, pa in q.all():
            items.append({
                "id": ap.id,
                "patient": pa.name or pa.contact,
                "start_at_naive_local": ap.start_at.isoformat() if ap.start_at else None,
                "status": str(ap.status),
                "event_id": ap.event_id,
            })
    return {"ok": True, "date": date, "count": len(items), "appointments": items}

@router.post("/db/clear_day")
def admin_db_clear_day(
    x_admin_token: str | None = Header(default=None),
    date: str = Query(..., description="YYYY-MM-DD"),
):
    """
    ⚠️ SOLO para pruebas: borra todas las citas de BD de ese día y,
    si tienen event_id, también borra el evento en Google Calendar.
    """
    _require_admin(x_admin_token)
    d = _parse_date(date)
    start = datetime(d.year, d.month, d.day, 0, 0, 0)
    end   = start + timedelta(days=1)

    deleted = []
    for db in _db():
        q = (
            db.query(models.Appointment)
            .filter(models.Appointment.start_at >= start)
            .filter(models.Appointment.start_at < end)
        )
        for ap in q.all():
            if ap.event_id:
                try:
                    delete_event(ap.event_id)
                except Exception:
                    pass
            deleted.append(ap.id)
            db.delete(ap)
        db.commit()
    return {"ok": True, "date": date, "deleted_ids": deleted}

@router.post("/db/clear_range")
def admin_db_clear_range(
    x_admin_token: str | None = Header(default=None),
    start_date: Optional[str] = Query(None, description="YYYY-MM-DD (por defecto: hoy-30d)"),
    end_date: Optional[str]   = Query(None, description="YYYY-MM-DD (por defecto: hoy+90d)"),
):
    """
    Borra TODAS las citas de la BD cuyo start_at (naive local) cae en el rango [start_date, end_date)
    y, si tienen event_id, también borra el evento en Google Calendar.
    """
    _require_admin(x_admin_token)
    today = datetime.utcnow().date()
    s_d = _parse_date(start_date) if start_date else (today - timedelta(days=30))
    e_d = _parse_date(end_date)   if end_date   else (today + timedelta(days=90))
    if e_d <= s_d:
        raise HTTPException(status_code=400, detail="end_date debe ser mayor que start_date.")

    start_dt = datetime(s_d.year, s_d.month, s_d.day, 0, 0, 0)
    end_dt   = datetime(e_d.year, e_d.month, e_d.day, 0, 0, 0)

    deleted = []
    for db in _db():
        q = (
            db.query(models.Appointment)
            .filter(models.Appointment.start_at >= start_dt)
            .filter(models.Appointment.start_at < end_dt)
        )
        for ap in q.all():
            if ap.event_id:
                try:
                    delete_event(ap.event_id)
                except Exception:
                    pass
            deleted.append(ap.id)
            db.delete(ap)
        db.commit()

    return {
        "ok": True,
        "start_date": s_d.isoformat(),
        "end_date": e_d.isoformat(),
        "deleted_ids": deleted,
        "deleted_count": len(deleted),
    }