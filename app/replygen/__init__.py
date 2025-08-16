# app/replygen/__init__.py
from .core import generate_reply as _base_generate
from .llm import polish as _polish

def generate_reply(intent: str, user_text: str, state=None) -> str:
    """
    1) Genera respuesta estable y correcta (core)
    2) Si OPENAI_API_KEY está presente (y REPLYGEN_POLISH != 0), la pule para sonar 100% natural
    """
    base = _base_generate(intent=intent, user_text=user_text, state=state or {})
    return _polish(base)