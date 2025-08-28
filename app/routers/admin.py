# app/routers/admin.py
from __future__ import annotations
from fastapi import APIRouter, Header, HTTPException, Query
from datetime import datetime, timedelta
from typing import Optional

from ..config import settings

# Memoria del agente
try:
    from ..agent.agent_controller import _AGENT_SESSIONS  # type: ignore
except Exception:
    _AGENT_SESSIONS = {}

# Herramientas de Calendar
from ..services.scheduling import (
    _get_service,
    TIMEZONE,
    CALENDAR_ID,
    create_event,
)

router = APIRouter(tags=["admin"])

def _require_admin(x_admin_token: str | None) -> None:
    expected = (settings.ADMIN_TOKEN or "").strip()
    provided = (x_admin_token or "").strip()
    if not expected:
        raise HTTPException(status_code=403, detail="ADMIN_TOKEN no configurado")
    if provided != expected:
        raise HTTPException(status_code=401, detail="Token inválido")

# ====== Básicos ======

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

# ====== Calendar: diagnóstico ======

@router.get("/calendar/list")
def admin_calendar_list(
    x_admin_token: str | None = Header(default=None),
    limit: int = Query(default=10, ge=1, le=50),
):
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
    _require_admin(x_admin_token)
    try:
        from datetime import time as _time
        import pytz
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
        tz = pytz.timezone(TIMEZONE)
        day_start = tz.localize(datetime.combine(d, _time(0, 0)))
        day_end = day_start + timedelta(days=1)
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