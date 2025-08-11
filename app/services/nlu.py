# app/services/nlu.py
import os, json, re
from datetime import datetime, timedelta
from openai import OpenAI

INTENTS = ["greet","book","reschedule","confirm","cancel","info","smalltalk","fallback"]

SYSTEM = (
    "Eres el asistente del Dr. Ontiveros (cardiología intervencionista). "
    "Tono cercano, claro y breve. "
    "Debes devolver EXCLUSIVAMENTE un JSON válido con esta forma exacta:\n"
    '{"intent":"greet|book|reschedule|confirm|cancel|info|smalltalk|fallback",'
    '"entities":{"date":"","time_pref":"","topic":""},"reply":""}\n'
    "No incluyas comentarios, texto extra, ni bloques de código. "
    "Si falta información, pídela amablemente. No asumas que ya hay cita."
)

API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=API_KEY) if API_KEY else None

# Switch para activar/desactivar llamadas a OpenAI (útil si no hay saldo)
USE_OPENAI = os.getenv("USE_OPENAI_NLU", "true").lower() in ("1","true","yes")


def _keyword_router(texto: str) -> dict:
    """
    Router básico por palabras clave (funciona sin API).
    - Respuestas humanas (sin 'Puedes decirlo con tus palabras').
    - Entiende 'hoy', 'mañana', 'pasado mañana' -> entities.date (ISO).
    - Entiende 'por la mañana/tarde/noche' -> entities.time_pref.
    """
    t = (texto or "").lower().strip()

    entities = {}
    now = datetime.now()

    # fecha relativa (checar 'pasado mañana' antes que 'mañana')
    if "pasado mañana" in t:
        entities["date"] = (now + timedelta(days=2)).date().isoformat()
    elif "mañana" in t and "por la " not in t:
        entities["date"] = (now + timedelta(days=1)).date().isoformat()
    elif "hoy" in t:
        entities["date"] = now.date().isoformat()

    # preferencia de turno
    if "por la mañana" in t:
        entities["time_pref"] = "manana"
    elif "por la tarde" in t:
        entities["time_pref"] = "tarde"
    elif "por la noche" in t:
        entities["time_pref"] = "noche"

    if not t:
        return {"intent":"fallback","entities":entities,"reply":"¿Te apoyo a agendar, confirmar o reprogramar?"}

    if any(k in t for k in ["hola","buenas","menu","menú","buenos días","buenas tardes","buenas noches"]):
        return {"intent":"greet","entities":entities,"reply":"Hola 👋 ¿en qué te ayudo?"}

    if any(k in t for k in ["agendar","cita","sacar cita","reservar"]):
        return {"intent":"book","entities":entities,"reply":"¿Qué día te gustaría?"}

    if any(k in t for k in ["cambiar","reagendar","modificar","mover"]):
        return {"intent":"reschedule","entities":entities,"reply":"¿Qué día te conviene y en qué horario (mañana/tarde)?"}

    if any(k in t for k in ["confirmar","confirmo"]):
        return {"intent":"confirm","entities":entities,"reply":"De acuerdo, intento confirmar tu cita."}

    if any(k in t for k in ["cancelar","dar de baja"]):
        return {"intent":"cancel","entities":entities,"reply":"Entendido. Si existe una cita activa, puedo cancelarla. ¿Deseas agendar otra fecha?"}

    if any(k in t for k in ["costo","precio","ubicacion","ubicación","direccion","dirección","preparacion","preparación","informacion","información","info"]):
        return {"intent":"info","entities":entities,"reply":"¿Te interesa costos, ubicación o preparación?"}

    return {"intent":"fallback","entities":entities,"reply":"¿Buscas agendar, confirmar/reprogramar o información (costos, ubicación, preparación)?"}


def _extract_json(s: str) -> str:
    """Robusto: si el modelo devuelve texto con ruido, extrae el primer {...} JSON."""
    if not s:
        return "{}"
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    m = re.search(r"\{.*\}", s, re.DOTALL)
    return m.group(0) if m else "{}"


def analizar(texto: str) -> dict:
    """
    Retorna dict: {"intent": str, "entities": dict, "reply": str}
    1) Router por palabras (rápido/barato)
    2) (opcional) Llamada a OpenAI con JSON forzado
    3) Parse robusto con extracción de {...}
    """
    # 1) Atajo barato: si ya detectamos claro por keywords, úsalo directo
    kw = _keyword_router(texto)
    if kw["intent"] != "fallback":
        return kw

    # 2) Si no hay cliente o el switch está en off, nos quedamos con keywords
    if not client or not USE_OPENAI:
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

        # saneo mínimo
        if not isinstance(data, dict):
            raise ValueError("Formato no dict")
        if data.get("intent") not in INTENTS:
            data["intent"] = "fallback"
        ents = data.get("entities")
        if not isinstance(ents, dict):
            data["entities"] = {}
        if not data.get("reply"):
            data["reply"] = "¿Buscas agendar, confirmar/reprogramar o información?"
        return data

    except Exception as e:
        print(f"[NLU ERROR] {e}")
        return kw


# Compatibilidad vieja: devuelve solo el texto
def analizar_mensaje(texto: str) -> str:
    out = analizar(texto)
    return out.get("reply","¿Te ayudo a agendar, confirmar o reprogramar?")