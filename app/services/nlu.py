# app/services/nlu.py
import os, json, re
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

def _keyword_router(texto: str) -> dict:
    """Router básico por palabras clave (funciona sin API)."""
    t = (texto or "").lower()
    if not t:
        return {"intent":"fallback","entities":{},"reply":"¿Te apoyo a agendar, confirmar o reprogramar?"}
    if any(k in t for k in ["hola","buenas","menu","menú"]):
        return {"intent":"greet","entities":{},"reply":"Hola 👋 ¿En qué te apoyo hoy?"}
    if any(k in t for k in ["agendar","cita","sacar cita","reservar"]):
        return {"intent":"book","entities":{},"reply":"Perfecto. ¿Qué día te gustaría? Puedes decirlo con tus palabras."}
    if any(k in t for k in ["cambiar","reagendar","modificar","mover"]):
        return {"intent":"reschedule","entities":{},"reply":"Claro. ¿Qué día te conviene y en qué horario (mañana/tarde)?"}
    if any(k in t for k in ["confirmar","confirmo"]):
        return {"intent":"confirm","entities":{},"reply":"De acuerdo. Intento confirmar tu cita, dame un momento."}
    if any(k in t for k in ["cancelar","dar de baja"]):
        return {"intent":"cancel","entities":{},"reply":"Entendido, puedo cancelarla si existe. ¿Deseas agendar otra fecha?"}
    if any(k in t for k in ["costo","precio","ubicacion","ubicación","direccion","dirección","preparacion","preparación","informacion","información","info"]):
        return {"intent":"info","entities":{},"reply":"Con gusto. ¿Te interesa costos, ubicación o preparación?"}
    return {"intent":"fallback","entities":{},"reply":"¿Buscas agendar, confirmar/reprogramar o información (costos, ubicación, preparación)?"}

def _extract_json(s: str) -> str:
    """Robusto: si el modelo devuelve texto con ruido, extrae el primer {...} JSON."""
    if not s:
        return "{}"
    # Intenta con todo el string primero
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    # Si venía con ```json ... ```
    m = re.search(r"\{.*\}", s, re.DOTALL)
    return m.group(0) if m else "{}"

def analizar(texto: str) -> dict:
    """
    Retorna dict: {"intent": str, "entities": dict, "reply": str}
    1) Atajo por palabras clave (rápido/barato)
    2) Si hay API key, pedimos al modelo en modo JSON forzado
    3) Parse robusto con extracción de {...}
    """
    # 1) Atajo barato: si ya detectamos claro por keywords, ahorramos token
    kw = _keyword_router(texto)
    # Si el atajo no es fallback, úsalo directo
    if kw["intent"] != "fallback":
        return kw

    # 2) Sin API → nos quedamos con el atajo
    if not client:
        return kw

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            # Forzamos JSON estricto
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
        if "entities" not in data or not isinstance(data["entities"], dict):
            data["entities"] = {}
        if not data.get("reply"):
            data["reply"] = "¿Buscas agendar, confirmar/reprogramar o información?"
        return data

    except Exception as e:
        print(f"[NLU ERROR] {e}")
        # Último recurso: usa el atajo por keywords
        return kw

# Compatibilidad vieja
def analizar_mensaje(texto: str) -> str:
    out = analizar(texto)
    return out.get("reply","¿Te ayudo a agendar, confirmar o reprogramar?")
