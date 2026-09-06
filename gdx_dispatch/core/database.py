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

# ─── Older names, still imported ──────────────────────────────────────────
# `app_engine`, `tenant_context` and `get_tenant_db` are the one engine, a
# no-op context and `get_db` under the names call sites still use.
# `auth.core._db_verify_user` reads `app_engine` on EVERY authenticated
# request, so its absence 401s the entire API.
app_engine = engine


def get_db(request=None) -> Generator[Session, None, None]:
    """Dependency for injecting the database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def tenant_context():
    """No-op context under the name older modules import."""
    return nullcontext()

def get_tenant_db(request=None):
    """`get_db` under the name older modules import.
    Must be a generator (not return one) so FastAPI's Depends() injects a Session."""
    yield from get_db(request)
