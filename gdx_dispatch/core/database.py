from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import nullcontext

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

# ─── Single-tenant Database Setup ──────

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")
engine = create_engine(DATABASE_URL, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db(request=None) -> Generator[Session, None, None]:
    """Dependency for injecting the database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
