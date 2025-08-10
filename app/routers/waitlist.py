from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..database import SessionLocal
from .. import models, schemas

router = APIRouter(prefix="", tags=["waitlist"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/waitlist/add")
def waitlist_add(req: schemas.WaitlistAddRequest, db: Session = Depends(get_db)):
    patient = db.query(models.Patient).filter(models.Patient.contact == req.patient.contact).first()
    if not patient:
        patient = models.Patient(name=req.patient.name, contact=req.patient.contact, consent_messages=req.patient.consent_messages)
        db.add(patient); db.flush()
    db.add(models.MessageLog(direction="out", channel="whatsapp", template="waitlist_add", payload=req.preferences or "", status="queued"))
    db.commit()
    return {"ok": True}
