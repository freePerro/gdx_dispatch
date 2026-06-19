"""Sprint 1.0 Phase B4 — users router create_user fix tests.

Regression coverage for H1 + H5:
  - H1: create_user used to INSERT without setting `username`, which is
    NOT NULL on prod Postgres (ORM declaration drifted to nullable=True;
    the column constraint still exists in the live schema). Silent
    IntegrityError left /users.Vue showing no error. Fixed: username
    auto-populates from email; IntegrityError → 400 with column in detail.
  - H5: a direct-DB insert that "succeeded" but persisted nothing
    (an earlier session ghost row). Fixed: read-your-write post-INSERT; 500 if
    not readable.

Tests use a mocked Session because the live path requires a PG engine
(SQLite UUID-binding friction) and the H1/H5 assertions are purely about
the handler's error-branch plumbing, not the commit path itself.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.requests import Request

from gdx_dispatch.routers.users import UserCreateIn, create_user


def _fake_request(tenant_id: str = "tenant-t1") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/users",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 0),
    }
    req = Request(scope)
    req.state.tenant = {"id": tenant_id}
    return req


def _admin_user() -> dict:
    return {"sub": "admin-1", "tenant_id": "tenant-t1", "role": "admin"}


def _fake_db_with_readback(readback_value) -> MagicMock:
    """Session stub: duplicate-check returns None; readback controlled."""
    exec_call = MagicMock()
    exec_call.first.return_value = None
    exec_call.scalar_one_or_none.return_value = readback_value
    db = MagicMock(spec=Session)
    db.execute.return_value = exec_call
    return db


def test_integrity_error_on_commit_returns_400_with_column() -> None:
    """H1 class: IntegrityError on username NOT NULL (or any constraint)
    converts to HTTPException(400) with the failing column name in detail."""
    db = _fake_db_with_readback(readback_value=None)
    fake_orig = MagicMock()
    fake_orig.diag.column_name = "username"
    db.commit.side_effect = IntegrityError("stmt", {}, fake_orig)

    payload = UserCreateIn(email="err@acme.com", name="Err User", password="CorrectHorse1!")
    with pytest.raises(HTTPException) as exc:
        create_user(payload, _fake_request(), _admin_user(), db)

    assert exc.value.status_code == 400
    assert "username" in exc.value.detail
    db.rollback.assert_called_once()


def test_integrity_error_without_diag_returns_400_generic() -> None:
    """IntegrityError without a diag.column_name still converts to 400
    with a generic message rather than leaking a stack trace."""
    db = _fake_db_with_readback(readback_value=None)
    db.commit.side_effect = IntegrityError("stmt", {}, Exception("no diag"))

    payload = UserCreateIn(email="err2@acme.com", name="Err2", password="CorrectHorse1!")
    with pytest.raises(HTTPException) as exc:
        create_user(payload, _fake_request(), _admin_user(), db)

    assert exc.value.status_code == 400
    assert "constraint" in exc.value.detail.lower()


def test_readback_none_after_commit_returns_500_ghost_row() -> None:
    """H5 class: commit succeeds but post-INSERT SELECT doesn't find the
    row — the 'ghost insert' from an earlier session. Now 500 instead of success."""
    db = _fake_db_with_readback(readback_value=None)  # not readable

    payload = UserCreateIn(email="ghost@acme.com", name="Ghost", password="CorrectHorse1!")
    with pytest.raises(HTTPException) as exc:
        create_user(payload, _fake_request(), _admin_user(), db)

    assert exc.value.status_code == 500
    assert "persist" in exc.value.detail.lower()


def test_username_is_populated_on_insert() -> None:
    """H1 fix: the User constructed for add() must carry a non-empty
    `username`. Asserted by intercepting db.add() and inspecting the
    User object before commit happens."""
    captured: list = []
    db = _fake_db_with_readback(readback_value="uid-1")
    db.add.side_effect = lambda u: captured.append(u)

    payload = UserCreateIn(email="jane@acme.com", name="Jane", password="CorrectHorse1!")
    try:
        create_user(payload, _fake_request(), _admin_user(), db)
    except Exception:  # _audit() may fail on the mock but we captured already
        pass

    # db.add is called for both the User and an AuditLog; filter to User.
    from gdx_dispatch.models.tenant_models import User
    users_added = [o for o in captured if isinstance(o, User)]
    assert len(users_added) == 1
    user_obj = users_added[0]
    assert user_obj.username and len(user_obj.username) > 0
    assert user_obj.username == "jane@acme.com"
    assert len(user_obj.username) <= 80  # varchar(80) ceiling


def test_username_truncated_to_80_chars_for_long_emails() -> None:
    captured: list = []
    db = _fake_db_with_readback(readback_value="uid-2")
    db.add.side_effect = lambda u: captured.append(u)

    long_email = "a" * 80 + "@example-long-domain.io"
    payload = UserCreateIn(email=long_email, name="Long", password="CorrectHorse1!")
    try:
        create_user(payload, _fake_request(), _admin_user(), db)
    except Exception:
        pass

    from gdx_dispatch.models.tenant_models import User
    users_added = [o for o in captured if isinstance(o, User)]
    assert len(users_added) == 1
    assert len(users_added[0].username) <= 80
