"""The office can finally record a second person at a customer.

Why this file exists: `customer_contacts` shipped with a real model, a mobile
create path and an email recipient picker — and **zero rows in production**.
The only way to create a contact was a tech tapping through a mobile job
screen, so a second person at a business account had nowhere to live and ended
up as a QuickBooks sub-customer instead (see
docs/design/qb-subcustomer-flattening-plan.md).

Contracts pinned here:

1. **Every mutation leaves an audit row** — invariant #1. A contact write that
   succeeds without a trace is the defect class this repo cares most about.
2. **Delete is soft** — invariant #2. `customer_contacts` carries `deleted_at`;
   the row survives so "who did we email" stays reconstructable.
3. **The primary contact cannot be left unreachable.** The primary IS the
   address automated sends resolve to (`core/email_recipients.py:97`), so
   blanking their email is refused rather than silently breaking every
   automated email for the account.
4. **A contact id alone is not a key to any contact in the tenant** — reads and
   writes are scoped to the parent customer.
5. **A malformed customer id 404s, never 500s.**
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.routers.customers import (
    CustomerContactIn,
    CustomerContactPatchIn,
    create_customer_contact,
    delete_customer_contact,
    list_customer_contacts,
    make_contact_primary,
    update_customer_contact,
)

pytestmark = pytest.mark.anyio

USER = {"sub": "user-office-1", "email": "office@example.com"}


def _mock_request(tenant_id: str = "tenant-test") -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(tenant={"id": tenant_id}),
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_customer(db, name: str = "Riverbend Lumber") -> str:
    uid = uuid.uuid4()
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        text(
            """
            INSERT INTO customers (id, name, email, company_id, created_at, deleted_at)
            VALUES (:id, :name, :email, 'tenant-test', :created_at, NULL)
            """
        ),
        {"id": uid.hex, "name": name, "email": "account@example.com", "created_at": now},
    )
    db.commit()
    return str(uid)


def _audit_actions(db) -> list[str]:
    return [
        r[0]
        for r in db.execute(text("SELECT action FROM audit_logs ORDER BY rowid")).all()
    ]


async def _add(db, customer_id: str, **kw):
    payload = CustomerContactIn(**{"name": "Site A", **kw})
    return await create_customer_contact(
        customer_id, payload, _mock_request(), USER, db
    )


# ── create ──────────────────────────────────────────────────────────────────


async def test_create_contact_persists_and_audits(db):
    cid = _seed_customer(db)
    out = await _add(db, cid, phone="218-555-0100", label="property manager")

    assert out["name"] == "Site A"
    assert out["label"] == "property manager"
    # A new contact never seizes the default-recipient role — that is an
    # explicit act through make-primary, never a side effect of adding a person.
    assert out["is_primary"] is False
    assert "customer_contact_added" in _audit_actions(db)


async def test_create_contact_on_unknown_customer_404s(db):
    with pytest.raises(HTTPException) as exc:
        await _add(db, str(uuid.uuid4()))
    assert exc.value.status_code == 404


async def test_create_contact_on_malformed_customer_id_404s_not_500s(db):
    with pytest.raises(HTTPException) as exc:
        await _add(db, "not-a-uuid")
    assert exc.value.status_code == 404


# ── list ────────────────────────────────────────────────────────────────────


async def test_list_returns_only_this_customers_live_contacts(db):
    cid_a, cid_b = _seed_customer(db, "A"), _seed_customer(db, "B")
    await _add(db, cid_a, name="Site A")
    await _add(db, cid_b, name="Someone Else")

    names = [c["name"] for c in await list_customer_contacts(cid_a, USER, db)]
    assert names == ["Site A"]


async def test_list_on_malformed_customer_id_404s_not_500s(db):
    with pytest.raises(HTTPException) as exc:
        await list_customer_contacts("not-a-uuid", USER, db)
    assert exc.value.status_code == 404


# ── patch ───────────────────────────────────────────────────────────────────


async def test_patch_changes_only_the_fields_sent(db):
    cid = _seed_customer(db)
    made = await _add(db, cid, phone="218-555-0100", email="jeff@example.com")

    out = await update_customer_contact(
        cid, made["id"], CustomerContactPatchIn(email="jeff.new@example.com"),
        _mock_request(), USER, db,
    )
    assert out["email"] == "jeff.new@example.com"
    assert out["phone"] == "218-555-0100"   # untouched
    assert out["name"] == "Site A"            # untouched
    assert "customer_contact_updated" in _audit_actions(db)


async def test_patch_audit_records_field_names_never_values(db):
    """PII stays out of the audit row — the repo's established precedent."""
    cid = _seed_customer(db)
    made = await _add(db, cid, email="jeff@example.com")
    await update_customer_contact(
        cid, made["id"], CustomerContactPatchIn(email="secret@example.com"),
        _mock_request(), USER, db,
    )
    blob = " ".join(
        str(r[0]) for r in db.execute(
            text("SELECT details FROM audit_logs WHERE action='customer_contact_updated'")
        ).all()
    )
    assert "email" in blob            # which field changed
    assert "secret@example.com" not in blob   # never the value


