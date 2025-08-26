# app/main.py
from fastapi import FastAPI
from .config import settings
from .database import init_db
from .jobs.scheduler import start_scheduler

# Routers
from .routers.appointments import router as appointments_router
from .routers.waitlist import router as waitlist_router
from .routers.webhooks import router as webhooks_router
from .routers.admin import router as admin_router

app = FastAPI(title=settings.APP_NAME)

# Monta rutas
app.include_router(appointments_router)
app.include_router(waitlist_router)
app.include_router(webhooks_router)
app.include_router(admin_router, prefix="/admin")  # <— importante

@app.on_event("startup")
def on_startup():
    init_db()
    start_scheduler()

@app.get("/")
def root():
    return {"ok": True, "app": settings.APP_NAME, "env": settings.ENV}
