# app/scripts/test_calendar.py
from datetime import date, timedelta
from app.services.scheduling import available_slots, TIMEZONE, CALENDAR_ID

def show_slots(d: date):
    print(f"\n=== Slots para {d.strftime('%Y-%m-%d')} | TZ={TIMEZONE} | CAL={CALENDAR_ID} ===")
    try:
        slots = available_slots(None, d)
    except Exception as e:
        print("ERROR al consultar Google Calendar:", e)
        return
    if not slots:
        print("No hay slots disponibles.")
        return
    for s in slots:
        print(" -", s.strftime("%Y-%m-%d %H:%M"))

if __name__ == "__main__":
    hoy = date.today()
    show_slots(hoy)
    show_slots(hoy + timedelta(days=1))     # mañana
    show_slots(hoy + timedelta(days=2))     # pasado mañana
