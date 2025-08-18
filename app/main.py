from fastapi import FastAPI
from .config import settings
from .database import init_db
from .routers import appointments, waitlist, webhooks
from .jobs.scheduler import start_scheduler
from .routers import webhooks, admin

app = FastAPI(title=settings.APP_NAME)

app.include_router(appointments.router)
app.include_router(waitlist.router)
app.include_router(webhooks.router)
app.include_router(admin.router)

@app.on_event("startup")
def on_startup():
    init_db()
    start_scheduler()

@app.get("/")
def root():
    return {"ok": True, "app": settings.APP_NAME, "env": settings.ENV}
