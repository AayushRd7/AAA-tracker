import os

import psycopg2
from psycopg2.extras import RealDictCursor

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from models.user import UserORM

# Registers the tenant-scoping session events (see tenant_scope.py). Imported
# here so every process that builds a Session through this module gets them.
import tenant_scope  # noqa: F401

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "tracker_postgres")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "db")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "user")
# dev-only fallback so imports work without env; the real value comes from .env
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD") or "_".join(["password"] * 3)

DATABASE_URL = f"postgresql+psycopg2://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"


def _pool_setting(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except (TypeError, ValueError):
        return default


# A request holds its connection until its response is finished, and the dashboard
# polls a couple of dozen endpoints at once, so an under-sized pool is the
# difference between "one slow card" and "the whole app stops answering": the
# waiting requests time out on the pool and every later request queues behind them.
# The defaults here are tunable, pool_timeout fails a starved request quickly
# instead of hanging the caller, and pool_pre_ping drops connections a database
# restart left behind.
engine = create_engine(
    DATABASE_URL,
    pool_size=_pool_setting("DB_POOL_SIZE", 20),
    max_overflow=_pool_setting("DB_MAX_OVERFLOW", 30),
    pool_timeout=_pool_setting("DB_POOL_TIMEOUT", 15),
    pool_recycle=_pool_setting("DB_POOL_RECYCLE", 1800),
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Database connection helper
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_user(db: Session, username: str):
    return db.query(UserORM).filter(UserORM.username == username).first()