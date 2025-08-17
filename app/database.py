# app/database.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os

# Render → Environment → DATABASE_URL
DATABASE_URL = os.getenv("DATABASE_URL")

# Detecta si es SQLite para ajustar opciones (local dev)
connect_args = {"check_same_thread": False} if DATABASE_URL and DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=connect_args,
    pool_pre_ping=True,         # mantiene viva la conexión
    pool_size=5 if DATABASE_URL and DATABASE_URL.startswith("postgres") else None,
    max_overflow=5 if DATABASE_URL and DATABASE_URL.startswith("postgres") else None,
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
    """Crear tablas solo si no usamos Alembic todavía."""
    if os.getenv("SKIP_CREATE_ALL", "0") == "1":
        return
    Base.metadata.create_all(bind=engine)
