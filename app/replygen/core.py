from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, Optional, List

# ==========================================================
#  ReplyGen (plantillas humanas, formales y consistentes)
# ==========================================================

def _fmt_date(dt: Optional[datetime]) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%d/%m/%Y")
    return ""

def _fmt_time(dt: Optional[datetime]) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%H:%M")
    return ""

def _fmt_dt(dt: Optional[datetime]) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%d/%m/%Y %H:%M")
    return ""

def _time_greeting(now: Optional[datetime] = None) -> str:
    h = (now or datetime.now()).hour
    if 6 <= h < 12:
        return "buenos días"
    if 12 <= h < 19:
        return "buenas tardes"
    return "buenas noches"

def _list_as_lines(items: List[str], limit: int = 12) -> str:
    return "\n".join(items[:limit])

# 1) Saludo
def _greet(state: Dict[str, Any]) -> str:
    saludo = _time_greeting(state.get("now"))
    return f"Hola, {saludo}. Soy el asistente del Dr. Ontiveros. ¿En qué puedo ayudarle hoy?"

# 2) Pedir fecha (Opción A acordada)
def _ask_date_strict(state: Dict[str, Any]) -> str:
    return "Claro, para agendar su cita, ¿me podría indicar la **fecha exacta** en formato **Día/Mes/Año**?"

# 3) Listar horarios de una fecha
def _list_slots_for_date(state: Dict[str, Any]) -> str:
    d: Optional[datetime] = state.get("date_dt")
    fecha = _fmt_date(d)
    slots: List[str] = state.get("slots_list") or []
    lista = _list_as_lines(slots, limit=12)
    return f"Perfecto. Para el {fecha} tengo disponibles los siguientes horarios:\n{lista}\n¿Cuál prefiere?"

# 4) Confirmar fecha y hora
def _confirm_date_time(state: Dict[str, Any]) -> str:
    d: Optional[datetime] = state.get("date_dt")
    t: Optional[datetime] = state.get("time_dt") or d  # si ya viene junto
    fecha = _fmt_date(d)
    hora  = _fmt_time(t)
    return f"Para confirmar, sería el 📅 {fecha} a las ⏰ {hora}. ¿Es correcto?"

# 5) Reservado OK
def _reserved_ok(state: Dict[str, Any]) -> str:
    dt = state.get("appt_dt")
    fecha = _fmt_date(dt)
    hora  = _fmt_time(dt)
    return (
        "Excelente, su cita ha quedado reservada.\n"
        f"📅 {fecha}\n"
        f"⏰ {hora}\n"
        "Le esperamos en el consultorio del Dr. Ontiveros. "
        "Si en algún momento necesita reprogramar o cancelar, con gusto le apoyo."
    )

# 6) Día lleno
def _day_full(state: Dict[str, Any]) -> str:
    d: Optional[datetime] = state.get("date_dt")
    fecha = _fmt_date(d) or "esa fecha"
    return f"Lamento informarle que para {fecha} ya no tengo espacios disponibles. ¿Desea que le sugiera días cercanos?"

# 7) Hora no disponible + sugerencias
def _time_unavailable(state: Dict[str, Any]) -> str:
    d: Optional[datetime] = state.get("date_dt")
    fecha = _fmt_date(d)
    slots: List[str] = state.get("slots_list") or []
    if not slots:
        return f"Lamentablemente ese horario ya está ocupado. ¿Desea que le sugiera otras horas para el {fecha}?"
    lista = _list_as_lines(slots, limit=12)
    return (
        "Lamentablemente ese horario ya está ocupado. "
        f"Para el {fecha} tengo disponibles los siguientes horarios:\n{lista}\n"
        "¿Desea que reserve alguno de ellos para usted?"
    )

# 8) Ya hay cita activa
def _has_active_appt(state: Dict[str, Any]) -> str:
    dt = state.get("appt_dt")
    fecha = _fmt_date(dt)
    hora  = _fmt_time(dt)
    return f"Parece que ya tiene una cita con nosotros para el 📅 {fecha} a las ⏰ {hora}. ¿Desea mantenerla o prefiere reprogramar?"

# 9) Precios
def _prices(state: Dict[str, Any]) -> str:
    cuerpo = (
        "• Consulta de primera vez: $1,200\n"
        "• Consulta subsecuente: $1,200\n"
        "• Valoración preoperatoria: $1,500\n"
        "• Ecocardiograma transtorácico: $3,000\n"
        "• Prueba de esfuerzo: $2,800\n"
        "• Holter 24 horas: $2,800\n"
        "• MAPA 24 h: $2,800"
    )
    return f"Claro, con gusto le comparto la lista de precios:\n{cuerpo}\n¿Le gustaría que agendemos?"

# 10) Despedida
def _goodbye(state: Dict[str, Any]) -> str:
    return "Perfecto, quedo a sus órdenes para cualquier duda o si desea agendar más adelante. Que tenga un excelente día."

# Fallback
def _fallback(state: Dict[str, Any]) -> str:
    return "Disculpe, ¿desea agendar, cambiar/confirmar una cita o consultar precios/ubicación?"

# ==========================
# Interfaz pública
# ==========================
_HANDLERS = {
    "greet": _greet,
    "ask_date_strict": _ask_date_strict,
    "list_slots_for_date": _list_slots_for_date,
    "confirm_date_time": _confirm_date_time,
    "reserved_ok": _reserved_ok,
    "day_full": _day_full,
    "time_unavailable": _time_unavailable,
    "has_active_appt": _has_active_appt,
    "prices": _prices,
    "goodbye": _goodbye,
    "fallback": _fallback,
}

def generate_reply(intent: str, state: Optional[Dict[str, Any]] = None) -> str:
    fn = _HANDLERS.get(intent, _fallback)
    try:
        return fn(state or {}).strip()
    except Exception:
        return _fallback(state or {})