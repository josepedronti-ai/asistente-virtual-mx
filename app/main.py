# app/main.py
import os
import logging
from typing import Optional

from fastapi import FastAPI, Depends, Header, HTTPException, status

from .config import settings
from .database import init_db
from .jobs.scheduler import start_scheduler

# Routers
from .routers.appointments import router as appointments_router
from .routers.waitlist import router as waitlist_router
from .routers.webhooks import router as webhooks_router
from .routers.admin import router as admin_router

# ──────────────────────────────────────────────────────────────────────────────
# LOGGING (pensado para Render)
# Controla niveles con variables de entorno:
#   LOG_LEVEL, AGENT_LOG_LEVEL, SQLA_LOG_LEVEL, UVICORN_LOG_LEVEL
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

# Verbosidad del agente
logging.getLogger("app.agent.agent_controller").setLevel(
    getattr(logging, os.getenv("AGENT_LOG_LEVEL", "DEBUG"), logging.DEBUG)
)
# (Opcional) ruido de SQLAlchemy y Uvicorn en Render
logging.getLogger("sqlalchemy.engine").setLevel(
    getattr(logging, os.getenv("SQLA_LOG_LEVEL", "WARNING"), logging.WARNING)
)
logging.getLogger("uvicorn.error").setLevel(
    getattr(logging, os.getenv("UVICORN_LOG_LEVEL", "INFO"), logging.INFO)
)

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# FastAPI App
# ──────────────────────────────────────────────────────────────────────────────
app = FastAPI(title=settings.APP_NAME)

# Monta rutas
app.include_router(appointments_router)
app.include_router(waitlist_router)
app.include_router(webhooks_router)
app.include_router(admin_router, prefix="/admin")  # ← importante: el admin.py NO debe repetir /admin

# ──────────────────────────────────────────────────────────────────────────────
# Endpoints de DEBUG (para pruebas end-to-end en Render)
# Protégete con DEBUG_RESET_TOKEN en tus variables de entorno de Render.
# ──────────────────────────────────────────────────────────────────────────────
DEBUG_RESET_TOKEN = os.getenv("DEBUG_RESET_TOKEN")  # ej: 'supersecreto123'

def require_debug_token(x_debug_token: Optional[str] = Header(None)):
    if DEBUG_RESET_TOKEN and x_debug_token != DEBUG_RESET_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid debug token")
    return True

@app.post("/debug/reset_sessions")
def debug_reset_sessions(_: bool = Depends(require_debug_token)):
    """
    Borra la memoria corta del agente (_AGENT_SESSIONS) para simular una conversación nueva.
    Útil cuando quieres volver a ver la presentación inicial o reiniciar flujos.
    """
    try:
        from app.agent.agent_controller import _AGENT_SESSIONS
        _AGENT_SESSIONS.clear()
        logger.info("Memoria del agente reseteada vía /debug/reset_sessions")
        return {"ok": True, "message": "Memoria del agente reseteada."}
    except Exception as e:
        logger.exception("No se pudo resetear memoria del agente: %s", e)
        raise HTTPException(status_code=500, detail="No se pudo resetear la memoria del agente")

@app.get("/debug/agent_state/{contact}")
def debug_get_agent_state(contact: str, _: bool = Depends(require_debug_token)):
    """
    Inspecciona el estado temporal del agente para un contacto.
    No expone todo el historial; solo un resumen útil para debugging.
    """
    try:
        from app.agent.agent_controller import _AGENT_SESSIONS
        state = _AGENT_SESSIONS.get(contact)
        if not state:
            return {"ok": True, "found": False, "message": "Sin estado para ese contacto (o expiró el TTL)."}
        preview = {
            "ts": str(state.get("ts")),
            "greeted": bool(state.get("greeted", False)),
            "messages_count": len(state.get("messages", [])),
            "last_user_msg": next((m.get("content") for m in reversed(state.get("messages", [])) if m.get("role") == "user"), None),
            "last_assistant_msg": next((m.get("content") for m in reversed(state.get("messages", [])) if m.get("role") == "assistant"), None),
        }
        return {"ok": True, "found": True, "state": preview}
    except Exception as e:
        logger.exception("No se pudo leer agent_state: %s", e)
        raise HTTPException(status_code=500, detail="No se pudo leer el estado del agente")

# ──────────────────────────────────────────────────────────────────────────────
# Ciclo de vida
# ──────────────────────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_startup():
    init_db()
    start_scheduler()
    logger.info("Startup completo: %s (%s)", settings.APP_NAME, settings.ENV)

@app.get("/")
def root():
    return {"ok": True, "app": settings.APP_NAME, "env": settings.ENV}