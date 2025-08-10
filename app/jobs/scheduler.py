from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import zoneinfo

from ..database import SessionLocal
from ..config import settings
from ..models import Appointment, AppointmentStatus
from ..services.notifications import send_reminder

def reminder_job():
    tz = zoneinfo.ZoneInfo(settings.TIMEZONE)
    now = datetime.now(tz)
    target = now + timedelta(hours=24)
    start = target.replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(minutes=59)

    db: Session = SessionLocal()
    try:
        appts = db.query(Appointment).filter(
            Appointment.start_at >= start,
            Appointment.start_at <= end,
            Appointment.status != AppointmentStatus.canceled
        ).all()
        for a in appts:
            contact = a.patient.contact if a.patient else None
            if contact:
                send_reminder(contact, a.start_at.isoformat(), when="24h")
    finally:
        db.close()

def start_scheduler():
    scheduler = BackgroundScheduler(timezone=settings.TIMEZONE)
    scheduler.add_job(reminder_job, CronTrigger(minute=0))  # cada hora
    scheduler.start()
    return scheduler
