# app/replygen/core.py
from __future__ import annotations
from datetime import datetime
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

def _fmt_date(dt: Optional[datetime]) -> str:
    if not isinstance(dt, datetime):
        return ""
    return dt.strftime("%d/%m/%Y")

def _ask_confirm_after_name(state: Dict[str, Any], seed: str) -> str:
    appt_dt: Optional[datetime] = state.get("appt_dt")
    patient_name: str = (state.get("patient_name") or "").strip()
    when = _fmt_dt(appt_dt) or "la fecha reservada"
    saludo = _stable_pick(["", "Gracias.", "Perfecto.", "Gracias por el dato."], seed)
    nombre = f"{patient_name}, " if patient_name else ""
    linea2 = _stable_pick(
        [
            f"¿La dejamos confirmada para {when} o prefieres moverla?",
            f"¿Confirmamos {when} o quieres cambiarla?",
            f"¿Te late mantener {when} o te propongo otro horario?",
        ],
        seed + "l2",
    )
    if saludo:
        return f"{saludo} {nombre}{linea2}".strip()
    return f"{nombre}{linea2}".strip()

def _ask_missing_date_or_time(state: Dict[str, Any], seed: str) -> str:
    last_date = state.get("last_date")
    if last_date:
        prefijos = [
            "De ese día tengo espacio.",
            "Para ese día hay disponibilidad.",
            "Ese día puedo ofrecerte varias horas."
        ]
        p = _stable_pick(prefijos, seed)
        return f"{p} ¿Qué hora te viene mejor?"
    variantes = [
        "Claro. Para avanzar, ¿qué fecha te queda mejor?",
        "Perfecto. Empecemos por la fecha, ¿cuál te acomoda?",
        "De acuerdo. Dime primero la fecha y luego vemos horas.",
        "Bien. ¿Qué día te gustaría y después revisamos horarios?"
    ]
    return _stable_pick(variantes, seed)

def _greet(state: Dict[str, Any], seed: str) -> str:
    nombre = (state.get("patient_name") or "").strip()
    saludo = _stable_pick(["Hola", "Buen día", "¿Qué tal?", "Hola, cuéntame"], seed)
    pref = f"{saludo} {nombre}".strip()
    variantes = [
        f"{pref}. ¿En qué te ayudo?",
        f"{pref}. Dime, ¿qué necesitas?",
        f"{pref}. ¿Agendamos, cambiamos o resuelvo una duda?"
    ]
    return _stable_pick(variantes, seed + "g2")

def _no_availability_for_date(state: Dict[str, Any], seed: str) -> str:
    d: Optional[datetime] = state.get("date_dt")
    fecha = _fmt_date(d) or "ese día"
    variantes = [
        f"Para {fecha} ya no tengo espacios. ¿Te propongo fechas cercanas?",
        f"{fecha} está lleno. ¿Quieres que vea opciones alrededor?",
        f"No tengo huecos el {fecha}. Si gustas, reviso días cercanos."
    ]
    return _stable_pick(variantes, seed)

def _time_unavailable_suggest_list(state: Dict[str, Any], seed: str) -> str:
    fecha = _fmt_date(state.get("date_dt")) or "ese día"
    lista: list[str] = state.get("suggestions") or []
    if not lista:
        return _no_availability_for_date(state, seed)
    encabezos = [
        f"A esa hora no tengo lugar. Para {fecha} puedo:",
        f"Esa hora no la tengo. Ese {fecha} puedo ofrecerte:",
        f"Esa hora no está disponible. Ese {fecha} tengo:"
    ]
    header = _stable_pick(encabezos, seed)
    bullets = "\n".join(lista[:6])
    cierre = _stable_pick(
        ["¿Alguno te funciona?", "¿Cuál te viene mejor?", "Dime si alguno te acomoda."],
        seed + "c",
    )
    return f"{header}\n{bullets}\n{cierre}"

