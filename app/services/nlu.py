# app/services/nlu.py
import os, json, re
from openai import OpenAI

INTENTS = ["greet","book","reschedule","confirm","cancel","info","smalltalk","fallback"]

SYSTEM = (
    "Eres el asistente del Dr. Ontiveros (Cardiólogo intervencionista 🫀). "
    "Tono cálido, profesional y humano. Respuestas breves y claras. "
    "Debes devolver EXCLUSIVAMENTE un JSON válido con esta forma exacta:\n"
    '{"intent":"greet|book|reschedule|confirm|cancel|info|smalltalk|fallback",'
    '"entities":{"date":"","time_pref":"","topic":""},"reply":""}\n'
    "No incluyas comentarios, texto extra, ni bloques de código. "
    "Si falta información, pídela amablemente. No asumas que ya hay una cita previa."
)

API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=API_KEY) if API_KEY else None


def _keyword_router(texto: str) -> dict:
    """
    Router rápido por palabras clave para ahorrar costos y latencia.
    En intents operativos, devolvemos reply=\"\" para que el copy lo controle webhooks.py.
    """
    t = (texto or "").lower().strip()
    if not t:
        return {
            "intent": "fallback",
            "entities": {},
            "reply": "¿Te ayudo a *programar*, *confirmar/reprogramar* o necesitas *información*? 🙂"
        }

    if any(k in t for k in ["hola","buenas","menu","menú","buenos días","buenas tardes","buenas noches"]):
        return {
            "intent": "greet",
            "entities": {},
            "reply": "¡Hola! 👋 ¿en qué te apoyo?"
        }

    if any(k in t for k in ["agendar","cita","sacar cita","reservar","programar"]):
        return {"intent": "book", "entities": {}, "reply": ""}

    if any(k in t for k in ["cambiar","reagendar","modificar","mover","reprogramar"]):
        return {"intent": "reschedule", "entities": {}, "reply": ""}

    if any(k in t for k in ["confirmar","confirmo"]):
        return {"intent": "confirm", "entities": {}, "reply": ""}

    if any(k in t for k in ["cancelar","dar de baja","anular"]):
        return {"intent": "cancel", "entities": {}, "reply": ""}

    if any(k in t for k in [
        "costo","precio","precios","ubicacion","ubicación","direccion","dirección",
        "informacion","información","info"
    ]):
        return {"intent": "info", "entities": {}, "reply": ""}

    return {
        "intent": "fallback",
        "entities": {},
        "reply": "¿Te gustaría *programar*, *confirmar/reprogramar* o saber *costos/ubicación*? 🙂"
    }


def _extract_json(s: str) -> str:
    """Si el modelo devuelve texto con ruido, extrae el primer bloque {...} JSON."""
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
    1) Intento por palabras clave (barato y rápido)
    2) Si aún es fallback y hay API, pedimos al modelo con JSON forzado
    3) Sanitizamos la salida y vaciamos reply en intents operativos
    """
    # 1) Atajo por keywords
    kw = _keyword_router(texto)
    if kw["intent"] != "fallback":
        return kw

    # 2) Si no hay API, nos quedamos con el atajo
    if not client:
        return kw

    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": texto or ""}
            ],
        )
        content = resp.choices[0].message.content or "{}"
        content = _extract_json(content)
        data = json.loads(content)

        # 3) Saneos mínimos
        if not isinstance(data, dict):
            raise ValueError("Formato no dict")
        if data.get("intent") not in INTENTS:
            data["intent"] = "fallback"
        if "entities" not in data or not isinstance(data["entities"], dict):
            data["entities"] = {}

        # Vaciar reply en intents operativos: el copy se controla desde webhooks.py
        if data.get("intent") in {"book","reschedule","confirm","cancel","info"}:
            data["reply"] = ""
        elif not data.get("reply"):
            data["reply"] = "¿Te ayudo a *programar*, *confirmar/reprogramar* o a resolver dudas de *costos/ubicación*? 🙂"

        return data

    except Exception as e:
        print(f"[NLU ERROR] {e}")
        return kw


# Compatibilidad con código antiguo que espera solo el texto
def analizar_mensaje(texto: str) -> str:
    out = analizar(texto)
    return out.get("reply", "¿Te ayudo a *programar*, *confirmar/reprogramar* o a resolver dudas de *costos/ubicación*? 🙂")