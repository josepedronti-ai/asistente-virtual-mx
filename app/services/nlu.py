# app/services/nlu.py
import os, json, re
from openai import OpenAI

# Marca de build (útil para verificar qué versión corre en Render)
NLU_BUILD = "nlu-2025-08-15-r2"

INTENTS = ["greet","book","reschedule","confirm","cancel","info","smalltalk","fallback"]

SYSTEM = (
    "Eres el asistente del Dr. Ontiveros (cardiología intervencionista). "
    "Tono cercano, claro y breve. "
    "Debes devolver EXCLUSIVAMENTE un JSON válido con esta forma exacta:\n"
    '{"intent":"greet|book|reschedule|confirm|cancel|info|smalltalk|fallback",'
    '"entities":{"date":"","time_pref":"","topic":""},"reply":""}\n'
    "Reglas adicionales:\n"
    "- Si el usuario menciona una fecha relativa (hoy/mañana/pasado mañana) o un día de la semana, "
    "  asume intención de reservar (intent=book) y pide la hora si falta.\n"
    "- Si detectas una hora explícita (hh:mm o h am/pm), también usa intent=book.\n"
    "- Si el usuario se despide o dice 'no gracias', responde con despedida amable (intent=smalltalk).\n"
    "No incluyas comentarios, texto extra, ni bloques de código. "
    "Si falta información, pídela amablemente. No asumas que ya hay cita."
)

API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=API_KEY) if API_KEY else None

_WEEKDAYS = ["lunes","martes","miercoles","miércoles","jueves","viernes","sabado","sábado","domingo"]
_FAREWELLS = (
    "no gracias","no, gracias","gracias","muchas gracias","listo","todo bien","está bien",
    "esta bien","ok gracias","ok, gracias","ya no","ya no gracias","es todo","ninguno","ninguna"
)

def _has_explicit_time(t: str) -> bool:
    return bool(
        re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t) or    # hh:mm
        re.search(r"\b([1-9]|1[0-2])\s*(am|pm)\b", t)         # h am/pm
    )

def _enrich_entities(texto: str, entities: dict) -> dict:
    """Asegura date/time_pref/topic básicos aunque el modelo falle."""
    t = (texto or "").lower().strip()
    ent = {"date":"", "time_pref":"", "topic":""}
    ent.update(entities or {})

    # date (relativas y días)
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

    # time_pref (franja)
    if not ent.get("time_pref"):
        if "tarde" in t:
            ent["time_pref"] = "tarde"
        elif "noche" in t:
            ent["time_pref"] = "noche"
        # Solo marcar "manana" (turno) si se menciona explícitamente la franja
        elif (" por la mañana" in (" " + t)) or (" en la mañana" in t):
            ent["time_pref"] = "manana"

    # topic (info)
    if not ent.get("topic"):
        if any(k in t for k in ["costo","costos","precio","precios"]):
            ent["topic"] = "costos"
        elif any(k in t for k in ["ubicacion","ubicación","direccion","dirección"]):
            ent["topic"] = "ubicacion"

    return ent

def _keyword_router(texto: str) -> dict:
    t = (texto or "").lower().strip()
    if not t:
        return {"intent":"fallback","entities":{"date":"","time_pref":"","topic":""},"reply":"¿Te apoyo a *programar*, *confirmar* o *reprogramar*?"}

    # Despedida/smalltalk amable
    if any(p in t for p in _FAREWELLS):
        return {
            "intent":"smalltalk",
            "entities":{"date":"","time_pref":"","topic":""},
            "reply":"💙 **¡Un gusto ayudarte!**\nCuando lo necesites, aquí estaré para apoyarte."
        }

    # Hora explícita → booking
    if _has_explicit_time(t):
        ents = _enrich_entities(texto, {})
        return {"intent":"book","entities":ents,"reply":"Entendido, ¿para qué **día** es esa hora?"}

    # Saludo inicial con menú
    if any(k in t for k in ["hola","buenas","menu","menú","buenos días","buenos dias","buenas tardes","buenas noches"]):
        return {
            "intent":"greet",
            "entities":{"date":"","time_pref":"","topic":""},
            "reply":(
                "👋 ¡Hola! Soy el asistente del **Dr. Ontiveros** (Cardiólogo intervencionista 🫀).\n"
                "Cuéntame, ¿en qué puedo apoyarte hoy?\n\n"
                "• 📅 **Agendar** una cita\n"
                "• 🔄 **Confirmar** o **reprogramar**\n"
                "• 💳 **Costos** y 📍 **ubicación**\n"
                "• ❓ **Otras dudas** o información general."
            )
        }

    # Construye entities base (date/time_pref/topic) desde el texto
    entities = _enrich_entities(texto, {})

    # Si hay fecha clara (aunque no diga "agendar"), intent=book y pedimos hora
    if entities.get("date"):
        return {
            "intent":"book",
            "entities":entities,
            "reply":"🕘 ¡Perfecto! Para ese día, ¿qué **hora** te viene mejor?"
        }

    # Verbos de agenda y demás
    if any(k in t for k in ["agendar","cita","sacar cita","reservar","programar"]):
        return {"intent":"book","entities":entities,"reply":"📅 ¡Perfecto! ¿Qué **día** te gustaría?"}
    if any(k in t for k in ["cambiar","reagendar","modificar","mover","reprogramar"]):
        return {"intent":"reschedule","entities":entities,"reply":"Claro, ¿qué **día** te conviene?"}
    if any(k in t for k in ["confirmar","confirmo"]):
        return {"intent":"confirm","entities":entities,"reply":"De acuerdo, intento confirmar tu cita…"}
    if any(k in t for k in ["cancelar","dar de baja"]):
        return {"intent":"cancel","entities":entities,"reply":"Entendido, puedo cancelarla si existe. ¿Deseas agendar otra fecha?"}
    if any(k in t for k in ["costo","precio","costos","precios","ubicacion","ubicación","direccion","dirección","informacion","información","info"]):
        entities = _enrich_entities(texto, {"topic": entities.get("topic","")})
        return {"intent":"info","entities":entities,"reply":"Con gusto, ¿te interesa *costos* o *ubicación*?"}

    # Fallback con entities enriquecidas (date/time_pref/topic) por si webhooks puede usarlas
    return {
        "intent":"fallback",
        "entities":entities,
        "reply":"¿Buscas **programar**, **confirmar/reprogramar** o **información** (costos, ubicación)?"
    }

def _extract_json(s: str) -> str:
    if not s:
        return "{}"
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    m = re.search(r"\{.*\}", s, re.DOTALL)
    return m.group(0) if m else "{}"

def analizar(texto: str) -> dict:
    # 1) Router económico primero
    kw = _keyword_router(texto)
    if kw["intent"] != "fallback":
        return kw

    # 2) Si no hay API, nos quedamos con lo anterior
    if not client:
        return kw

    # 3) Llamada a modelo con JSON forzado
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

        # Post-procesado seguro de entidades
        data["entities"] = _enrich_entities(texto, data.get("entities", {}))

        if not data.get("reply"):
            # Fallback de cortesía
            if data["intent"] == "book" and data["entities"].get("date"):
                data["reply"] = "🕘 ¡Perfecto! Para ese día, ¿qué **hora** te viene mejor?"
            else:
                data["reply"] = "¿Buscas **programar**, **confirmar/reprogramar** o **información**?"
        return data

    except Exception as e:
        print(f"[NLU ERROR] {e}")
        return kw

def analizar_mensaje(texto: str) -> str:
    out = analizar(texto)
    return out.get("reply","¿Te ayudo a *programar*, *confirmar* o *reprogramar*?")