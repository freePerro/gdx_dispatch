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
#   - control_engine:    core.sla_monitor (background task).
#   - _decrypt_db_url:   routers.bug_reports + backfill tools. DB-URL encryption
#                        was removed (db_url_enc dropped in migrations 081-083),
#                        so decryption is now identity.
#   - CONTROL_DATABASE_URL: a migration tool; falls back to the app DB URL.
app_engine = engine
control_engine = engine
CONTROL_DATABASE_URL = os.getenv("CONTROL_DATABASE_URL") or DATABASE_URL


def _decrypt_db_url(value):
    """No-op shim: DB-URL-at-rest encryption was removed in the single-tenant
    collapse (db_url_enc column dropped). The stored value is already plaintext."""
    return value


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
