# app/routers/admin.py
from __future__ import annotations
from fastapi import APIRouter, Header, HTTPException, Query
from datetime import datetime, date, timedelta

from ..config import settings

# Memoria del agente
try:
    from ..agent.agent_controller import _AGENT_SESSIONS  # type: ignore
except Exception:
    _AGENT_SESSIONS = {}

# Scheduling helpers
from ..services.scheduling import (
    CALENDAR_ID, TIMEZONE, list_upcoming_events, freebusy_for_date,
    _get_service, create_event, delete_event
)

router = APIRouter(tags=["admin"])

def _require_admin(x_admin_token: str | None) -> None:
    expected = (settings.ADMIN_TOKEN or "").strip()
    provided = (x_admin_token or "").strip()
    if not expected:
        raise HTTPException(status_code=403, detail="ADMIN_TOKEN no configurado")
    if provided != expected:
        raise HTTPException(status_code=401, detail="Token inválido")

@router.get("/admin/ping")
def admin_ping():
    return {"ok": True, "ts": datetime.utcnow().isoformat()}

@router.get("/admin/health")
def admin_health():
    return {
        "ok": True,
        "app": settings.APP_NAME,
        "env": settings.ENV,
        "tz": settings.TIMEZONE,
        "calendarId": CALENDAR_ID,
        "agent_sessions": len(_AGENT_SESSIONS) if isinstance(_AGENT_SESSIONS, dict) else "n/a",
        "ts": datetime.utcnow().isoformat(),
    }

@router.post("/admin/mem/clear")
def admin_clear_memory(x_admin_token: str | None = Header(default=None)):
    _require_admin(x_admin_token)
    try:
        if isinstance(_AGENT_SESSIONS, dict):
            _AGENT_SESSIONS.clear()
    except Exception:
        pass
    return {"ok": True, "message": "Memoria del agente limpiada."}

# ====== Calendar Diagnostics ======

@router.get("/admin/calendar/list")
def admin_calendar_list(x_admin_token: str | None = Header(default=None), limit: int = Query(10, ge=1, le=50)):
    _require_admin(x_admin_token)
    events = list_upcoming_events(limit=limit)
    return {"ok": True, "calendarId": CALENDAR_ID, "events": events}

@router.get("/admin/calendar/freebusy")
def admin_calendar_freebusy(
    x_admin_token: str | None = Header(default=None),
    date_str: str = Query(..., description="YYYY-MM-DD")
):
    _require_admin(x_admin_token)
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Formato de fecha inválido. Usa YYYY-MM-DD.")
    busy = freebusy_for_date(d)
    return {"ok": True, "calendarId": CALENDAR_ID, "date": d.isoformat(), "busy": busy}

@router.post("/admin/calendar/test-create")
def admin_calendar_test_create(
    x_admin_token: str | None = Header(default=None),
    summary: str = Query("Prueba rápida CLIEMED"),
    minutes_from_now: int = Query(5, ge=0, le=120)
):
    """
    Crea un evento de prueba de 5 minutos a partir de ahora + offset.
    Lo borra inmediatamente después de crearlo (para probar permisos/impersonación).
    """
    _require_admin(x_admin_token)
    tz = TIMEZONE
    now_local = datetime.now().astimezone()
    start_local = now_local + timedelta(minutes=minutes_from_now)
    start_local = start_local.replace(second=0, microsecond=0)
    try:
        eid = create_event(summary=summary, start_local=start_local, duration_min=5, location="", description="[test]")
        # Lo eliminamos para dejar limpio el calendario
        delete_event(eid)
        return {"ok": True, "did_create_and_delete": True, "event_id": eid, "start_local": start_local.isoformat(), "tz": tz}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Fallo al crear/borrar evento de prueba: {e}")