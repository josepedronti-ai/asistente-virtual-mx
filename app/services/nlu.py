# app/services/nlu.py
import os, json, re
from typing import Dict, Any, Optional
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

# Marca de build para identificar versión en logs
NLU_BUILD = "nlu-2025-08-16-hybrid-r2"

INTENTS = ["greet","book","reschedule","confirm","cancel","info","smalltalk","fallback"]

SYSTEM = (
    "Eres un clasificador NLU para un asistente médico en México. "
    "Devuelves SOLO un JSON válido con: intent, entities(date,time,time_pref,name,topic), reply. "
    "No agregues texto fuera del JSON. "
    "Usa trato de usted, tono profesional, sin emojis. "
    "date puede ser 'hoy', 'mañana', 'pasado mañana', un día de la semana en español, "
    "o una fecha como '18/08/2025', '18-08-2025', '18 agosto 2025' o '18 de agosto de 2025'. "
    "topic puede ser 'costos' o 'ubicacion'. "
    "reply es una frase breve y humana (usted, MX, sin emojis)."
)

_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=_API_KEY) if (_API_KEY and OpenAI) else None

_WEEKDAYS = ["lunes","martes","miercoles","miércoles","jueves","viernes","sabado","sábado","domingo"]

_FAREWELLS = [
    "no gracias","no, gracias","gracias","muchas gracias","listo","es todo",
    "todo bien","esta bien","está bien","ninguno","ninguna","ok gracias","ok, gracias"
]

_MESES = {
    "enero":1, "febrero":2, "marzo":3, "abril":4, "mayo":5, "junio":6,
    "julio":7, "agosto":8, "septiembre":9, "setiembre":9,
    "octubre":10, "noviembre":11, "diciembre":12
}

_NUMERIC_DATE_PAT = re.compile(r"\b([0-3]?\d)[/\-\.]([01]?\d)[/\-\.](\d{4})\b")
_TEXTUAL_DATE_PAT = re.compile(
    r"\b([0-3]?\d)\s*(?:de\s+)?([a-záéíóúñ]+)\s*(?:de\s+)?(\d{4})\b", re.IGNORECASE
)

def _enrich_entities(texto: str, entities: dict) -> dict:
    """
    Completa entities con valores derivados del texto cuando falten.
    - date: hoy/mañana/pasado, día de semana, dd/mm/yyyy, dd-mm-yyyy, dd mes yyyy, dd de mes de yyyy
    - topic: costos/ubicacion
    """
    t = (texto or "").lower().strip()
    ent = {"date":"", "time":"", "time_pref":"", "name":"", "topic":""}
    ent.update(entities or {})

    # ------ DATE ------
    if not ent.get("date"):
        # Relativas
        if "pasado mañana" in t:
            ent["date"] = "pasado mañana"
        elif "mañana" in t:
            ent["date"] = "mañana"
        elif "hoy" in t:
            ent["date"] = "hoy"
        else:
            # Días de la semana
            for wd in _WEEKDAYS:
                if re.search(rf"\b{wd}\b", t):
                    ent["date"] = wd
                    break

        # Numéricas (dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy)
        if not ent.get("date"):
            m = _NUMERIC_DATE_PAT.search(t)
            if m:
                # Conserva la forma original capturada por claridad aguas abajo
                ent["date"] = m.group(0)

        # Textuales: "18 agosto 2025" / "18 de agosto de 2025"
        if not ent.get("date"):
            m2 = _TEXTUAL_DATE_PAT.search(t)
            if m2:
                dia = m2.group(1)
                mes_txt = m2.group(2).lower()
                anio = m2.group(3)
                # Deja el texto tal cual; el parser downstream convierte
                ent["date"] = f"{dia} {mes_txt} {anio}"

    # ------ TOPIC ------
    if not ent.get("topic"):
        if any(k in t for k in ["costo","costos","precio","precios"]):
            ent["topic"] = "costos"
        elif any(k in t for k in ["ubicacion","ubicación","direccion","dirección"]):
            ent["topic"] = "ubicacion"

    return ent

def _keyword_router(texto: str) -> dict:
    t = (texto or "").lower().strip()
    if not t:
        return {"intent":"fallback","entities":{},"reply":"¿Le ayudo a programar, confirmar o reprogramar una cita?"}

    # Despedidas rápidas
    if any(x in t for x in _FAREWELLS):
        return {"intent":"smalltalk","entities":{},"reply":"Con todo gusto. Quedo al pendiente."}

    # Hora explícita → dirigir a reservar/reprogramar
    if re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t) or re.search(r"\b([1-9]|1[0-2])\s*(am|pm)\b", t):
        return {"intent":"book","entities":_enrich_entities(texto, {}),"reply":"Entendido. ¿Para qué fecha desea esa hora?"}

    # Saludo
    if any(k in t for k in ["hola","buenas","menu","menú","buenos dias","buenos días","buenas tardes","buenas noches"]):
        return {"intent":"greet","entities":{},"reply":"Hola, ¿en qué puedo ayudarle?"}

    # Construye entities (date/topic) a partir del texto
    entities = _enrich_entities(texto, {})

    # Intenciones básicas
    if any(k in t for k in ["agendar","cita","sacar cita","reservar","programar"]):
        return {"intent":"book","entities":entities,"reply":"De acuerdo. ¿Qué fecha desea?"}
    if any(k in t for k in ["cambiar","reagendar","modificar","mover","reprogramar"]):
        return {"intent":"reschedule","entities":entities,"reply":"Claro, ¿qué fecha prefiere?"}
    if any(k in t for k in ["confirmar","confirmo"]):
        return {"intent":"confirm","entities":entities,"reply":"Con gusto, intento confirmar su cita."}
    if any(k in t for k in ["cancelar","dar de baja"]):
        return {"intent":"cancel","entities":entities,"reply":"Entiendo, puedo cancelarla si existe. ¿Desea agendar otra fecha?"}
    if any(k in t for k in ["costo","precio","costos","precios","ubicacion","ubicación","direccion","dirección","informacion","información","info"]):
        return {"intent":"info","entities":entities,"reply":"¿Le interesa costos o ubicación?"}

    return {"intent":"fallback","entities":entities,"reply":"¿Le apoyo a programar, reprogramar/confirmar o con información de costos/ubicación?"}

def _extract_json(s: str) -> str:
    if not s:
        return "{}"
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    m = re.search(r"\{.*\}", s, re.DOTALL)
    return m.group(0) if m else "{}"

def analizar(texto: str) -> dict:
    # 1) Router rápido primero
    kw = _keyword_router(texto)
    if kw["intent"] != "fallback":
        return kw

    # 2) Si no hay API, nos quedamos con el router
    if not client:
        return kw

    # 3) Llamada al modelo con respuesta JSON forzada
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
            raise ValueError("bad format")
        if data.get("intent") not in INTENTS:
            data["intent"] = "fallback"
        if "entities" not in data or not isinstance(data["entities"], dict):
            data["entities"] = {}

        # Post-procesado seguro de entidades (añade fechas textuales/numéricas si faltaron)
        data["entities"] = _enrich_entities(texto, data.get("entities", {}))

        if not data.get("reply"):
            data["reply"] = "¿Desea programar, reprogramar/confirmar o consultar costos/ubicación?"
        return data
    except Exception as e:
        print(f"[NLU ERROR] {e}")
        return kw

def analizar_mensaje(texto: str) -> str:
    out = analizar(texto)
    return out.get("reply","¿Le apoyo a programar, confirmar o reprogramar?")