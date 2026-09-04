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

# ─── Single-tenant collapse compatibility shims ───────────────────────────
# The control/app plane and tenant plane are one database now, so the old
# multi-tenant symbols collapse to the single engine/URL. Several call sites
# still import these; without the shims they raise ImportError at call time:
#   - app_engine:        auth.core._db_verify_user — runs on EVERY authenticated
#                        request, so its absence 401s the entire API.
#   (control_engine, CONTROL_DATABASE_URL and the _decrypt_db_url identity
#   shim were removed 2026-09-03 with the SaaS-residue purge: their last
#   consumers were ops tools that walked a control-plane tenants table whose
#   db_url_enc column no longer exists.)
app_engine = engine


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
    """Fallback stub for get_tenant_db imported by modules prior to refactor.
    Must be a generator (not return one) so FastAPI's Depends() injects a Session."""
    yield from get_db(request)
