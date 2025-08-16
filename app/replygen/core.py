# app/replygen/core.py
from __future__ import annotations
from datetime import datetime, date as date_cls
from typing import Any, Dict, Optional

def _stable_pick(options: list[str], seed: str) -> str:
    if not options:
        return ""
    idx = (abs(hash(seed)) % len(options))
    return options[idx]

def _fmt_dt(dt: Optional[datetime]) -> str:
    if not isinstance(dt, datetime):
        return ""
    return dt.strftime("%d/%m/%Y %H:%M")

def _fmt_date(d: Optional[datetime | date_cls]) -> str:
    if isinstance(d, datetime):
        return d.strftime("%d/%m/%Y")
    if isinstance(d, date_cls):
        return d.strftime("%d/%m/%Y")
    return ""

# ========= plantillas (tono MX, cálido, natural, sin emojis) =========

def _greet(state: Dict[str, Any], seed: str) -> str:
    nombre = (state.get("patient_name") or "").strip()
    inicio = _stable_pick(["Hola", "Buen día", "¿Qué tal"], seed)
    base = [
        f"{inicio}{' ' + nombre if nombre else ''}. ¿En qué te puedo ayudar?",
        f"{inicio}{' ' + nombre if nombre else ''}. ¿Agendamos, cambiamos o resolvemos una duda?",
        f"{inicio}{' ' + nombre if nombre else ''}. Dime, ¿qué necesitas?"
    ]
    return _stable_pick(base, seed + "g2")

def _ask_missing_date_or_time(state: Dict[str, Any], seed: str) -> str:
    last_date = state.get("last_date")
    if last_date:
        pref = _stable_pick(
            ["Para ese día sí tengo espacios.", "Ese día hay disponibilidad.", "Hay varias horas libres ese día."],
            seed
        )
        return f"{pref} ¿Qué hora te acomoda?"
    base = [
        "Claro. Para avanzar, ¿qué fecha te acomoda?",
        "Perfecto, empecemos por la fecha. ¿Cuál te queda mejor?",
        "De acuerdo. Dime primero la fecha y enseguida revisamos horarios."
    ]
    return _stable_pick(base, seed)

def _propose_slots_date(state: Dict[str, Any], seed: str) -> str:
    d = state.get("date_dt")
    fecha = _fmt_date(d) or "ese día"
    lst: list[str] = state.get("suggestions") or []
    header = _stable_pick(
        [f"Para el {fecha} tengo estos horarios:", f"El {fecha} puedo en:"],
        seed
    )
    bullets = "\n".join(lst[:12])
    cierre = _stable_pick(
        ["¿Cuál te acomoda mejor?", "¿Te queda alguno de esos?", "Dime cuál te funciona."],
        seed + "c"
    )
    return f"{header}\n{bullets}\n{cierre}"

def _time_unavailable_suggest_list(state: Dict[str, Any], seed: str) -> str:
    fecha = _fmt_date(state.get("date_dt")) or "ese día"
    lst: list[str] = state.get("suggestions") or []
    if not lst:
        return _no_availability_for_date(state, seed)
    header = _stable_pick(
        [f"Esa hora no la tengo. Para el {fecha} puedo:", f"Esa hora no está libre. Ese {fecha} tengo:"],
        seed
    )
    bullets = "\n".join(lst[:12])
    cierre = _stable_pick(
        ["¿Alguno te sirve?", "¿Cuál te queda mejor?", "Dime si alguno te acomoda."],
        seed + "c"
    )
    return f"{header}\n{bullets}\n{cierre}"

def _no_availability_for_date(state: Dict[str, Any], seed: str) -> str:
    d = state.get("date_dt")
    fecha = _fmt_date(d) or "ese día"
    base = [
        f"Para {fecha} ya no tengo espacios. ¿Vemos fechas cercanas?",
        f"{fecha} está lleno. Si gustas, te propongo días alrededor.",
        f"No tengo huecos el {fecha}. ¿Quieres que te comparta opciones cercanas?"
    ]
    return _stable_pick(base, seed)

def _ask_confirm_after_name(state: Dict[str, Any], seed: str) -> str:
    appt_dt: Optional[datetime] = state.get("appt_dt")
    patient_name: str = (state.get("patient_name") or "").strip()
    when = _fmt_dt(appt_dt) or "la fecha reservada"
    saludo = _stable_pick(["Gracias.", "Perfecto.", "De acuerdo."], seed)
    linea = _stable_pick(
        [f"{patient_name and patient_name + ', ' or ''}¿confirmamos {when} o prefieres moverla?",
         f"{patient_name and patient_name + ', ' or ''}¿la dejamos confirmada para {when} o quieres otro horario?"],
        seed + "l"
    )
    return f"{saludo} {linea}".strip()

