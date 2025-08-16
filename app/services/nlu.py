# app/services/nlu.py
import os, json, re
from typing import Dict
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # para entornos sin lib instalada

# Marca de build (útil para verificar qué versión corre en Render)
NLU_BUILD = "nlu-2025-08-15-r3"

INTENTS = ["greet","book","reschedule","confirm","cancel","info","smalltalk","fallback"]

# Sistema minimalista: SOLO devuelve JSON (sin tono/estilo)
SYSTEM = (
    "Eres un parser NLU para WhatsApp del consultorio del Dr. Ontiveros. "
    "Objetivo: clasificar INTENT y extraer ENTITIES. "
    "NO redactes respuestas; solo estructura. "
    "INTENTS permitidos: greet, book, reschedule, confirm, cancel, info, smalltalk, fallback. "
    "ENTITIES: {date, time, time_pref, name, topic}. "
    "Devuelve EXCLUSIVAMENTE JSON válido con la forma exacta:\n"
    "{"
    "\"intent\":\"greet|book|reschedule|confirm|cancel|info|smalltalk|fallback\","
    "\"entities\":{\"date\":\"\",\"time\":\"\",\"time_pref\":\"\",\"name\":\"\",\"topic\":\"\"},"
    "\"reply\":\"\""
    "}"
)

API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=API_KEY) if (API_KEY and OpenAI) else None

_WEEKDAYS = ["lunes","martes","miercoles","miércoles","jueves","viernes","sabado","sábado","domingo"]

_FAREWELLS = [
    "no gracias","no, gracias","gracias","muchas gracias","listo","es todo",
    "todo bien","está bien","esta bien","ninguno","ninguna","ok gracias","ok, gracias"
]

def _enrich_entities(texto: str, entities: dict) -> dict:
    """
    Asegura llaves esperadas y rellena date/time_pref/topic si el modelo no lo hizo.
    """
    t = (texto or "").lower().strip()
    ent = {"date":"", "time":"", "time_pref":"", "name":"", "topic":""}
    ent.update(entities or {})

    # date (relativas y días semana)
    if not ent.get("date"):
        if "pasado mañana" in t:
            ent["date"] = "pasado mañana"
        elif "mañana" in t:
            ent["date"] = "mañana"
        elif "hoy" in t:
            ent["date"] = "hoy"
        else:
            for wd in _WEEKDAYS:
                if re.search(rf"\b{wd}\b", t):
                    ent["date"] = wd
                    break

    # time_pref (franja, opcional)
    if not ent.get("time_pref"):
        if "tarde" in t:
            ent["time_pref"] = "tarde"
        elif "noche" in t:
            ent["time_pref"] = "noche"
        elif (" por la mañana" in (" " + t)) or (" en la mañana" in t):
            ent["time_pref"] = "manana"

    # topic (info)
    if not ent.get("topic"):
        if any(k in t for k in ["costo","costos","precio","precios"]):
            ent["topic"] = "costos"
        elif any(k in t for k in ["ubicacion","ubicación","direccion","dirección"]):
            ent["topic"] = "ubicacion"

    return ent

def _keyword_router(texto: str) -> Dict:
    """
    Router rápido/determinista sin OpenAI (o como primera pasada).
    Devuelve intent + entities, con reply siempre vacío (lo redacta replygen).
    """
    t = (texto or "").lower().strip()
    if not t:
        return {"intent":"fallback","entities":{},"reply":""}

    # Smalltalk / despedida rápida
    if any(x in t for x in _FAREWELLS):
        return {"intent":"smalltalk","entities":{},"reply":""}

    # Hora explícita → booking
    if re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t) or re.search(r"\b([1-9]|1[0-2])\s*(am|pm)\b", t):
        return {"intent":"book","entities":_enrich_entities(texto, {}),"reply":""}

    # Saludo inicial
    if any(k in t for k in ["hola","buenas","menu","menú","buenos días","buenos dias","buenas tardes","buenas noches"]):
        return {"intent":"greet","entities":{},"reply":""}

    # Construye entities base (date/time_pref/topic)
    entities = _enrich_entities(texto, {})

    if any(k in t for k in ["agendar","cita","sacar cita","reservar","programar"]):
        return {"intent":"book","entities":entities,"reply":""}
    if any(k in t for k in ["cambiar","reagendar","modificar","mover","reprogramar"]):
        return {"intent":"reschedule","entities":entities,"reply":""}
    if any(k in t for k in ["confirmar","confirmo"]):
        return {"intent":"confirm","entities":entities,"reply":""}
    if any(k in t for k in ["cancelar","dar de baja"]):
        return {"intent":"cancel","entities":entities,"reply":""}
    if any(k in t for k in ["costo","precio","costos","precios","ubicacion","ubicación","direccion","dirección","informacion","información","info"]):
        entities = _enrich_entities(texto, {"topic": entities.get("topic","")})
        return {"intent":"info","entities":entities,"reply":""}

    return {"intent":"fallback","entities":entities,"reply":""}

def _extract_json(s: str) -> str:
    if not s:
        return "{}"
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    m = re.search(r"\{.*\}", s, re.DOTALL)
    return m.group(0) if m else "{}"

def analizar(texto: str) -> dict:
    """
    1) Intent/entidades por router de palabras clave (rápido).
    2) Si hay OPENAI_API_KEY y sigue en fallback, llama al modelo para refinar.
    """
    kw = _keyword_router(texto)
    if kw["intent"] != "fallback":
        return kw

    if not client:
        return kw

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            response_format={"type":"json_object"},
            messages=[
                {"role":"system","content": SYSTEM},
                {"role":"user","content": texto or ""}
            ],
        )
        content = resp.choices[0].message.content or "{}"
        content = _extract_json(content)
        data = json.loads(content)

        if not isinstance(data, dict):
            raise ValueError("Formato no dict")
        if data.get("intent") not in INTENTS:
            data["intent"] = "fallback"
        if "entities" not in data or not isinstance(data["entities"], dict):
            data["entities"] = {}

        # Normalizamos entidades de forma segura
        data["entities"] = _enrich_entities(texto, data.get("entities", {}))
        # reply se mantiene vacío; la redacción la hace replygen
        data["reply"] = ""
        return data

    except Exception as e:
        print(f"[NLU ERROR] {e}")
        return kw

def analizar_mensaje(texto: str) -> str:
    # Solo para compatibilidad; ya no usamos replies aquí.
    out = analizar(texto)
    return out.get("reply","")