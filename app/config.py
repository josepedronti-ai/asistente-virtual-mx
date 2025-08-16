# app/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ===== App =====
    APP_NAME: str = "asistente_virtual"
    ENV: str = "dev"
    TIMEZONE: str = "America/Mexico_City"

    # ===== DB =====
    DATABASE_URL: str = "sqlite:///./asistente.db"

    # ===== Twilio =====
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    TWILIO_WHATSAPP_FROM: Optional[str] = None
    TWILIO_TEST_TO: Optional[str] = None

    # Simular envíos (no manda mensajes reales si True)
    DRY_RUN: bool = False

    # ===== Google Calendar (Service Account) =====
    # Lo que usa scheduling.py:
    GCAL_CALENDAR_ID: str = "primary"
    # Puede ser:
    #   - JSON completo en una sola línea (con \n escapados)
    #   - Ruta a archivo JSON (p.ej. "credentials.json")
    GCAL_SA_JSON: Optional[str] = None
    # Opcional (solo si usas Workspace y delegación)
    GCAL_IMPERSONATE_EMAIL: Optional[str] = None

    # ===== Horario de consultorio y slots =====
    CLINIC_START_HOUR: int = 9
    CLINIC_END_HOUR: int = 18
    SLOT_MINUTES: int = 30
    EVENT_DURATION_MIN: int = 30

    # ====== Compatibilidad hacia atrás (variables antiguas) ======
    # Si aún tienes GOOGLE_* en tu .env, los mapeamos automáticamente.
    GOOGLE_CALENDAR_ID: Optional[str] = None
    GOOGLE_CREDENTIALS_FILE: Optional[str] = None
    GOOGLE_TOKEN_FILE: Optional[str] = None
    GOOGLE_CREDENTIALS_JSON: Optional[str] = None

    def model_post_init(self, __context) -> None:
        """
        Backfill automático desde GOOGLE_* → GCAL_* si las nuevas no están definidas.
        Esto permite migrar sin romper la configuración existente.
        """
        # Calendar ID
        if (not self.GCAL_CALENDAR_ID or self.GCAL_CALENDAR_ID == "primary") and self.GOOGLE_CALENDAR_ID:
            self.GCAL_CALENDAR_ID = self.GOOGLE_CALENDAR_ID

        # Credenciales (prioridad: JSON en env > ruta a archivo)
        if not self.GCAL_SA_JSON:
            if self.GOOGLE_CREDENTIALS_JSON:
                self.GCAL_SA_JSON = self.GOOGLE_CREDENTIALS_JSON.strip()
            elif self.GOOGLE_CREDENTIALS_FILE:
                # Guardamos la RUTA, scheduling.py sabrá leer archivo o JSON
                p = Path(self.GOOGLE_CREDENTIALS_FILE)
                # No validamos existencia aquí; scheduling.py manejará errores mejor
                self.GCAL_SA_JSON = str(p)

        # No necesitamos GOOGLE_TOKEN_FILE con Service Account, se ignora.

settings = Settings()