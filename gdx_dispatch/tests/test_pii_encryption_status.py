"""Single-source-of-truth tests for ``pii.encryption_status()``.

Auditor's round-3 finding (S122-1c): three independent surfaces emitted
"is PII encrypted at rest?" with conflicting answers. The boot gate,
SOC2 evidence collector, and schema-drift checker now consume one helper.
Auditor's round-4 finding (this commit): the first cut cached the scan
at boot — the cache hid lazy-imported models. Removed.

These tests pin the contract.
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import Column, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from gdx_dispatch.core import pii


def test_known_encrypted_columns_today():
    """The encrypted-column inventory. webhook + integration secrets (slice 2),
    ``customers.address`` (slice 3), and ``vendors.{account_number,tax_id}``
    (vendor-PII). ``customers.{name,email,phone}`` stay plaintext for
    substring-LIKE search support; they re-encrypt when
    D-S122-9-customer-search-encryption lands a search architecture.
    """
    status = pii.encryption_status()
    observed = {(c.table, c.column) for c in status.columns}
    expected = {
        ("webhook_endpoints", "secret"),
        ("integration_configs", "secret"),
        ("customers", "address"),
        ("vendors", "account_number"),
        ("vendors", "tax_id"),
    }
    assert observed == expected, (
        f"Encrypted-column inventory drift. "
        f"Observed={observed}, expected={expected}. "
        f"If you added/removed an EncryptedString column, update both."
    )


def test_status_key_loaded_reflects_fernet(monkeypatch):
    monkeypatch.setattr(pii, "_FERNET", None)
    assert pii.encryption_status().key_loaded is False

    monkeypatch.setattr(pii, "_FERNET", object())
    assert pii.encryption_status().key_loaded is True


def test_columns_actually_encrypted_requires_both(monkeypatch):
    """``columns_actually_encrypted`` is the richer "data is encrypted
    at rest" attestation. It must require BOTH a loaded key AND at
    least one declared column. The legacy ``key_loaded`` bool tracks
    only the key — keep them distinct so dashboards graphing the legacy
    flag stay continuous.

    Key unset + columns present → False (today's pre-activation state).
    Key set + columns present → True (post-activation state).
    Key set + zero columns → False (no-live-consumers, no attestation).
    """
    # Force key OFF, scan finds the 2 real columns → False (no key).
    monkeypatch.setattr(pii, "_FERNET", None)
    status = pii.encryption_status()
    assert status.key_loaded is False
    assert len(status.columns) >= 1
    assert status.columns_actually_encrypted is False

    # Force key ON, scan finds the 2 real columns → True.
    monkeypatch.setattr(pii, "_FERNET", object())
    status = pii.encryption_status()
    assert status.key_loaded is True
    assert len(status.columns) >= 1
    assert status.columns_actually_encrypted is True


def test_scanner_picks_up_encryptedstring_column(monkeypatch):
    """Fixture base with one EncryptedString column — the scanner must
    find it AND record the model's module path. Pins the SQLAlchemy 2.0
    ``registry.mappers`` API contract."""

    class _FixtureBase(DeclarativeBase):
        pass

    class _RowWithSecret(_FixtureBase):
        __tablename__ = "fixture_rows_a"
        id: Mapped[str] = mapped_column(String(36), primary_key=True)
        secret: Mapped[str] = mapped_column(pii.EncryptedString, nullable=False)

    monkeypatch.setattr(pii, "_FERNET", object())
    status = pii.encryption_status(bases=[_FixtureBase])

    assert len(status.columns) == 1
    col = status.columns[0]
    # Plane is "other" for arbitrary bases; module path is the truth.
    assert col.plane in ("tenant", "control", "other")
    assert col.module.endswith("test_pii_encryption_status")
    assert col.table == "fixture_rows_a"
    assert col.column == "secret"
    assert col.type_name == "EncryptedString"
    assert status.columns_actually_encrypted is True


def test_scanner_no_caching_across_calls(monkeypatch):
    """Cache removal (auditor round-4 finding): every call re-scans so
    that lazy-imported models registered after boot become visible to
    SOC2 evidence collected at request time, not invisible because the
    boot-time snapshot was frozen.
    """

    class _FixtureBase(DeclarativeBase):
        pass

    # First scan against an empty base — finds nothing.
    assert pii.encryption_status(bases=[_FixtureBase]).columns == ()

    # Register a model on the same base, then re-scan — must find it.
    class _RowLazyRegistered(_FixtureBase):
        __tablename__ = "fixture_rows_b"
        id: Mapped[str] = mapped_column(String(36), primary_key=True)
        secret: Mapped[str] = mapped_column(pii.EncryptedString, nullable=False)

    status = pii.encryption_status(bases=[_FixtureBase])
    assert len(status.columns) == 1, (
        "Scanner must re-read registry.mappers on every call — caching "
        "would freeze a stale view as lazy-imported models register."
    )


def test_scanner_surfaces_import_failure_via_scan_error(monkeypatch):
    """If _default_bases() reports a base-import error, the helper must
    surface that fact in ``scan_error`` (not silently report
    ``columns=()`` as a clean empty scan — that's the false-attestation
    surface this helper was supposed to close)."""

    def _broken_bases():
        return ([], "TenantBase: ImportError: simulated")

    monkeypatch.setattr(pii, "_default_bases", _broken_bases)
    status = pii.encryption_status()
    assert status.scan_error is not None
    assert "ImportError" in status.scan_error
    assert status.columns_actually_encrypted is False


def test_scanner_returns_asdict_serializable():
    """SOC2 evidence collector uses dataclasses.asdict + JSON-serializes."""
    import json

    status = pii.encryption_status()
    payload = dataclasses.asdict(status)
    assert "key_loaded" in payload
    assert "columns" in payload
    assert "columns_actually_encrypted" in payload
    assert "scan_error" in payload
    # Must round-trip through JSON (tuples render as arrays).
    decoded = json.loads(json.dumps(payload))
    assert isinstance(decoded["columns"], list)


def test_drift_map_matches_actual_rendered_pg_type():
    """The hard-coded ``_ORM_TO_PG["EncryptedString"] = "text"`` mapping
    must match what the TypeDecorator's ``impl`` actually compiles to in
    Postgres. This catches:
      * ``impl = LargeBinary`` (class rename) → would compile to ``bytea``
      * ``impl = String(200)`` (parameterized) → would compile to ``varchar(200)``
      * Any other future impl change that would silently leave the drift
        map out of sync.

    Method: compile the TypeDecorator for the Postgres dialect and
    compare the rendered type to the drift-map entry. The map stores
    bare type names ("text", "varchar") without the parenthesized size;
    strip parens before comparing.
    """
    from sqlalchemy.dialects import postgresql
    from gdx_dispatch.tools.tenant_schema_drift_check import _ORM_TO_PG

    rendered = pii.EncryptedString().compile(dialect=postgresql.dialect()).lower()
    canonical = rendered.split("(")[0].strip()
    drift_entry = _ORM_TO_PG["EncryptedString"]
    assert canonical == drift_entry, (
        f"Drift map says EncryptedString → {drift_entry!r} but the actual "
        f"compiled PG type is {rendered!r} (canonical={canonical!r}). "
        f"Update both, or revisit every tenant's column type in the "
        f"drift comparison."
    )


def test_real_model_writes_ciphertext_when_key_set(monkeypatch):
    """Activation integration test (auditor round 2 of slice 2): with
    ``pii._FERNET`` configured, an ORM ``WebhookEndpoint.secret`` write
    must land on disk as Fernet ciphertext, not plaintext. Closes the
    "tests pass with _FERNET=None so encryption-activation is unproven"
    theater finding.
    """
    import base64
    import os as _os
    from cryptography.fernet import Fernet
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool

    key = base64.urlsafe_b64encode(_os.urandom(32))
    monkeypatch.setattr(pii, "_FERNET", Fernet(key))

    from gdx_dispatch.core.audit import TenantBase
    from gdx_dispatch.core.webhooks.models import WebhookEndpoint

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Create just the webhook_endpoints table — other TenantBase tables
    # would drag in too many dependencies for this focused assertion.
    WebhookEndpoint.__table__.create(engine, checkfirst=True)

    with Session(engine) as session:
        endpoint = WebhookEndpoint(
            url="https://hooks.example.com/x",
            events=["job.created"],
            secret="my-shared-secret",
            is_active=True,
        )
        session.add(endpoint)
        session.commit()

        # On-disk bytes: raw text() select bypasses the TypeDecorator,
        # so it returns whatever is physically stored. Single-row table —
        # no WHERE needed and avoids SQLite UUID-binding compatibility quirks.
        stored = session.execute(
            text("SELECT secret FROM webhook_endpoints LIMIT 1"),
        ).scalar_one()
        assert stored.startswith("gAAAAA"), (
            f"WebhookEndpoint.secret did not encrypt; on-disk value "
            f"prefix={stored[:8]!r}. The EncryptedString TypeDecorator's "
            f"process_bind_param either didn't fire or pii._FERNET is "
            f"not properly patched into this test."
        )

        # ORM round-trip: decrypts cleanly.
        session.expire_all()
        re_read = session.get(WebhookEndpoint, endpoint.id)
        assert re_read is not None
        assert re_read.secret == "my-shared-secret"
