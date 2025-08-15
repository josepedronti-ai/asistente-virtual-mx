import os, json, re
from openai import OpenAI

INTENTS = ["greet","book","reschedule","confirm","cancel","info","smalltalk","fallback"]

SYSTEM = (
    "Eres el asistente del Dr. Ontiveros (Cardiólogo intervencionista). "
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
    t = (texto or "").lower().strip()
    if not t:
        return {"intent":"fallback","entities":{},"reply":"🤔 No estoy seguro de haber entendido.\n¿Quieres **agendar**, **confirmar/reprogramar** o saber sobre **costos** y **ubicación**?"}

    # Hora explícita → tratamos como booking (para que no se vaya a 'info')
    if re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t) or re.search(r"\b([1-9]|1[0-2])\s*(am|pm)\b", t):
        return {"intent":"book","entities":{},"reply":"Entendido, ¿para qué **día** es esa hora?"}

    # Saludo inicial con menú formateado
    if any(k in t for k in ["hola","buenas","menu","menú","buenos días","buenos dias","buenas tardes","buenas noches"]):
        return {
            "intent":"greet",
            "entities":{},
            "reply":(
                "👋 ¡Hola! Soy el asistente del **Dr. Ontiveros** (Cardiólogo intervencionista 🫀).\n"
                "Cuéntame, ¿en qué puedo apoyarte hoy?\n\n"
                "• 📅 **Agendar** una cita\n"
                "• 🔄 **Confirmar** o **reprogramar**\n"
                "• 💳 **Costos** y 📍 **ubicación**\n"
                "• ❓ **Otras dudas** o información general."
            )
        }

    if any(k in t for k in ["agendar","cita","sacar cita","reservar","programar"]):
        return {"intent":"book","entities":{},"reply":"📅 ¡Perfecto! ¿Qué **día** te gustaría?"}
    if any(k in t for k in ["cambiar","reagendar","modificar","mover","reprogramar"]):
        return {"intent":"reschedule","entities":{},"reply":"Claro, ¿qué **día** te conviene y en qué turno *(mañana/tarde/noche)*?"}
    if any(k in t for k in ["confirmar","confirmo"]):
        return {"intent":"confirm","entities":{},"reply":"De acuerdo, intento confirmar tu cita…"}
    if any(k in t for k in ["cancelar","dar de baja"]):
        return {"intent":"cancel","entities":{},"reply":"Entendido, puedo cancelarla si existe. ¿Deseas agendar otra fecha?"}
    if any(k in t for k in ["costo","precio","ubicacion","ubicación","direccion","dirección","informacion","información","info"]):
        return {"intent":"info","entities":{},"reply":"Con gusto, ¿te interesa *costos* o *ubicación*?"}
    return {"intent":"fallback","entities":{},"reply":"🤔 No estoy seguro de haber entendido.\n¿Quieres **agendar**, **confirmar/reprogramar** o saber sobre **costos** y **ubicación**?"}

def _extract_json(s: str) -> str:
    if not s:
        return "{}"
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    m = re.search(r"\{.*\}", s, re.DOTALL)
    return m.group(0) if m else "{}"

def analizar(texto: str) -> dict:
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
        if not data.get("reply"):
            data["reply"] = "🤔 No estoy seguro de haber entendido.\n¿Quieres **agendar**, **confirmar/reprogramar** o saber sobre **costos** y **ubicación**?"
        return data

    except Exception as e:
        print(f"[NLU ERROR] {e}")
        return kw

def analizar_mensaje(texto: str) -> str:
    out = analizar(texto)
    return out.get("reply","¿Te ayudo a **programar**, **confirmar** o **reprogramar**?")