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

def tenant_context():
    """Fallback stub for tenant_context imported by modules prior to refactor."""
    return nullcontext()

def get_tenant_db(request=None):
    """Fallback stub for get_tenant_db imported by modules prior to refactor."""
    return get_db(request)
