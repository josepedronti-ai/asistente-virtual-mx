# app/database.py
from __future__ import annotations
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from .config import settings

# Usa siempre la URL desde settings (que a su vez lee .env)
DATABASE_URL: str | None = settings.DATABASE_URL
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL no está configurada (revisa tu .env).")

# Configura el engine según el tipo de base
if DATABASE_URL.startswith("sqlite"):
    # SQLite local (archivo)
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},  # requerido por SQLite en hilos
        pool_pre_ping=True,
        future=True,
    )
else:
    # Postgres u otros (producción/Render)
    engine = create_engine(
        DATABASE_URL,
        pool_size=getattr(settings, "DB_POOL_SIZE", 2),
        max_overflow=getattr(settings, "DB_MAX_OVERFLOW", 5),
        pool_timeout=getattr(settings, "DB_POOL_TIMEOUT", 30),
        pool_recycle=getattr(settings, "DB_POOL_RECYCLE", 1800),  # 30 min
        pool_pre_ping=True,
        future=True,
    )

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    future=True,
)

Base = declarative_base()

def init_db():
    """
    Crea las tablas si no existen. Importa modelos antes para que SQLAlchemy
    conozca todos los metadatos.
    """
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
