"""SS-27 slice A tests — cross_tenant_sharing core helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.core.cross_tenant_sharing import (
    ShareAcceptanceError,
    ShareError,
    ShareNotFoundError,
    ShareRevocationError,
    accept_share,
    check_share_grants_capability,
    create_share,
    revoke_share,
)
from gdx_dispatch.tests.factories.platform import tenant_uuid_from_slug
from gdx_dispatch.models.platform_ss27_additions import (
    CrossTenantShare,
    CrossTenantShareAcceptance,
    SS27Base,
)


def _make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'ss27.db'}", future=True)
    SS27Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return SessionLocal()


def _mk(db, **overrides):
    defaults = dict(
        sharer=tenant_uuid_from_slug("tenant-a"),
        sharee=tenant_uuid_from_slug("tenant-b"),
        resource_type="parts_catalog",
        resource_id="catalog-42",
        capabilities=["read"],
        created_by_identity_id="identity-alice",
    )
    defaults.update(overrides)
    return create_share(db, **defaults)


def test_create_share_basic(tmp_path):
    db = _make_session(tmp_path)
    try:
        result = _mk(db)
        db.commit()
        assert result.was_existing is False
        assert result.acceptance_token is not None
        assert len(result.acceptance_token) >= 22  # 16-byte urlsafe
        assert result.share.sharer_tenant_id == tenant_uuid_from_slug("tenant-a")
        assert result.share.sharee_tenant_id == tenant_uuid_from_slug("tenant-b")
        assert result.share.capabilities == ["read"]
        # Hash is stored, not plaintext.
        assert result.share.acceptance_token_hash != result.acceptance_token
        assert result.share.expires_at is not None
    finally:
        db.close()


def test_create_share_idempotent(tmp_path):
    db = _make_session(tmp_path)
    try:
        a = _mk(db)
        db.commit()
        b = _mk(db)
        db.commit()
        assert b.was_existing is True
        assert b.acceptance_token is None
        assert b.share.id == a.share.id
    finally:
        db.close()


def test_create_share_rejects_self_share(tmp_path):
    db = _make_session(tmp_path)
    try:
        with pytest.raises(ShareError):
            _mk(db, sharer=tenant_uuid_from_slug("tenant-x"), sharee=tenant_uuid_from_slug("tenant-x"))
    finally:
        db.close()


def test_create_share_rejects_empty_capabilities(tmp_path):
    db = _make_session(tmp_path)
    try:
        with pytest.raises(ShareError):
            _mk(db, capabilities=[])
    finally:
        db.close()


def test_create_share_normalizes_capabilities(tmp_path):
    db = _make_session(tmp_path)
    try:
        r = _mk(db, capabilities=["Read", "WRITE", "read", "  aggregate  "])
        db.commit()
        assert r.share.capabilities == ["aggregate", "read", "write"]
    finally:
        db.close()


def test_accept_share_happy_path(tmp_path):
    db = _make_session(tmp_path)
    try:
        r = _mk(db)
        db.commit()
        acc = accept_share(
            db,
            acceptance_token=r.acceptance_token,
            accepted_by_identity_id="identity-bob",
            accepted_by_tenant_id=tenant_uuid_from_slug("tenant-b"),
        )
        db.commit()
        assert acc.share_id == r.share.id
        assert acc.accepted_by_identity_id == "identity-bob"
    finally:
        db.close()


def test_accept_share_wrong_token_rejected(tmp_path):
    db = _make_session(tmp_path)
    try:
        _mk(db)
        db.commit()
        with pytest.raises(ShareAcceptanceError):
            accept_share(
                db,
                acceptance_token="not-the-token",
                accepted_by_identity_id="identity-bob",
                accepted_by_tenant_id=tenant_uuid_from_slug("tenant-b"),
            )
    finally:
        db.close()


def test_accept_share_single_use(tmp_path):
    db = _make_session(tmp_path)
    try:
        r = _mk(db)
        db.commit()
        accept_share(
            db,
            acceptance_token=r.acceptance_token,
            accepted_by_identity_id="identity-bob",
            accepted_by_tenant_id=tenant_uuid_from_slug("tenant-b"),
        )
        db.commit()
        with pytest.raises(ShareAcceptanceError):
            accept_share(
                db,
                acceptance_token=r.acceptance_token,
                accepted_by_identity_id="identity-bob",
                accepted_by_tenant_id=tenant_uuid_from_slug("tenant-b"),
            )
    finally:
        db.close()


def test_accept_share_expired(tmp_path):
    db = _make_session(tmp_path)
    try:
        r = _mk(db)
        # Force expiry into the past.
        r.share.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        with pytest.raises(ShareAcceptanceError):
            accept_share(
                db,
                acceptance_token=r.acceptance_token,
                accepted_by_identity_id="identity-bob",
                accepted_by_tenant_id=tenant_uuid_from_slug("tenant-b"),
            )
    finally:
        db.close()


def test_revoke_share(tmp_path):
    db = _make_session(tmp_path)
    try:
        r = _mk(db)
        db.commit()
        revoke_share(db, share_id=r.share.id, revoker_identity_id="identity-alice")
        db.commit()
        assert r.share.revoked_at is not None
        assert r.share.revoked_by_identity_id == "identity-alice"
    finally:
        db.close()


def test_revoke_share_unknown(tmp_path):
    db = _make_session(tmp_path)
    try:
        from uuid import uuid4
        with pytest.raises(ShareNotFoundError):
            revoke_share(db, share_id=uuid4(), revoker_identity_id="x")
    finally:
        db.close()


def test_revoke_share_requires_revoker(tmp_path):
    db = _make_session(tmp_path)
    try:
        r = _mk(db)
        db.commit()
        with pytest.raises(ShareRevocationError):
            revoke_share(db, share_id=r.share.id, revoker_identity_id="")
    finally:
        db.close()


def test_check_share_grants_capability_happy(tmp_path):
    db = _make_session(tmp_path)
    try:
        r = _mk(db, capabilities=["read", "write"])
        db.commit()
        accept_share(
            db,
            acceptance_token=r.acceptance_token,
            accepted_by_identity_id="identity-bob",
            accepted_by_tenant_id=tenant_uuid_from_slug("tenant-b"),
        )
        db.commit()
        assert (
            check_share_grants_capability(
                db,
                sharee_tenant_id=tenant_uuid_from_slug("tenant-b"),
                caller_capability="read",
                resource_type="parts_catalog",
                resource_id="catalog-42",
            )
            is True
        )
        assert (
            check_share_grants_capability(
                db,
                sharee_tenant_id=tenant_uuid_from_slug("tenant-b"),
                caller_capability="WRITE",
                resource_type="parts_catalog",
                resource_id="catalog-42",
            )
            is True
        )
        assert (
            check_share_grants_capability(
                db,
                sharee_tenant_id=tenant_uuid_from_slug("tenant-b"),
                caller_capability="delete",
                resource_type="parts_catalog",
                resource_id="catalog-42",
            )
            is False
        )
    finally:
        db.close()


def test_check_share_requires_acceptance(tmp_path):
    db = _make_session(tmp_path)
    try:
        _mk(db)
        db.commit()
        # Not yet accepted → False.
        assert (
            check_share_grants_capability(
                db,
                sharee_tenant_id=tenant_uuid_from_slug("tenant-b"),
                caller_capability="read",
                resource_type="parts_catalog",
                resource_id="catalog-42",
            )
            is False
        )
    finally:
        db.close()


def test_check_share_revoked_returns_false(tmp_path):
    db = _make_session(tmp_path)
    try:
        r = _mk(db)
        db.commit()
        accept_share(
            db,
            acceptance_token=r.acceptance_token,
            accepted_by_identity_id="identity-bob",
            accepted_by_tenant_id=tenant_uuid_from_slug("tenant-b"),
        )
        db.commit()
        revoke_share(db, share_id=r.share.id, revoker_identity_id="identity-alice")
        db.commit()
        assert (
            check_share_grants_capability(
                db,
                sharee_tenant_id=tenant_uuid_from_slug("tenant-b"),
                caller_capability="read",
                resource_type="parts_catalog",
                resource_id="catalog-42",
            )
            is False
        )
    finally:
        db.close()


def test_check_share_expired_returns_false(tmp_path):
    db = _make_session(tmp_path)
    try:
        r = _mk(db)
        db.commit()
        accept_share(
            db,
            acceptance_token=r.acceptance_token,
            accepted_by_identity_id="identity-bob",
            accepted_by_tenant_id=tenant_uuid_from_slug("tenant-b"),
        )
        r.share.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert (
            check_share_grants_capability(
                db,
                sharee_tenant_id=tenant_uuid_from_slug("tenant-b"),
                caller_capability="read",
                resource_type="parts_catalog",
                resource_id="catalog-42",
            )
            is False
        )
    finally:
        db.close()
