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
    # TZ local del consultorio
    TIMEZONE: str = "America/Mexico_City"

    # ===== DB =====
    # En Render define DATABASE_URL con tu Postgres. Local puede caer a SQLite.
    DATABASE_URL: str = "sqlite:///./asistente.db"

    # Opciones de pool (puedes sobreescribir en Render → Environment)
    DB_POOL_SIZE: int = 2
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800  # 30 min

    # ===== Twilio =====
    TWILIO_ACCOUNT_SID: Optional[str] = None
    TWILIO_AUTH_TOKEN: Optional[str] = None
    TWILIO_WHATSAPP_FROM: Optional[str] = None
    TWILIO_TEST_TO: Optional[str] = None

    # Simulación (True = no envía mensajes reales)
    DRY_RUN: bool = False

    # ===== Google Calendar (Service Account) =====
    GCAL_CALENDAR_ID: str = "primary"
    # Puede ser JSON (una sola línea) o ruta a archivo .json
    GCAL_SA_JSON: Optional[str] = None
    # Opcional si usas delegación en Workspace
    GCAL_IMPERSONATE_EMAIL: Optional[str] = None

    # ===== Horario de consultorio y slots =====
    # Nombres “nuevos”
    CLINIC_OPEN_HOUR: int = 16    # 16:00
    CLINIC_CLOSE_HOUR: int = 22   # 22:00
    SLOT_MINUTES: int = 30
    EVENT_DURATION_MIN: int = 30

    # ===== Compatibilidad hacia atrás =====
    # Nombres “antiguos” por si tu código los usaba en algún punto
    CLINIC_START_HOUR: int = 16
    CLINIC_END_HOUR: int = 22

    # Compat con variables GOOGLE_*
    GOOGLE_CALENDAR_ID: Optional[str] = None
    GOOGLE_CREDENTIALS_FILE: Optional[str] = None
    GOOGLE_TOKEN_FILE: Optional[str] = None
    GOOGLE_CREDENTIALS_JSON: Optional[str] = None

    # ===== Admin =====
    ADMIN_TOKEN: Optional[str] = None

    def model_post_init(self, __context) -> None:
        """
        Backfill de:
          - GOOGLE_* → GCAL_*
          - START/END ↔ OPEN/CLOSE (ambos nombres quedan válidos)
        """
        # Calendar ID
        if (not self.GCAL_CALENDAR_ID or self.GCAL_CALENDAR_ID == "primary") and self.GOOGLE_CALENDAR_ID:
            self.GCAL_CALENDAR_ID = self.GOOGLE_CALENDAR_ID

        # Credenciales (prioridad: JSON en env > ruta a archivo)
        if not self.GCAL_SA_JSON:
            if self.GOOGLE_CREDENTIALS_JSON:
                self.GCAL_SA_JSON = self.GOOGLE_CREDENTIALS_JSON.strip()
            elif self.GOOGLE_CREDENTIALS_FILE:
                self.GCAL_SA_JSON = str(Path(self.GOOGLE_CREDENTIALS_FILE))

        # Alias horarios: si alguien cambió un set y no el otro, reflejamos a ambos
        if self.CLINIC_OPEN_HOUR != self.CLINIC_START_HOUR:
            # si difieren, homogeniza a OPEN/CLOSE
            self.CLINIC_START_HOUR = self.CLINIC_OPEN_HOUR
            self.CLINIC_END_HOUR = self.CLINIC_CLOSE_HOUR
        else:
            # si OPEN/CLOSE no fue seteado explícitamente pero START/END sí
            self.CLINIC_OPEN_HOUR = self.CLINIC_START_HOUR
            self.CLINIC_CLOSE_HOUR = self.CLINIC_END_HOUR


settings = Settings()