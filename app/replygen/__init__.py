# app/replygen/__init__.py
import os
from .core import generate_reply as _gen
from .llm import rewrite_like_human

_USE_LLM = bool(os.getenv("OPENAI_API_KEY"))

def generate_reply(intent: str, user_text: str, state=None) -> str:
    s = _gen(intent, user_text, state)
    return rewrite_like_human(s) if _USE_LLM else s