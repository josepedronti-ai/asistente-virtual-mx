from datetime import datetime, date, time, timedelta
import zoneinfo
from typing import List
from sqlalchemy.orm import Session
from ..models import Appointment, AppointmentStatus

def working_slots_for_day(d: date, tzname: str) -> list[datetime]:
    tz = zoneinfo.ZoneInfo(tzname)
    blocks = [(time(9,0), time(14,0)), (time(16,0), time(19,0))]
    step = timedelta(minutes=30)
    out: list[datetime] = []
    for s,e in blocks:
        cur = datetime.combine(d, s, tzinfo=tz)
        end = datetime.combine(d, e, tzinfo=tz)
        while cur < end:
            out.append(cur)
            cur += step
    return out

def available_slots(db: Session, d: date, tzname: str) -> List[datetime]:
    tz = zoneinfo.ZoneInfo(tzname)
    start = datetime(d.year, d.month, d.day, 0, 0, tzinfo=tz)
    end = start + timedelta(days=1)
    busy = {a.start_at for a in db.query(Appointment).filter(
        Appointment.start_at >= start,
        Appointment.start_at < end,
        Appointment.status != AppointmentStatus.canceled
    ).all()}
    return [s for s in working_slots_for_day(d, tzname) if s not in busy]