def _confirm_done(state: Dict[str, Any], seed: str) -> str:
    dt = _fmt_dt(state.get("appt_dt"))
    nombre = (state.get("patient_name") or "").strip()
    n = f" de {nombre}" if nombre else ""
    variantes = [
        f"Listo: queda confirmada{n} para {dt}. ¿Algo más?",
        f"Perfecto, confirmada{n} para {dt}. ¿Te ayudo en algo más?",
        f"Bien, quedó confirmada{n} para {dt}. ¿Necesitas otra cosa?"
    ]
    return _stable_pick(variantes, seed)

def _booked_pending_name(state: Dict[str, Any], seed: str) -> str:
    dt = _fmt_dt(state.get("appt_dt"))
    variantes = [
        f"Reservé {dt}. ¿A nombre de quién la registramos? (Nombre y apellido)",
        f"Quedó reservado {dt}. ¿Cómo registramos el nombre del paciente?",
        f"Anoté {dt}. ¿Me compartes nombre y apellido para el registro?"
    ]
    return _stable_pick(variantes, seed)

def _booked_or_moved_ok(state: Dict[str, Any], seed: str) -> str:
    dt = _fmt_dt(state.get("appt_dt"))
    variantes = [
        f"Queda para {dt}. ¿Algo más?",
        f"Perfecto, agendado para {dt}. ¿Necesitas otra cosa?",
        f"Listo, quedó para {dt}. ¿Te apoyo con algo más?"
    ]
    return _stable_pick(variantes, seed)

def _canceled_ok(state: Dict[str, Any], seed: str) -> str:
    variantes = [
        "Listo, ya quedó cancelada. Si gustas, te propongo nuevos horarios.",
        "Hecho, la cancelé. Si necesitas reagendar, te ayudo.",
        "Cancelación realizada. ¿Quieres ver opciones para otra fecha?"
    ]
    return _stable_pick(variantes, seed)

def _prices(state: Dict[str, Any], seed: str) -> str:
    encabezos = [
        "Claro, aquí está la lista de precios:",
        "Con gusto, te comparto los costos:",
        "Sí, te paso la lista de precios:"
    ]
    header = _stable_pick(encabezos, seed)
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
        ["¿Tienes alguna otra duda?", "¿Deseas agendar con alguno de estos estudios?", "¿Te reservo fecha?" ],
        seed + "p",
    )
    return f"{header}\n{cuerpo}\n{cierre}"

def _location(state: Dict[str, Any], seed: str) -> str:
    variantes = [
        "Estamos en CLIEMED, Av. Prof. Moisés Sáenz 1500, Leones, 64600, Monterrey, N.L.",
        "La consulta es en CLIEMED: Av. Prof. Moisés Sáenz 1500, Leones, 64600, Monterrey, N.L.",
        "Atendemos en CLIEMED (Av. Prof. Moisés Sáenz 1500, Leones, 64600, Monterrey, N.L.)."
    ]
    return _stable_pick(variantes, seed)

def _goodbye(state: Dict[str, Any], seed: str) -> str:
    variantes = [
        "Perfecto. Cualquier cosa me escribes.",
        "De acuerdo. Si necesitas algo más, estoy aquí.",
        "Muy bien. Cuando gustes, me contactas."
    ]
    return _stable_pick(variantes, seed)

def _fallback(state: Dict[str, Any], seed: str) -> str:
    variantes = [
        "Perdón, no alcancé a entenderte bien. ¿Buscabas agendar, cambiar/confirmar o información de costos/ubicación?",
        "Creo que me perdí un poco. ¿Quieres agendar, modificar/confirmar una cita o consultar costos/ubicación?",
        "Se me escapó el detalle. ¿Te ayudo a agendar, reprogramar/confirmar o con costos/ubicación?"
    ]
    return _stable_pick(variantes, seed)

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
        return fn(state, seed).strip()
    except Exception:
        return _fallback(state, seed)