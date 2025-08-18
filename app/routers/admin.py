# app/routers/admin.py
from fastapi import APIRouter, Header, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional, List
from ..database import SessionLocal
from ..config import settings
from .. import models
from ..routers.webhooks import SESSION_CTX  # para limpiar memoria corta
from ..services.scheduling import delete_event

router = APIRouter(prefix="/admin", tags=["admin"])

def db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.delete("/reset-contact")
def reset_contact(
    contact: str = Query(..., description="Ej. whatsapp:+5214771297039"),
    x_admin_token: Optional[str] = Header(None),
    delete_calendar: bool = Query(True, description="Si True, elimina eventos confirmados del Calendar"),
):
    if not x_admin_token or x_admin_token != getattr(settings, "ADMIN_TOKEN", None):
        raise HTTPException(status_code=401, detail="Unauthorized")

    deleted = {"appointments": 0, "patients": 0, "message_log": 0, "calendar_deleted": 0}

    for db in db_session():
        # Buscar paciente
        patient = db.query(models.Patient).filter(models.Patient.contact == contact).first()
        if not patient:
            # limpiar contexto en cualquier caso
            SESSION_CTX.pop(contact, None)
            return {"ok": True, "detail": "No existe paciente con ese contacto", "deleted": deleted}

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
            deleted["message_log"] += db.query(models.MessageLog)\
                .filter(models.MessageLog.channel == "whatsapp")\
                .filter(models.MessageLog.payload.like(f"%{contact}%")).delete(synchronize_session=False)
        except Exception:
            pass

        # Borrar citas
        deleted["appointments"] += db.query(models.Appointment)\
            .filter(models.Appointment.patient_id == patient.id)\
            .delete(synchronize_session=False)

        # Borrar paciente
        deleted["patients"] += db.query(models.Patient)\
            .filter(models.Patient.id == patient.id)\
            .delete(synchronize_session=False)

        db.commit()

    # limpiar memoria corta (flujo por número)
    SESSION_CTX.pop(contact, None)

    return {"ok": True, "deleted": deleted}