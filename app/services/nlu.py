# app/services/nlu.py
import os, json, re
from openai import OpenAI
from ..config import settings

INTENTS = ["greet","book","reschedule","confirm","cancel","info","smalltalk","fallback"]

SYSTEM = (
    "Eres el asistente del Dr. Ontiveros (Cardiólogo intervencionista). "
    "Tono humano, amable, claro y breve. "
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

    # Atajos vacíos
    if not t:
        return {
            "intent":"fallback",
            "entities":{},
            "reply":"¿Buscas **programar**, **confirmar/reprogramar** o **información** (costos, ubicación)?"
        }

    # Saludo
    if any(k in t for k in ["hola","buenas","menu","menú","buenos dias","buenas tardes","buenas noches"]):
        return {
            "intent":"greet",
            "entities":{},
            "reply":"👋 ¡Hola! Soy el asistente del Dr. Ontiveros (Cardiólogo intervencionista 🫀).\n"
                    "¿En qué puedo apoyarte hoy?\n"
                    "• **Programar** una cita\n"
                    "• **Confirmar** o **reprogramar**\n"
                    "• **Información** sobre costos o ubicación"
        }

    # Frases de agendar
    if any(k in t for k in ["agendar","cita","sacar cita","reservar","quiero agendar","programar"]):
        # Pequeña ayuda con 'mañana' para mejorar UX; la fecha real la resolverá el webhook
        if "mañana" in t or "manana" in t:
            return {
                "intent":"book",
                "entities":{"date":"mañana","time_pref":"","topic":""},
                "reply":"📅 Perfecto, ¿qué **día** te gustaría?"
            }
        return {
            "intent":"book",
            "entities":{},
            "reply":"📅 Perfecto, ¿qué **día** te gustaría?"
        }

    # Reagendar / cambiar
    if any(k in t for k in ["cambiar","reagendar","modificar","mover"]):
        return {
            "intent":"reschedule",
            "entities":{},
            "reply":"🔁 Claro, ¿qué **día** te conviene y en qué **turno** (mañana/tarde)?"
        }

    # Confirmar
    if any(k in t for k in ["confirmar","confirmo"]):
        return {
            "intent":"confirm",
            "entities":{},
            "reply":"👍 De acuerdo, intento confirmar tu cita."
        }

    # Cancelar
    if any(k in t for k in ["cancelar","dar de baja"]):
        return {
            "intent":"cancel",
            "entities":{},
            "reply":"🗑️ Entendido. Puedo cancelarla si existe. ¿Deseas agendar otra fecha?"
        }

    # Información (costos/ubicación)
    if any(k in t for k in ["costo","costos","precio","precios","ubicacion","ubicación","direccion","dirección","informacion","información","info"]):
        return {
            "intent":"info",
            "entities":{},
            "reply":"ℹ️ Con gusto. ¿Te interesa **costos** o **ubicación**?"
        }

    # Fallback
    return {
        "intent":"fallback",
        "entities":{},
        "reply":"¿Buscas **programar**, **confirmar/reprogramar** o **información** (costos, ubicación)?"
    }

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
    1) Atajo por palabras clave (rápido/barato)
    2) Si hay API key y USE_OPENAI_NLU=True, pedimos al modelo en modo JSON forzado
    3) Parse robusto con extracción de {...}
    """
    # 1) Atajo barato
    kw = _keyword_router(texto)
    if kw["intent"] != "fallback":
        return kw

    # 2) Sin API o con USE_OPENAI_NLU desactivado → nos quedamos con el atajo
    if not client or not getattr(settings, "USE_OPENAI_NLU", True):
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

        # Saneo mínimo
        if not isinstance(data, dict):
            raise ValueError("Formato no dict")
        if data.get("intent") not in INTENTS:
            data["intent"] = "fallback"
        if "entities" not in data or not isinstance(data["entities"], dict):
            data["entities"] = {}
        if not data.get("reply"):
            data["reply"] = "¿Buscas **programar**, **confirmar/reprogramar** o **información** (costos, ubicación)?"
        return data

    except Exception as e:
        print(f"[NLU ERROR] {e}")
        # Último recurso: usa el atajo por keywords
        return kw

# Compatibilidad vieja
def analizar_mensaje(texto: str) -> str:
    out = analizar(texto)
    return out.get("reply","¿Te ayudo a programar, confirmar o reprogramar?")