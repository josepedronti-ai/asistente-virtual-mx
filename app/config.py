from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    APP_NAME: str = "asistente_virtual"
    ENV: str = "dev"
    TIMEZONE: str = "America/Mexico_City"

    DATABASE_URL: str = "sqlite:///./asistente.db"

    TWILIO_ACCOUNT_SID: str | None = None
    TWILIO_AUTH_TOKEN: str | None = None
    TWILIO_WHATSAPP_FROM: str | None = None
    TWILIO_TEST_TO: str | None = None

settings = Settings()
