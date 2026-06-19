"""SS-27 slice B tests — sharing event emit helpers + schemas."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from gdx_dispatch.control.models import Base
from gdx_dispatch.core.cross_tenant_sharing import accept_share, create_share, revoke_share
from gdx_dispatch.core.cross_tenant_sharing_events import (
    SHARING_ACCEPTED,
    SHARING_CREATED,
    SHARING_REVOKED,
    SHARING_USED,
    emit_share_accepted,
    emit_share_created,
    emit_share_revoked,
    emit_share_used,
)
from gdx_dispatch.core.event_catalog import list_event_types, validate_event
from gdx_dispatch.models.platform_extensions import EventOutbox
from gdx_dispatch.models.platform_ss27_additions import SS27Base
from gdx_dispatch.tests.factories.platform import tenant_uuid_from_slug


def _make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'ss27e.db'}", future=True)
    Base.metadata.create_all(engine, tables=[EventOutbox.__table__])
    SS27Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    return SessionLocal()


def _seed(db):
    r = create_share(
        db,
        sharer=tenant_uuid_from_slug("tenant-a"),
        sharee=tenant_uuid_from_slug("tenant-b"),
        resource_type="parts_catalog",
        resource_id="catalog-42",
        capabilities=["read"],
        created_by_identity_id="identity-alice",
    )
    db.commit()
    return r


def test_schemas_registered_in_catalog():
    names = {e["event_type"] for e in list_event_types()}
    assert SHARING_CREATED in names
    assert SHARING_ACCEPTED in names
    assert SHARING_REVOKED in names
    assert SHARING_USED in names


def test_emit_share_created_payload_validates(tmp_path):
    db = _make_session(tmp_path)
    try:
        r = _seed(db)
        emit_share_created(db, r.share)
        db.commit()
        row = db.execute(select(EventOutbox)).scalars().first()
        assert row is not None
        assert row.event_name == SHARING_CREATED
        assert row.tenant_id == tenant_uuid_from_slug("tenant-a")
        validate_event(SHARING_CREATED, row.payload)
    finally:
        db.close()


def test_emit_share_accepted_payload_validates(tmp_path):
    db = _make_session(tmp_path)
    try:
        r = _seed(db)
        acc = accept_share(
            db,
            acceptance_token=r.acceptance_token,
            accepted_by_identity_id="identity-bob",
            accepted_by_tenant_id=tenant_uuid_from_slug("tenant-b"),
        )
        db.commit()
        emit_share_accepted(db, r.share, acc)
        db.commit()
        row = db.execute(
            select(EventOutbox).where(EventOutbox.event_name == SHARING_ACCEPTED)
        ).scalars().first()
        assert row is not None
        assert row.tenant_id == tenant_uuid_from_slug("tenant-a")  # metered against sharer
        validate_event(SHARING_ACCEPTED, row.payload)
    finally:
        db.close()


def test_emit_share_revoked_payload_validates(tmp_path):
    db = _make_session(tmp_path)
    try:
        r = _seed(db)
        revoke_share(db, share_id=r.share.id, revoker_identity_id="identity-alice")
        db.commit()
        emit_share_revoked(db, r.share)
        db.commit()
        row = db.execute(
            select(EventOutbox).where(EventOutbox.event_name == SHARING_REVOKED)
        ).scalars().first()
        assert row is not None
        validate_event(SHARING_REVOKED, row.payload)
    finally:
        db.close()


def test_emit_share_used_payload_validates(tmp_path):
    db = _make_session(tmp_path)
    try:
        r = _seed(db)
        emit_share_used(
            db,
            r.share,
            capability="read",
            used_at=datetime.now(timezone.utc),
            principal_identity_id="identity-bob",
        )
        db.commit()
        row = db.execute(
            select(EventOutbox).where(EventOutbox.event_name == SHARING_USED)
        ).scalars().first()
        assert row is not None
        assert row.tenant_id == tenant_uuid_from_slug("tenant-a")
        validate_event(SHARING_USED, row.payload)
    finally:
        db.close()