async def test_patch_cannot_blank_the_name(db):
    cid = _seed_customer(db)
    made = await _add(db, cid)
    with pytest.raises(HTTPException) as exc:
        await update_customer_contact(
            cid, made["id"], CustomerContactPatchIn(name="   "),
            _mock_request(), USER, db,
        )
    assert exc.value.status_code == 422


async def test_patch_cannot_strand_the_primary_without_an_email(db):
    """The primary IS the automated-send address. Blanking it would leave every
    automated email for the account resolving to nothing, silently."""
    cid = _seed_customer(db)
    made = await _add(db, cid, email="jeff@example.com")
    await make_contact_primary(cid, made["id"], USER, db)

    with pytest.raises(HTTPException) as exc:
        await update_customer_contact(
            cid, made["id"], CustomerContactPatchIn(email=""),
            _mock_request(), USER, db,
        )
    assert exc.value.status_code == 422
    assert "default recipient" in exc.value.detail


async def test_patch_can_blank_a_non_primary_email(db):
    cid = _seed_customer(db)
    made = await _add(db, cid, email="jeff@example.com")
    out = await update_customer_contact(
        cid, made["id"], CustomerContactPatchIn(email=""),
        _mock_request(), USER, db,
    )
    assert out["email"] is None


async def test_patch_is_scoped_to_the_parent_customer(db):
    """A contact id from another account must not be reachable."""
    cid_a, cid_b = _seed_customer(db, "A"), _seed_customer(db, "B")
    made = await _add(db, cid_b, name="Theirs")
    with pytest.raises(HTTPException) as exc:
        await update_customer_contact(
            cid_a, made["id"], CustomerContactPatchIn(name="Hijacked"),
            _mock_request(), USER, db,
        )
    assert exc.value.status_code == 404


async def test_patch_with_no_changes_writes_no_audit_row(db):
    """A no-op PATCH is not an event. Audit noise costs the trail its meaning."""
    cid = _seed_customer(db)
    made = await _add(db, cid)
    await update_customer_contact(
        cid, made["id"], CustomerContactPatchIn(name="Site A"),
        _mock_request(), USER, db,
    )
    assert "customer_contact_updated" not in _audit_actions(db)


# ── delete ──────────────────────────────────────────────────────────────────


async def test_delete_is_soft_and_audited(db):
    cid = _seed_customer(db)
    made = await _add(db, cid)

    out = await delete_customer_contact(cid, made["id"], _mock_request(), USER, db)
    assert out["ok"] is True

    # Invariant #2: the row survives, only deleted_at moves.
    row = db.execute(
        text("SELECT deleted_at FROM customer_contacts WHERE id = :i"), {"i": made["id"]}
    ).first()
    assert row is not None and row[0] is not None
    assert await list_customer_contacts(cid, USER, db) == []
    assert "customer_contact_deleted" in _audit_actions(db)


async def test_deleting_the_primary_clears_the_role_and_says_so(db):
    """Leaving is_primary set on a dead row would let the resolver's
    'at most one live primary' invariant read as satisfied while no live
    contact holds the role."""
    cid = _seed_customer(db)
    made = await _add(db, cid, email="jeff@example.com")
    await make_contact_primary(cid, made["id"], USER, db)

    out = await delete_customer_contact(cid, made["id"], _mock_request(), USER, db)
    assert out["was_primary"] is True
    assert out["fallback"] == "account_email"

    still_primary = db.execute(
        text("SELECT is_primary FROM customer_contacts WHERE id = :i"), {"i": made["id"]}
    ).scalar()
    assert not still_primary


async def test_delete_is_scoped_to_the_parent_customer(db):
    cid_a, cid_b = _seed_customer(db, "A"), _seed_customer(db, "B")
    made = await _add(db, cid_b, name="Theirs")
    with pytest.raises(HTTPException) as exc:
        await delete_customer_contact(cid_a, made["id"], _mock_request(), USER, db)
    assert exc.value.status_code == 404
    assert len(await list_customer_contacts(cid_b, USER, db)) == 1
