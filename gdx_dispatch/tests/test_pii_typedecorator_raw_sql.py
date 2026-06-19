"""Regression test for the S122-1b incident root cause.

S122-1b activated MASTER_ENCRYPTION_KEY on prod with Customer PII typed
EncryptedString. ``gdx_dispatch/routers/customers.py:226`` reads via
``db.execute(text("SELECT name, email FROM customers"))`` — raw SQL,
which bypasses ``EncryptedString.process_result_value``. The customer
list page rendered ``gAAAAA…`` ciphertext across 269 rows.

This test pins the bypass class so future contributors don't
reintroduce the same shape:
  1. With ``pii._FERNET`` configured, ORM writes encrypt.
  2. With ``pii._FERNET`` configured, an ORM read decrypts.
  3. With ``pii._FERNET`` configured, ``connection.execute(text(...))``
     returns the raw stored bytes — NOT the decoded value.

Therefore: any new EncryptedString column REQUIRES ORM-only access.
"""

from __future__ import annotations

import base64
import os
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import Column, String, create_engine, text
from sqlalchemy.orm import Session, declarative_base
from sqlalchemy.pool import StaticPool


@pytest.fixture
def encrypted_session(monkeypatch):
    """Build a tiny in-memory DB with one EncryptedString-typed table
    and a real Fernet instance plugged into ``gdx_dispatch.core.pii._FERNET``.
    """
    import gdx_dispatch.core.pii as pii_mod

    key = base64.urlsafe_b64encode(os.urandom(32))
    monkeypatch.setattr(pii_mod, "_FERNET", Fernet(key))

    Base = declarative_base()

    class Row(Base):
        __tablename__ = "rows"
        id = Column(String(36), primary_key=True)
        secret = Column(pii_mod.EncryptedString, nullable=False)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        yield session, Row


def test_orm_write_encrypts(encrypted_session):
    session, Row = encrypted_session
    row_id = str(uuid4())
    session.add(Row(id=row_id, secret="hello world"))
    session.commit()

    # Read the raw stored value via plain SQL — must be ciphertext.
    stored = session.execute(
        text("SELECT secret FROM rows WHERE id = :id"),
        {"id": row_id},
    ).scalar_one()
    assert stored != "hello world", (
        "EncryptedString.process_bind_param did not encrypt — pii._FERNET "
        "probably not patched into this test, or the TypeDecorator regressed."
    )
    assert stored.startswith("gAAAAA"), f"Expected Fernet ciphertext, got {stored!r}"


def test_orm_read_decrypts(encrypted_session):
    session, Row = encrypted_session
    row_id = str(uuid4())
    session.add(Row(id=row_id, secret="hello world"))
    session.commit()
    session.expire_all()

    fetched = session.get(Row, row_id)
    assert fetched is not None
    assert fetched.secret == "hello world"


def test_raw_sql_bypasses_typedecorator(encrypted_session):
    """⚠ S122-1b root cause (reader-side).

    ``connection.execute(text("SELECT secret FROM rows"))`` returns the
    raw column bytes — Fernet ciphertext — because the TypeDecorator only
    fires when SQLAlchemy knows the column is typed. Untyped text() does
    not consult the model registry.

    If this assertion ever flips (because someone "fixed" the
    TypeDecorator to inspect raw SQL), revisit
    ``gdx_dispatch/routers/customers.py`` etc. — the original incident might no
    longer be reproducible and the constraint that produced S122-1c
    Option A is gone.
    """
    session, Row = encrypted_session
    row_id = str(uuid4())
    session.add(Row(id=row_id, secret="hello world"))
    session.commit()
    session.expire_all()

    raw = session.execute(
        text("SELECT secret FROM rows WHERE id = :id"),
        {"id": row_id},
    ).scalar_one()

    assert raw != "hello world", (
        "TypeDecorator unexpectedly decrypted a raw text() select — "
        "the S122-1b bypass class no longer holds. See test docstring."
    )
    assert raw.startswith("gAAAAA"), f"Expected ciphertext, got {raw!r}"


def test_raw_sql_insert_bypasses_bind_param(encrypted_session):
    """⚠ Symmetric writer-side bypass (the auditor's S122-1c finding).

    ``connection.execute(text("INSERT INTO ... VALUES (:secret, ...)"))``
    binds the plain Python string straight into the column — it does NOT
    call ``process_bind_param``. So a raw-SQL writer against an
    EncryptedString column writes PLAINTEXT regardless of whether
    ``_FERNET`` is configured. A subsequent ORM read then calls
    ``process_result_value`` on plaintext and raises ``InvalidToken``.

    This is the writer-side mirror of the S122-1b Customer bug. The two
    raw-SQL writers identified at sprint close are
    ``gdx_dispatch/api/public_router.py:493`` and ``gdx_dispatch/core/public_api.py:395``,
    both inserting into ``webhook_endpoints.secret``. Those columns are
    now plain ``Text`` (S122-1c round-2 extended Option A) so the
    bypass has no observable effect today. The test still pins the
    SQLAlchemy contract so any future ``EncryptedString`` user gets a
    clean failure mode rather than silent prod corruption.
    """
    session, Row = encrypted_session
    row_id = str(uuid4())
    session.execute(
        text("INSERT INTO rows (id, secret) VALUES (:id, :secret)"),
        {"id": row_id, "secret": "hello world"},
    )
    session.commit()
    session.expire_all()

    raw = session.execute(
        text("SELECT secret FROM rows WHERE id = :id"),
        {"id": row_id},
    ).scalar_one()
    assert raw == "hello world", (
        "Raw INSERT unexpectedly encrypted via TypeDecorator — bypass "
        "class no longer holds; D-S122-1c-webhook-secret-bypass is moot."
    )

    # Post-S122-9 Slice 1, EncryptedString.process_result_value has an
    # InvalidToken→plaintext-passthrough safety net for the mixed-state
    # transition window. ORM read of a plaintext row returns the
    # plaintext as-is instead of raising.
    fetched = session.get(Row, row_id)
    assert fetched is not None
    assert fetched.secret == "hello world", (
        "EncryptedString.process_result_value lost its plaintext-passthrough "
        "on InvalidToken — mixed-state activation will break."
    )


def test_process_result_value_passthrough_explicit(monkeypatch):
    """Pin the InvalidToken passthrough at the TypeDecorator method
    level (not just observed through SQLAlchemy). Method takes raw bytes,
    returns them on InvalidToken. Pattern lifted from
    ``gdx_dispatch.core.database._decrypt_db_url:102``.

    Removed in the strict-mode step at the end of any future Option C
    rollout (after the detection query confirms zero plaintext rows for
    24 h).
    """
    import base64
    import os

    from cryptography.fernet import Fernet

    from gdx_dispatch.core import pii

    key = base64.urlsafe_b64encode(os.urandom(32))
    monkeypatch.setattr(pii, "_FERNET", Fernet(key))

    td = pii.EncryptedString()
    # Plaintext string — not a Fernet token. Must pass through unchanged.
    assert td.process_result_value("postgresql://user:pw@host/db", None) == (
        "postgresql://user:pw@host/db"
    )
    # Real ciphertext from this key — must decrypt cleanly.
    cipher = Fernet(key).encrypt(b"hello world").decode("utf-8")
    assert td.process_result_value(cipher, None) == "hello world"
    # None and empty pass through cheaply.
    assert td.process_result_value(None, None) is None
