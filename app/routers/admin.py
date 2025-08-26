# app/routers/admin.py
from __future__ import annotations
from fastapi import APIRouter, Header, HTTPException
from datetime import datetime

from ..config import settings

# Importamos la memoria del Agente para poder “limpiarla”
try:
    from ..agent.agent_controller import _AGENT_SESSIONS  # type: ignore
except Exception:
    _AGENT_SESSIONS = {}  # fallback por si no está disponible

router = APIRouter(tags=["admin"])

def _require_admin(x_admin_token: str | None) -> None:
    """
    Autorización simple por header: X-Admin-Token: <token>
    El token se toma de settings.ADMIN_TOKEN.
    """
    expected = (settings.ADMIN_TOKEN or "").strip()
    provided = (x_admin_token or "").strip()
    if not expected:
        # Si no hay token configurado, bloqueamos por seguridad
        raise HTTPException(status_code=403, detail="ADMIN_TOKEN no configurado")
    if provided != expected:
        raise HTTPException(status_code=401, detail="Token inválido")

@router.get("/admin/ping")
def admin_ping():
    return {"ok": True, "ts": datetime.utcnow().isoformat()}

@router.get("/admin/health")
def admin_health():
    return {
        "ok": True,
        "app": settings.APP_NAME,
        "env": settings.ENV,
        "tz": settings.TIMEZONE,
        "agent_sessions": len(_AGENT_SESSIONS) if isinstance(_AGENT_SESSIONS, dict) else "n/a",
        "ts": datetime.utcnow().isoformat(),
    }

@router.post("/admin/mem/clear")
def admin_clear_memory(x_admin_token: str | None = Header(default=None)):
    _require_admin(x_admin_token)
    try:
        if isinstance(_AGENT_SESSIONS, dict):
            _AGENT_SESSIONS.clear()
    except Exception:
        # Si por alguna razón no es dict o falla, devolvemos estado
        pass
    return {"ok": True, "message": "Memoria del agente limpiada."}