from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from datetime import date
from dateutil import parser as dtparser

from ..database import SessionLocal
from ..config import settings
from .. import models, schemas
from ..services.scheduling import available_slots
from ..services.notifications import send_confirmation

router = APIRouter(prefix="", tags=["appointments"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/slots", response_model=schemas.SlotsResponse)
def get_slots(date: str = Query(..., description="YYYY-MM-DD"), type: str = "consulta", db: Session = Depends(get_db)):
    try:
        d = dtparser.parse(date).date()
    except Exception:
        raise HTTPException(status_code=400, detail="Formato de fecha inválido. Usa YYYY-MM-DD.")
    slots = available_slots(db, d, settings.TIMEZONE)
    return schemas.SlotsResponse(slots=[s.isoformat() for s in slots])

@router.post("/book", response_model=schemas.BookResponse)
def book(req: schemas.BookRequest, db: Session = Depends(get_db)):
    patient = db.query(models.Patient).filter(models.Patient.contact == req.patient.contact).first()
    if not patient:
        patient = models.Patient(name=req.patient.name, contact=req.patient.contact, consent_messages=req.patient.consent_messages)
        db.add(patient)
        db.flush()

    day = req.start_at.date()
    slots = {s.isoformat() for s in available_slots(db, day, settings.TIMEZONE)}
    if req.start_at.isoformat() not in slots:
        raise HTTPException(status_code=409, detail="Horario no disponible")

    appt = models.Appointment(
        patient_id=patient.id,
        type=req.type,
        start_at=req.start_at,
        status=models.AppointmentStatus.reserved,
        channel=models.Channel.whatsapp
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)

    send_confirmation(patient.contact, req.start_at.isoformat())

    return schemas.BookResponse(appointment_id=appt.id, status=appt.status.value, start_at=appt.start_at)

@router.post("/reschedule")
def reschedule(req: schemas.RescheduleRequest, db: Session = Depends(get_db)):
    appt = db.query(models.Appointment).get(req.appointment_id)
    if not appt:
        raise HTTPException(status_code=404, detail="Cita no encontrada")
    day = req.new_start_at.date()
    slots = {s.isoformat() for s in available_slots(db, day, settings.TIMEZONE)}
    if req.new_start_at.isoformat() not in slots:
        raise HTTPException(status_code=409, detail="Nuevo horario no disponible")
    appt.start_at = req.new_start_at
    db.commit()
    return {"ok": True, "appointment_id": appt.id, "new_start_at": appt.start_at.isoformat()}

@router.post("/cancel")
def cancel(req: schemas.CancelRequest, db: Session = Depends(get_db)):
    appt = db.query(models.Appointment).get(req.appointment_id)
    if not appt:
        raise HTTPException(status_code=404, detail="Cita no encontrada")
    appt.status = models.AppointmentStatus.canceled
    db.commit()
    return {"ok": True, "appointment_id": appt.id, "status": appt.status.value}
