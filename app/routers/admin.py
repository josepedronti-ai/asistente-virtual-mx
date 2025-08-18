# app/routers/admin.py
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from typing import Optional, List

from ..database import SessionLocal
from ..config import settings
from .. import models
from ..routers.webhooks import SESSION_CTX  # limpiar memoria corta
from ..services.scheduling import delete_event

# OJO: sin prefix aquí; lo montamos en main.py con prefix="/admin"
router = APIRouter(tags=["admin"])

def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def require_admin(x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token")):
    if not x_admin_token or x_admin_token != getattr(settings, "ADMIN_TOKEN", None):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

@router.get("/ping")
def ping(_: bool = Depends(require_admin)):
    return {"ok": True, "service": settings.APP_NAME}

@router.delete("/reset-contact")
def reset_contact(
    contact: str = Query(..., description="Ej. whatsapp:+5214771297039"),
    delete_calendar: bool = Query(True, description="Si True, elimina eventos confirmados del Calendar"),
    _: bool = Depends(require_admin),
    db: Session = Depends(db_session),
):
    deleted = {"appointments": 0, "patients": 0, "message_log": 0, "calendar_deleted": 0}

    # Buscar paciente
    patient = db.query(models.Patient).filter(models.Patient.contact == contact).first()
    if not patient:
        # limpiar contexto en cualquier caso
        SESSION_CTX.pop(contact, None)
        return JSONResponse({"ok": True, "detail": "No existe paciente con ese contacto", "deleted": deleted})

    # Eliminar eventos de Calendar si aplica
    if delete_calendar:
        appts: List[models.Appointment] = (
            db.query(models.Appointment)
            .filter(models.Appointment.patient_id == patient.id)
            .all()
        )
        for a in appts:
            if a.event_id:
                try:
                    delete_event(a.event_id)
                    deleted["calendar_deleted"] += 1
                except Exception:
                    pass  # no interrumpe

    # Borrar logs si usas tabla message_log
    try:
        deleted["message_log"] += (
            db.query(models.MessageLog)
            .filter(models.MessageLog.channel == "whatsapp")
            .filter(models.MessageLog.payload.like(f"%{contact}%"))
            .delete(synchronize_session=False)
        )
    except Exception:
        pass

    # Borrar citas
    deleted["appointments"] += (
        db.query(models.Appointment)
        .filter(models.Appointment.patient_id == patient.id)
        .delete(synchronize_session=False)
    )

    # Borrar paciente
    deleted["patients"] += (
        db.query(models.Patient)
        .filter(models.Patient.id == patient.id)
        .delete(synchronize_session=False)
    )

    db.commit()

    # limpiar memoria corta (flujo por número)
    SESSION_CTX.pop(contact, None)

    return JSONResponse({"ok": True, "deleted": deleted})

# --- Diagnóstico de Google Calendar ---
from datetime import datetime, timedelta
import pytz
from ..services.scheduling import _get_service, CALENDAR_ID, TIMEZONE

@router.get("/calendar/diag")
def calendar_diag(_: bool = Depends(require_admin)):
    """
    Verifica que podemos leer el calendario y cuenta los 'busy' de hoy.
    """
    out = {"can_read": False, "calendar_summary": None, "tz": TIMEZONE, "freebusy_count_today": 0}
    try:
        svc = _get_service()
        info = svc.calendars().get(calendarId=CALENDAR_ID).execute()
        out["can_read"] = True
        out["calendar_summary"] = info.get("summary")

        tz = pytz.timezone(TIMEZONE)
        start = tz.localize(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
        end = start + timedelta(days=1)

        fb = svc.freebusy().query(body={
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "timeZone": TIMEZONE,
            "items": [{"id": CALENDAR_ID}],
        }).execute()
        out["freebusy_count_today"] = len(fb["calendars"][CALENDAR_ID]["busy"])
        return {"ok": True, **out}
    except Exception as e:
        return {"ok": False, "error": str(e), **out}

@router.post("/calendar/mock")
def calendar_mock(_: bool = Depends(require_admin)):
    """
    Crea y borra un evento de prueba para validar permisos de escritura.
    """
    try:
        svc = _get_service()
        tz = pytz.timezone(TIMEZONE)
        start = tz.localize(datetime.now() + timedelta(minutes=10))
        end = start + timedelta(minutes=30)
        ev = svc.events().insert(calendarId=CALENDAR_ID, body={
            "summary": "TEST — Asistente Virtual",
            "start": {"dateTime": start.isoformat(), "timeZone": TIMEZONE},
            "end": {"dateTime": end.isoformat(), "timeZone": TIMEZONE},
            "description": "Evento de prueba creado por /admin/calendar/mock",
        }).execute()
        ev_id = ev["id"]
        svc.events().delete(calendarId=CALENDAR_ID, eventId=ev_id).execute()
        return {"ok": True, "created_then_deleted": True, "event_id": ev_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}