def _confirm_done(state: Dict[str, Any], seed: str) -> str:
    dt = _fmt_dt(state.get("appt_dt"))
    nombre = (state.get("patient_name") or "").strip()
    n = f" de {nombre}" if nombre else ""
    base = [
        f"Listo: queda confirmada{n} para {dt}. ¿Algo más?",
        f"Perfecto, confirmada{n} para {dt}. ¿Te apoyo con algo más?",
        f"Bien, quedó confirmada{n} para {dt}. ¿Necesitas otra cosa?"
    ]
    return _stable_pick(base, seed)

def _booked_pending_name(state: Dict[str, Any], seed: str) -> str:
    dt = _fmt_dt(state.get("appt_dt"))
    base = [
        f"Reservé {dt}. ¿A nombre de quién la registramos? (Nombre y apellido)",
        f"Quedó reservado {dt}. ¿Cómo registramos el nombre del paciente?",
        f"Anoté {dt}. ¿Me compartes nombre y apellido para el registro?"
    ]
    return _stable_pick(base, seed)

def _booked_or_moved_ok(state: Dict[str, Any], seed: str) -> str:
    dt = _fmt_dt(state.get("appt_dt"))
    base = [
        f"Queda para {dt}. ¿Algo más?",
        f"Perfecto, agendado para {dt}. ¿Te ayudo con algo más?",
        f"Listo, quedó para {dt}. ¿Necesitas algo más?"
    ]
    return _stable_pick(base, seed)

def _canceled_ok(state: Dict[str, Any], seed: str) -> str:
    base = [
        "Listo, ya quedó cancelada. Si gustas, te propongo nuevos horarios.",
        "Hecho, la cancelé. Si necesitas reagendar, con gusto te ayudo.",
        "Cancelación realizada. ¿Quieres ver opciones para otra fecha?"
    ]
    return _stable_pick(base, seed)

def _prices(state: Dict[str, Any], seed: str) -> str:
    header = _stable_pick(
        ["Claro, te comparto los costos:", "Con gusto, aquí está la lista de precios:"],
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
    cierre = _stable_pick(
        ["¿Te reservo fecha?", "¿Deseas agendar con alguno de estos estudios?", "¿Tienes otra duda?"],
        seed + "p"
    )
    return f"{header}\n{cuerpo}\n{cierre}"

def _location(state: Dict[str, Any], seed: str) -> str:
    base = [
        "Estamos en CLIEMED: Av. Prof. Moisés Sáenz 1500, Leones, 64600, Monterrey, N.L.",
        "La consulta es en CLIEMED (Av. Prof. Moisés Sáenz 1500, Leones, 64600, Monterrey, N.L.).",
        "Atendemos en CLIEMED, Av. Prof. Moisés Sáenz 1500, Leones, 64600, Monterrey, N.L."
    ]
    return _stable_pick(base, seed)

def _goodbye(state: Dict[str, Any], seed: str) -> str:
    return _stable_pick(
        ["Perfecto. Cualquier cosa me escribes.",
         "De acuerdo. Si necesitas algo más, aquí estoy.",
         "Muy bien. Cuando gustes, me contactas."],
        seed
    )

def _fallback(state: Dict[str, Any], seed: str) -> str:
    base = [
        "Creo que me perdí un poco. ¿Quieres agendar, cambiar/confirmar o consultar costos/ubicación?",
        "Se me fue el detalle. ¿Te ayudo a agendar, reprogramar/confirmar o con costos/ubicación?"
    ]
    return _stable_pick(base, seed)

# ================= interfaz pública =================

def generate_reply(intent: str, user_text: str, state: Optional[Dict[str, Any]] = None) -> str:
    state = state or {}
    seed = f"{intent}|{user_text}|{state.get('patient_name','')}|{state.get('last_date','')}|{state.get('date_dt','')}"
    handlers = {
        "greet": _greet,
        "ask_missing_date_or_time": _ask_missing_date_or_time,
        "propose_slots_date": _propose_slots_date,
        "time_unavailable_suggest_list": _time_unavailable_suggest_list,
        "no_availability_for_date": _no_availability_for_date,
        "ask_confirm_after_name": _ask_confirm_after_name,
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
        return fn(state, seed).strip()
    except Exception:
        return _fallback(state, seed)