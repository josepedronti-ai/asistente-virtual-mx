from pydantic import BaseModel
from datetime import datetime

class PatientIn(BaseModel):
    name: str
    contact: str
    consent_messages: bool = True

class BookRequest(BaseModel):
    patient: PatientIn
    type: str = "consulta"
    start_at: datetime

class BookResponse(BaseModel):
    appointment_id: int
    status: str
    start_at: datetime

class RescheduleRequest(BaseModel):
    appointment_id: int
    new_start_at: datetime

class CancelRequest(BaseModel):
    appointment_id: int
    reason: str | None = None

class SlotsResponse(BaseModel):
    slots: list[str]

class WaitlistAddRequest(BaseModel):
    patient: PatientIn
    preferences: str | None = None
