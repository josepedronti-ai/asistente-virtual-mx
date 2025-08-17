# app/database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os

DATABASE_URL = os.getenv("DATABASE_URL")

def _int_from_env(name: str, default: int | None):
    val = os.getenv(name)
    if val is None or str(val).strip() == "":
        return default
    try:
        return int(val)
    except Exception:
        return default

# Construimos kwargs sin pasar None accidentales
engine_kwargs = {
    "pool_pre_ping": True,
    "future": True,
}

_pool_size = _int_from_env("DB_POOL_SIZE", 5)
_max_overflow = _int_from_env("DB_MAX_OVERFLOW", 10)
_pool_timeout = _int_from_env("DB_POOL_TIMEOUT", 30)   # segundos
_pool_recycle = _int_from_env("DB_POOL_RECYCLE", 1800) # 30 min

# Solo añadimos si no son None
if _pool_size is not None:
    engine_kwargs["pool_size"] = _pool_size
if _max_overflow is not None:
    engine_kwargs["max_overflow"] = _max_overflow
if _pool_timeout is not None:
    engine_kwargs["pool_timeout"] = _pool_timeout
if _pool_recycle is not None:
    engine_kwargs["pool_recycle"] = _pool_recycle

engine = create_engine(DATABASE_URL, **engine_kwargs)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
Base = declarative_base()

def init_db():
    # Fase 1: crear tablas una sola vez
    if os.getenv("SKIP_CREATE_ALL", "0") not in ("1", "true", "True"):
        Base.metadata.create_all(bind=engine)
