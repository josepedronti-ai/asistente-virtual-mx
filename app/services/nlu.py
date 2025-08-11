# app/services/nlu.py
import os, json
from openai import OpenAI

INTENTS = ["greet","book","reschedule","confirm","cancel","info","smalltalk","fallback"]

SYSTEM = (
    "Eres el asistente del Dr. Ontiveros (cardiología intervencionista). "
    "Respondes con tono cercano, claro y breve. "
    "Devuelve EXCLUSIVAMENTE un JSON válido: "
    '{"intent":"'+INTENTS[0]+'|'+INTENTS[1]+'|'+INTENTS[2]+'|'+INTENTS[3]+'|'+INTENTS[4]+'|'+INTENTS[5]+'|'+INTENTS[6]+'|'+INTENTS[7]+'",'
    '"entities":{}, "reply": "<texto>"} '
    "entities puede incluir: date (YYYY-MM-DD), time_pref (manana/tarde/noche), topic (costos/ubicacion/preparacion). "
    "Si falta info, pídela amablemente. No asumas que ya hay cita."
)

API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=API_KEY) if API_KEY else None

def analizar(texto: str) -> dict:
    """Retorna dict: {intent, entities, reply}. Seguro si no hay API key."""
    if not texto:
        return {"intent":"fallback","entities":{},"reply":"¿Te apoyo a agendar, confirmar o reprogramar?"}

    # Modo sin API (backup local)
    if not client:
        t = (texto or "").lower()
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

    # Modo con API (OpenAI)
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            messages=[
                {"role":"system","content": SYSTEM},
                {"role":"user","content": texto}
            ],
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
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
        return {"intent":"fallback","entities":{},"reply":"Estoy teniendo problemas para pensar la mejor respuesta. ¿Quieres agendar, confirmar o reprogramar?"}

# Compatibilidad hacia atrás si algún lugar llama aún a analizar_mensaje
def analizar_mensaje(texto: str) -> str:
    out = analizar(texto)
    return out.get("reply","¿Te ayudo a agendar, confirmar o reprogramar?")
