from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

settings = get_settings()

# check_same_thread=False only matters for SQLite; harmless to compute regardless.
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(
    settings.database_url,
    connect_args=connect_args,
    future=True,
    pool_pre_ping=True,  # recycle stale connections (long-lived sync + IDLE sessions)
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    # Register models on Base before create_all.
    from . import models  # noqa: F401

    # pg_trgm must exist BEFORE create_all builds the GIN trigram index on
    # messages.search_text, so create the extension first.
    if settings.database_url.startswith("postgresql"):
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

    # Greenfield schema: models define everything, create_all makes it in one shot.
    # (No incremental migrations pre-1.0 — recreate the volume on schema changes.)
    Base.metadata.create_all(bind=engine)
