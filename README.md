# Asistente Virtual (unificado) – FastAPI + WhatsApp (Twilio) + Deploy 24/7

Incluye:
- Endpoints: `/slots`, `/book`, `/reschedule`, `/cancel`, `/waitlist/add`.
- WhatsApp real (Twilio) con webhook `/webhooks/whatsapp`.
- Recordatorios (APScheduler) de ejemplo a 24h.
- Config por variables de entorno (`.env.example`).
- Archivos de despliegue: `Dockerfile`, `Procfile`, `render.yaml`, `railway.json`.
- BD local SQLite por defecto; en producción usa Postgres via `DATABASE_URL`.

## Correr local (Mac)
```bash
python3 -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
# http://127.0.0.1:8000/docs
```

## Variables de entorno
```
APP_NAME=asistente_virtual
TIMEZONE=America/Mexico_City
ENV=dev

# Base de datos (local)
DATABASE_URL=sqlite:///./asistente.db
# Producción (ejemplo):
# DATABASE_URL=postgresql+psycopg2://USER:PASSWORD@HOST:5432/DBNAME

# Twilio (WhatsApp)
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=your_auth_token
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_TEST_TO=whatsapp:+5218112345678
```

## Render (rápido)
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Webhook Twilio: `https://TU-APP.onrender.com/webhooks/whatsapp`
