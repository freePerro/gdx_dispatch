"""A note has to record who wrote it.

Both note writers resolved the author's name off the auth dict —
``user.get("name") or user.get("full_name") or user.get("email")`` — against a
token that carries **sub, role, tenant_id, exp, jti, typ** and nothing else. So
the expression was always None, ``job_notes.author_name`` was NULL for every
note ever written, and JobDetailView rendered ``author_name || 'Unknown'``:
production had 11 notes, 11 author_ids, 0 names. Nobody could tell who wrote
anything on any job.

These tests use a token shaped like the REAL one (id only). That is the whole
point: a fixture that helpfully includes `name` passes against the broken code
and proves nothing, which is how this survived.
"""
from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.user_display import resolve_author_name
from gdx_dispatch.models.tenant_models import User


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    User.__table__.create(bind=engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _add_user(db: Session, **kw) -> User:
    user = User(id=uuid.uuid4(), company_id="tenant-a", **kw)
    db.add(user)
    db.commit()
    return user


def _real_token(user_id) -> dict:
    """The access token as it actually is: an id, a role, a tenant. No name."""
    return {"sub": str(user_id), "role": "technician", "tenant_id": "tenant-a"}


def test_resolves_from_a_real_token_which_has_no_name(db: Session) -> None:
    """The bug, reproduced. This is every request in production."""
    user = _add_user(db, name="Dana Rivera", email="tech@example.com")

    assert resolve_author_name(db, _real_token(user.id)) == "Dana Rivera"


def test_falls_back_through_the_name_columns(db: Session) -> None:
    user = _add_user(db, name=None, full_name="Full Name", email="x@example.com")
    assert resolve_author_name(db, _real_token(user.id)) == "Full Name"

    user2 = _add_user(db, name=None, full_name=None, username="drivera")
    assert resolve_author_name(db, _real_token(user2.id)) == "drivera"

    user3 = _add_user(db, name=None, full_name=None, username=None, email="e@x.com")
    assert resolve_author_name(db, _real_token(user3.id)) == "e@x.com"


def test_blank_name_is_not_a_name(db: Session) -> None:
    user = _add_user(db, name="   ", full_name="Real Name")
    assert resolve_author_name(db, _real_token(user.id)) == "Real Name"


def test_unknown_user_returns_none_not_a_placeholder(db: Session) -> None:
    """None, never "Unknown"/"System": the column is nullable and a fabricated
    name is indistinguishable from a real one downstream."""
    assert resolve_author_name(db, _real_token(uuid.uuid4())) is None


def test_non_uuid_author_id_returns_none_without_raising(db: Session) -> None:
    """author_id legitimately holds 'system' from service callers. Feeding that
    to a uuid column raises, and a note must never fail to save because we
    couldn't pretty up a name."""
    assert resolve_author_name(db, {"sub": "system", "role": "admin"}) is None
    assert resolve_author_name(db, {}) is None
    assert resolve_author_name(db, None) is None


def test_prefers_a_name_already_on_the_auth_dict(db: Session) -> None:
    """Free when a caller passes a fuller dict — no DB round trip."""
    assert resolve_author_name(db, {"sub": str(uuid.uuid4()), "name": "From Dict"}) == "From Dict"


def test_explicit_user_id_wins_over_the_dict(db: Session) -> None:
    """mobile.py resolves its user_id via _user_id() first and passes it in."""
    user = _add_user(db, name="Dana Rivera")
    assert resolve_author_name(db, {}, user_id=str(user.id)) == "Dana Rivera"
