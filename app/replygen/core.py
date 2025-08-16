# app/replygen/core.py
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, Optional
from .llm import polish

def _stable_pick(options: list[str], seed: str) -> str:
    if not options:
        return ""
    idx = (abs(hash(seed)) % len(options))
    return options[idx]

def _fmt_dt(dt: Optional[datetime]) -> str:
    return dt.strftime("%d/%m/%Y %H:%M") if isinstance(dt, datetime) else ""

def _fmt_date(dt: Optional[datetime]) -> str:
    return dt.strftime("%d/%m/%Y") if isinstance(dt, datetime) else ""

def _spread_slots_text(slots: list[datetime], limit: int = 12) -> str:
    """Distribuye horarios a lo largo del día para que no salgan puros de mañana."""
    if not slots:
        return ""
    if len(slots) <= limit:
        sel = slots
    else:
        step = max(1, len(slots)//limit)
        sel = [slots[i] for i in range(0, len(slots), step)][:limit]
    return "\n".join(s.strftime("%d/%m/%Y %H:%M") for s in sel)

# ---- Intents ----

def _ask_confirm_after_name(state: Dict[str, Any], seed: str) -> str:
    appt_dt = state.get("appt_dt")
    patient_name = (state.get("patient_name") or "").strip()
    when = _fmt_dt(appt_dt) or "la fecha reservada"
    saludo = _stable_pick(["", "Gracias.", "Perfecto.", "Gracias por el dato."], seed)
    nombre = f"{patient_name}, " if patient_name else ""
    l2 = _stable_pick(
        [f"¿La dejamos confirmada para {when} o prefieres moverla?",
         f"¿Confirmamos {when} o quieres cambiarla?",
         f"¿Te parece mantener {when} o buscamos otro horario?"],
        seed + "l2"
    )
    base = f"{saludo} {nombre}{l2}".strip()
    return polish(base, state.get("user_text",""))

def _ask_missing_date_or_time(state: Dict[str, Any], seed: str) -> str:
    last_date = state.get("last_date")
    if last_date:
        base = _stable_pick(
            ["De ese día hay disponibilidad. ¿Qué hora te viene mejor?",
             "Para ese día tengo espacios. ¿Qué hora prefieres?",
             "Ese día tengo varias horas. ¿Cuál te acomoda?"],
            seed
        )
    else:
        base = _stable_pick(
            ["Claro. Para avanzar, ¿qué fecha te queda mejor?",
             "Perfecto. Empecemos por la fecha, ¿cuál te acomoda?",
             "De acuerdo. Dime primero la fecha y luego vemos horarios."],
            seed
        )
    return polish(base, state.get("user_text",""))

def _greet(state: Dict[str, Any], seed: str) -> str:
    nombre = (state.get("patient_name") or "").strip()
    saludo = _stable_pick(["Hola", "Buen día", "¿Qué tal?", "Hola, cuéntame"], seed)
    base = _stable_pick(
        [f"{saludo} {nombre}. ¿En qué te ayudo?".strip(),
         f"{saludo} {nombre}. ¿Agendamos, cambiamos o resuelvo una duda?".strip(),
         f"{saludo} {nombre}. Dime, ¿qué necesitas?".strip()],
        seed+"g2"
    )
    return polish(base, state.get("user_text",""))

def _no_availability_for_date(state: Dict[str, Any], seed: str) -> str:
    fecha = _fmt_date(state.get("date_dt")) or "ese día"
    base = _stable_pick(
        [f"Para {fecha} ya no tengo espacios. ¿Te propongo fechas cercanas?",
         f"{fecha} está lleno. ¿Quieres que vea opciones alrededor?",
         f"No tengo huecos el {fecha}. Si quieres, reviso días cercanos."],
        seed
    )
    return polish(base, state.get("user_text",""))

def _time_unavailable_suggest_list(state: Dict[str, Any], seed: str) -> str:
    fecha = _fmt_date(state.get("date_dt")) or "ese día"
    lista: list[str] = state.get("suggestions") or []
    if not lista:
        return _no_availability_for_date(state, seed)
    header = _stable_pick(
        [f"A esa hora no tengo lugar. Para {fecha} puedo:",
         f"Esa hora no la tengo. Ese {fecha} puedo ofrecerte:",
         f"Esa hora no está disponible. Ese {fecha} tengo:"],
        seed
    )
    cierre = _stable_pick(["¿Alguno te funciona?", "¿Cuál te viene mejor?", "Dime si alguno te acomoda."], seed+"c")
    base = f"{header}\n" + "\n".join(lista[:12]) + f"\n{cierre}"
    return polish(base, state.get("user_text",""))

def _confirm_done(state: Dict[str, Any], seed: str) -> str:
    dt = _fmt_dt(state.get("appt_dt"))
    nombre = (state.get("patient_name") or "").strip()
    n = f" de {nombre}" if nombre else ""
    base = _stable_pick(
        [f"Perfecto, confirmada{n} para {dt}. ¿Te ayudo en algo más?",
         f"Listo: queda confirmada{n} para {dt}. ¿Algo más?",
         f"Bien, quedó confirmada{n} para {dt}. ¿Necesitas otra cosa?"],
        seed
    )
    return polish(base, state.get("user_text",""))

def _booked_pending_name(state: Dict[str, Any], seed: str) -> str:
    dt = _fmt_dt(state.get("appt_dt"))
    base = _stable_pick(
        [f"Reservé {dt}. ¿A nombre de quién la registramos? (Nombre y apellido)",
         f"Quedó reservado {dt}. ¿Cómo registramos el nombre del paciente?",
         f"Anoté {dt}. ¿Me compartes nombre y apellido para el registro?"],
        seed
    )
    return polish(base, state.get("user_text",""))

def _booked_or_moved_ok(state: Dict[str, Any], seed: str) -> str:
    dt = _fmt_dt(state.get("appt_dt"))
    base = _stable_pick(
        [f"Queda para {dt}. ¿Algo más?",
         f"Perfecto, agendado para {dt}. ¿Necesitas otra cosa?",
         f"Listo, quedó para {dt}. ¿Te apoyo con algo más?"],
        seed
    )
    return polish(base, state.get("user_text",""))

def _canceled_ok(state: Dict[str, Any], seed: str) -> str:
    base = _stable_pick(
        ["Listo, ya quedó cancelada. Si gustas, te propongo nuevos horarios.",
         "Hecho, la cancelé. Si necesitas reagendar, te ayudo.",
         "Cancelación realizada. ¿Quieres ver opciones para otra fecha?"],
        seed
    )
    return polish(base, state.get("user_text",""))

def _prices(state: Dict[str, Any], seed: str) -> str:
    header = _stable_pick(
        ["Claro, aquí está la lista de precios:",
         "Con gusto, te comparto los costos:",
         "Sí, te paso la lista de precios:"],
        seed
    )
    cuerpo = (
        "- Consulta primera vez: $1,200\n"
        "- Consulta subsecuente: $1,200\n"
        "- Valoración preoperatoria: $1,500\n"
        "- Ecocardiograma transtorácico: $3,000\n"
        "- Prueba de esfuerzo: $2,800\n"
        "- Holter 24h: $2,800\n"
        "- MAPA: $2,800"
    )
    cierre = _stable_pick(["¿Tienes alguna otra duda?", "¿Deseas agendar?", "¿Te reservo fecha?"], seed+"p")
    return polish(f"{header}\n{cuerpo}\n{cierre}", state.get("user_text",""))

def _location(state: Dict[str, Any], seed: str) -> str:
    base = _stable_pick(
        ["Estamos en CLIEMED, Av. Prof. Moisés Sáenz 1500, Leones, 64600, Monterrey, N.L.",
         "La consulta es en CLIEMED: Av. Prof. Moisés Sáenz 1500, Leones, 64600, Monterrey, N.L.",
         "Atendemos en CLIEMED (Av. Prof. Moisés Sáenz 1500, Leones, 64600, Monterrey, N.L.)."],
        seed
    )
    return polish(base, state.get("user_text",""))

def _goodbye(state: Dict[str, Any], seed: str) -> str:
    base = _stable_pick(
        ["Perfecto. Cualquier cosa me escribes.",
         "De acuerdo. Si necesitas algo más, estoy aquí.",
         "Muy bien. Cuando gustes, me contactas."],
        seed
    )
    return polish(base, state.get("user_text",""))

def _fallback(state: Dict[str, Any], seed: str) -> str:
    base = _stable_pick(
        ["Perdón, no alcancé a entenderte bien. ¿Buscabas agendar, cambiar/confirmar o información de costos/ubicación?",
         "Creo que me perdí un poco. ¿Quieres agendar, modificar/confirmar una cita o consultar costos/ubicación?",
         "Se me escapó el detalle. ¿Te ayudo a agendar, reprogramar/confirmar o con costos/ubicación?"],
        seed
    )
    return polish(base, state.get("user_text",""))

def generate_reply(intent: str, user_text: str, state: Optional[Dict[str, Any]] = None) -> str:
    state = state or {}
    seed = f"{intent}|{user_text}|{state.get('patient_name','')}|{state.get('last_date','')}|{state.get('date_dt','')}"
    handlers = {
        "greet": _greet,
        "ask_confirm_after_name": _ask_confirm_after_name,
        "ask_missing_date_or_time": _ask_missing_date_or_time,
        "no_availability_for_date": _no_availability_for_date,
        "time_unavailable_suggest_list": _time_unavailable_suggest_list,
        "confirm_done": _confirm_done,
        "booked_pending_name": _booked_pending_name,
        "booked_or_moved_ok": _booked_or_moved_ok,
        "canceled_ok": _canceled_ok,
        "prices": _prices,
        "location": _location,
        "goodbye": _goodbye,
        "fallback": _fallback,
    }
    fn = handlers.get(intent, _fallback)
    try:
        # Pasamos user_text en state para pulido contextual
        st = dict(state)
        st["user_text"] = user_text
        return fn(st, seed).strip()
    except Exception:
        return _fallback(state, seed)