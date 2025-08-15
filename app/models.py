from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Integer, String, DateTime, Enum, ForeignKey, Boolean, Text
from datetime import datetime
import enum
from .database import Base

class Channel(str, enum.Enum):
    whatsapp = "whatsapp"
    phone = "phone"
    sms = "sms"
    email = "email"

class AppointmentStatus(str, enum.Enum):
    reserved = "reserved"
    confirmed = "confirmed"
    canceled = "canceled"
    no_show = "no_show"

class Patient(Base):
    __tablename__ = "patients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # ✅ Defaults y restricciones seguras
    name: Mapped[str] = mapped_column(
        String(200),
        index=True,
        nullable=False,
        default="Paciente"         # ← evita NULL en inserts
    )
    contact: Mapped[str] = mapped_column(
        String(120),
        index=True,
        unique=True,               # ← un paciente por contacto
        nullable=False             # ← obligatorio
    )  # ejemplo: whatsapp:+52...
    consent_messages: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True               # ← consentimiento por defecto
    )

    appointments = relationship("Appointment", back_populates="patient")

class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    patient_id: Mapped[int] = mapped_column(Integer, ForeignKey("patients.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(50), index=True)
    start_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[AppointmentStatus] = mapped_column(Enum(AppointmentStatus), default=AppointmentStatus.reserved)
    channel: Mapped[Channel] = mapped_column(Enum(Channel), default=Channel.whatsapp)

    patient = relationship("Patient", back_populates="appointments")

class MessageLog(Base):
    __tablename__ = "message_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    direction: Mapped[str] = mapped_column(String(10))  # in/out
    channel: Mapped[str] = mapped_column(String(20))
    template: Mapped[str] = mapped_column(String(120), default="")
    payload: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="queued")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
