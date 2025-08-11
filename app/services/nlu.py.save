from openai import OpenAI
import os

# El SDK toma la API key de la variable OPENAI_API_KEY automáticamente.
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM = (
    "Eres el asistente virtual del Dr. Ontiveros. "
    "Responde con tono amable, breve y claro. "
    "Si el paciente quiere agendar o cambiar cita, pídeles fecha (AAAA-MM-DD) "
    "y menciona que puedes proponer horarios."
)

def analizar_mensaje(texto: str) -> str:
    """
    Devuelve una respuesta natural para el paciente.
    """
    try:
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": texto}
            ],
            temperature=0.3,
        )
        return (resp.choices[0].message.content or "¿Te ayudo a agendar, confirmar o cambiar tu cita?").strip()
    except Exception as e:
        # Respuesta segura si hay algún problema de red o credenciales
        return "Tuve un problema para pensar la respuesta. ¿Quieres agendar, confirmar o cambiar tu cita?"
