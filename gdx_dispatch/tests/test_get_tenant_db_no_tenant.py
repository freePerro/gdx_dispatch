"""Single-tenant contract for ``get_db``.

Pre-collapse, ``get_db`` resolved a per-tenant engine from
``request.state.tenant`` and raised ``HTTPException(400)`` when no tenant was
present (the platform-host SPA polling ``/api/notifications/count`` between
login and the tenant-subdomain redirect used to trigger this; a ``RuntimeError``
there became a 500 + pager noise, so the 400 was the fix).

The single-tenant collapse (genesis step 3) pins the data plane to ONE database,
so ``get_db`` no longer reads ``request.state`` at all — it yields a
session on the single application engine unconditionally. The cases that used to
400 (missing / ``None`` / empty-dict tenant) must now yield a usable session,
because the tenant on ``request.state`` is irrelevant to DB selection. These
tests assert that inverted contract so a regression back to per-tenant
resolution is caught.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import Request
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import get_db


def _drain(gen) -> Session:
    """Advance the dependency generator and return the yielded session."""
    db = next(gen)
    try:
        # Prove it is a live, queryable session on the single DB.
        from sqlalchemy import text

        db.execute(text("SELECT 1"))
    finally:
        gen.close()  # run the finally: block that closes the session
    return db


def test_get_db_yields_session_when_no_tenant_on_request_state():
    request = MagicMock(spec=Request)
    request.state = MagicMock(spec=[])  # no `tenant` attribute
    assert isinstance(_drain(get_db(request)), Session)


def test_get_db_yields_session_when_tenant_is_none():
    request = MagicMock(spec=Request)
    request.state = MagicMock()
    request.state.tenant = None
    assert isinstance(_drain(get_db(request)), Session)


def test_get_db_yields_session_when_tenant_is_empty_dict():
    request = MagicMock(spec=Request)
    request.state = MagicMock()
    request.state.tenant = {}  # falsy
    assert isinstance(_drain(get_db(request)), Session)


def test_get_db_yields_session_with_no_request_at_all():
    # The `request` param is retained only for Depends() signature
    # compatibility; the single-tenant implementation never reads it.
    assert isinstance(_drain(get_db()), Session)
