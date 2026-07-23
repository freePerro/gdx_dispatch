from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.auth import get_current_user
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.routers.customers import (
    CustomerCreateIn,
    CustomerLocationCreateIn,
    CustomerUpdateIn,
    create_customer,
    create_customer_location,
    delete_customer,
    get_customer,
    list_customer_locations,
    list_customers,
    router,
    search_customers,
    update_customer,
)

pytestmark = pytest.mark.anyio


def _mock_request(tenant_id: str = "tenant-test") -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(tenant={"id": tenant_id}))


@pytest.fixture
def tenant_db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = Session()

    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _seed_customer(
    db,
    *,
    name: str = "Alice",
    email: str | None = "alice@example.com",
    phone: str | None = "555-1000",
    address: str | None = "123 Main",
    customer_type: str | None = "Retail",
    deleted: bool = False,
) -> str:
    uid = uuid.uuid4()
    # Store as 32-char hex (no dashes) to match SQLAlchemy Uuid(as_uuid=True) format on SQLite
    customer_id_hex = uid.hex
    now = datetime.now(timezone.utc).isoformat()
    deleted_at = now if deleted else None
    db.execute(
        text(
            """
            INSERT INTO customers
                (id, name, email, phone, address, customer_type, company_id, created_at, deleted_at)
            VALUES
                (:id, :name, :email, :phone, :address, :customer_type, 'tenant-test', :created_at, :deleted_at)
            """
        ),
        {
            "id": customer_id_hex,
            "name": name,
            "email": email,
            "phone": phone,
            "address": address,
            "customer_type": customer_type,
            "created_at": now,
            "deleted_at": deleted_at,
        },
    )
    db.commit()
    # Return the standard string representation (with dashes) as callers pass it to endpoints
    return str(uid)


def _seed_location(
    db,
    customer_id: str,
    *,
    label: str = "Service Address",
    address: str = "456 Oak",
    is_primary: bool = False,
) -> str:
    location_id = str(uuid.uuid4())
    db.execute(
        text(
            """
            INSERT INTO customer_locations
                (id, company_id, customer_id, label, address, access_notes, is_primary, created_at, deleted_at)
            VALUES
                (:id, :company_id, :customer_id, :label, :address, :access_notes, :is_primary, :created_at, NULL)
            """
        ),
        {
            "id": location_id,
            "company_id": "tenant-test",
            "customer_id": customer_id,
            "label": label,
            "address": address,
            "access_notes": None,
            "is_primary": 1 if is_primary else 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    db.commit()
    return location_id


def test_all_customer_routes_require_auth_dependency():
    guarded_paths = set()
    for route in router.routes:
        if not hasattr(route, "dependant"):
            continue
        for dep in route.dependant.dependencies:
            if dep.call is get_current_user:
                guarded_paths.add(route.path)
                break

    assert "/api/customers" in guarded_paths
    assert "/api/customers/{customer_id}" in guarded_paths
    assert "/api/customers/{customer_id}/locations" in guarded_paths
    assert "/api/customers/search" in guarded_paths


async def test_list_customers_empty(tenant_db_session):
    out = await list_customers(request=_mock_request(), q=None, page=1, per_page=50, _={}, db=tenant_db_session)
    assert out.items == []
    assert out.total == 0
    assert out.page == 1
    assert out.per_page == 50


async def test_create_customer_success(tenant_db_session):
    payload = CustomerCreateIn(
        name="Alice Johnson",
        phone="312-555-2222",
        email="alice@example.com",
        address="100 State St",
        customer_type="Retail",
    )
    out = await create_customer(payload=payload, _={}, db=tenant_db_session)

    assert out.id
    assert out.name == "Alice Johnson"
    assert out.phone == "312-555-2222"
    assert out.email == "alice@example.com"
    assert out.address == "100 State St"
    assert out.customer_type == "Retail"


def test_create_customer_validation_requires_name():
    with pytest.raises(Exception):
        CustomerCreateIn(phone="111")


async def test_get_customer_success(tenant_db_session):
    customer_id = _seed_customer(tenant_db_session, name="Bob")
    out = await get_customer(customer_id=customer_id, _={}, db=tenant_db_session)

    assert out["id"] == customer_id
    assert out["name"] == "Bob"
    # C8: jobs embedded so CustomerDetailView Jobs tab populates without
    # a second round-trip. Empty list when customer has no jobs.
    assert out["jobs"] == []


async def test_get_customer_embedded_jobs_carry_display_state(tenant_db_session):
    # The embedded jobs feed CustomerDetailView's JobStateChip (desktop).
    # Without display_state the chip falls back to the raw lifecycle_stage,
    # which reads "scheduled" even for a converted estimate with no
    # appointment date. Pin: enrichment present + authoritative shape.
    customer_id = _seed_customer(tenant_db_session, name="Dora")
    # ORM seed (mirrors test_customer_portal.py) — raw-SQL UUID strings don't
    # round-trip the Uuid(as_uuid=True) comparison in get_customer's query.
    from gdx_dispatch.models.tenant_models import Job

    tenant_db_session.add(
        Job(
            customer_id=uuid.UUID(customer_id),
            title="Converted estimate",
            lifecycle_stage="scheduled",
            status="Scheduled",
            dispatch_status="unassigned",
            billing_status="unbilled",
            company_id="tenant-test",
        )
    )
    tenant_db_session.commit()

    out = await get_customer(customer_id=customer_id, _={}, db=tenant_db_session)

    assert len(out["jobs"]) == 1
    j = out["jobs"][0]
    # scheduled_at key must be PRESENT (null) — the frontend's
    # isAwaitingSchedule guard requires the key to relabel.
    assert "scheduled_at" in j and j["scheduled_at"] is None
    ds = j["display_state"]
    assert ds is not None
    assert ds["stage"] == "scheduled"
    assert ds["type"] == "open"
    assert ds["label"] == "Scheduled"
    assert ds["is_finished"] is False


async def test_get_customer_not_found(tenant_db_session):
    with pytest.raises(Exception) as exc:
        await get_customer(customer_id=str(uuid.uuid4()), _={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 404


async def test_update_customer_success_partial(tenant_db_session):
    customer_id = _seed_customer(tenant_db_session, name="Carl", phone="555-0101")

    out = await update_customer(
        customer_id=customer_id,
        payload=CustomerUpdateIn(phone="555-0202", customer_type="Commercial"),
        _={},
        db=tenant_db_session,
    )
    assert out.phone == "555-0202"
    assert out.customer_type == "Commercial"


def test_referral_source_rejects_oversize_value():
    """Customer.source is String(50). Pydantic must reject before the row
    ever reaches Postgres — sqlite would accept it silently, masking the
    failure during tests (2026-05-21 re-audit catch)."""
    with pytest.raises(Exception):
        CustomerCreateIn(name="Eve", referral_source="x" * 51)
    with pytest.raises(Exception):
        CustomerUpdateIn(referral_source="x" * 51)


async def test_create_and_update_persist_notes_and_referral_source(tenant_db_session):
    """2026-05-21 audit catch: CustomerFormDialog had been shipping `notes`
    and `referral_source` long before the dialog extraction, but
    CustomerCreateIn/CustomerUpdateIn didn't accept either — Pydantic was
    silently dropping them. This pins the round-trip both ways."""
    payload = CustomerCreateIn(
        name="Dana",
        phone="555-9000",
        notes="VIP — flexible scheduling",
        referral_source="Angi",
    )
    created = await create_customer(payload=payload, _={}, db=tenant_db_session)
    assert created.notes == "VIP — flexible scheduling"
    assert created.referral_source == "Angi"

    updated = await update_customer(
        customer_id=created.id,
        payload=CustomerUpdateIn(notes="Updated note", referral_source="Google Ads"),
        _={},
        db=tenant_db_session,
    )
    assert updated.notes == "Updated note"
    assert updated.referral_source == "Google Ads"


def test_update_customer_validation_rejects_blank_name():
    with pytest.raises(Exception):
        CustomerUpdateIn(name="   ")


async def test_update_customer_not_found(tenant_db_session):
    with pytest.raises(Exception) as exc:
        await update_customer(
            customer_id=str(uuid.uuid4()),
            payload=CustomerUpdateIn(name="New Name"),
            _={},
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 404


async def test_delete_customer_soft_delete(tenant_db_session):
    customer_id = _seed_customer(tenant_db_session, name="Erin")

    resp = await delete_customer(customer_id=customer_id, _={}, db=tenant_db_session)
    assert resp.status_code == 204

    # ORM stores UUIDs as 32-char hex in SQLite; convert for raw SQL verification
    id_hex = uuid.UUID(customer_id).hex
    row = tenant_db_session.execute(
        text("SELECT deleted_at FROM customers WHERE id = :id"),
        {"id": id_hex},
    ).mappings().first()
    assert row is not None
    assert row["deleted_at"] is not None


async def test_delete_customer_not_found(tenant_db_session):
    with pytest.raises(Exception) as exc:
        await delete_customer(customer_id=str(uuid.uuid4()), _={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 404


async def test_list_customers_excludes_soft_deleted(tenant_db_session):
    _seed_customer(tenant_db_session, name="Active One", deleted=False)
    _seed_customer(tenant_db_session, name="Deleted One", deleted=True)

    out = await list_customers(request=_mock_request(), q=None, page=1, per_page=50, _={}, db=tenant_db_session)
    names = [c.name for c in out.items]
    assert "Active One" in names
    assert "Deleted One" not in names


async def test_list_customers_search_and_pagination(tenant_db_session):
    _seed_customer(tenant_db_session, name="Acme Heating", email="acme@example.com", phone="555-1111")
    _seed_customer(tenant_db_session, name="Beta Plumbing", email="beta@example.com", phone="555-2222")
    _seed_customer(tenant_db_session, name="Gamma HVAC", email="gamma@example.com", phone="555-3333")

    search_out = await list_customers(request=_mock_request(), q="beta", page=1, per_page=50, _={}, db=tenant_db_session)
    assert search_out.total == 1
    assert search_out.items[0].name == "Beta Plumbing"

    page_out = await list_customers(request=_mock_request(), q=None, page=2, per_page=1, _={}, db=tenant_db_session)
    assert page_out.page == 2
    assert page_out.per_page == 1
    assert page_out.total == 3
    assert len(page_out.items) == 1


async def test_list_customers_digits_query_matches_formatted_phone(tenant_db_session):
    # Regression (feat/daily-ux-improvements): the at-entry dedup UI queries
    # /api/customers with a digits-only phone ("5551234567"), but phones are
    # stored free-form ("(555) 123-4567"). A plain LIKE on the raw column never
    # matched, so the phone dedup check was silently dead. list_customers now
    # also compares a separator-stripped phone.
    _seed_customer(tenant_db_session, name="Formatted Phone", phone="(555) 123-4567")
    _seed_customer(tenant_db_session, name="Other", phone="555-999-0000")

    digits = await list_customers(
        request=_mock_request(), q="5551234567", page=1, per_page=50, _={}, db=tenant_db_session
    )
    assert digits.total == 1
    assert digits.items[0].name == "Formatted Phone"

    # A non-matching digit string still returns nothing (no false positives).
    none_out = await list_customers(
        request=_mock_request(), q="5550000001", page=1, per_page=50, _={}, db=tenant_db_session
    )
    assert none_out.total == 0


async def test_search_endpoint_matches_name_phone_email(tenant_db_session):
    _seed_customer(tenant_db_session, name="Martha", email="martha@example.com", phone="555-0100")
    _seed_customer(tenant_db_session, name="Nora", email="nora@example.com", phone="555-0200")

    by_name = await search_customers(q="marth", _={}, db=tenant_db_session)
    assert len(by_name) == 1

    by_email = await search_customers(q="nora@example", _={}, db=tenant_db_session)
    assert len(by_email) == 1

    by_phone = await search_customers(q="0100", _={}, db=tenant_db_session)
    assert len(by_phone) == 1


def test_search_endpoint_query_requires_min_length():
    from inspect import signature

    q_param = signature(search_customers).parameters["q"]
    field_info = q_param.default
    assert any(getattr(meta, "min_length", None) == 1 for meta in getattr(field_info, "metadata", []))


async def test_list_customer_locations_success(tenant_db_session):
    customer_id = _seed_customer(tenant_db_session, name="Loc Test")
    _seed_location(tenant_db_session, customer_id, label="HQ", address="1 Main", is_primary=True)
    _seed_location(tenant_db_session, customer_id, label="Warehouse", address="2 Main", is_primary=False)

    out = await list_customer_locations(customer_id=customer_id, _={}, db=tenant_db_session)
    assert len(out) == 2
    labels = {row.label for row in out}
    assert labels == {"HQ", "Warehouse"}


async def test_list_customer_locations_customer_not_found(tenant_db_session):
    with pytest.raises(Exception) as exc:
        await list_customer_locations(customer_id=str(uuid.uuid4()), _={}, db=tenant_db_session)
    assert getattr(exc.value, "status_code", None) == 404


async def test_create_customer_location_success(tenant_db_session):
    customer_id = _seed_customer(tenant_db_session, name="Loc Create")

    out = await create_customer_location(
        customer_id=customer_id,
        payload=CustomerLocationCreateIn(
            label="Service Address",
            address="55 Service Rd",
            access_notes="Gate code 1234",
            is_primary=True,
        ),
        request=_mock_request(),
        _={},
        db=tenant_db_session,
    )
    assert out.customer_id == customer_id
    assert out.label == "Service Address"
    assert out.address == "55 Service Rd"
    assert out.access_notes == "Gate code 1234"
    assert out.is_primary is True


def test_create_customer_location_validation_requires_address():
    with pytest.raises(Exception):
        CustomerLocationCreateIn(label="No Address", address="")


async def test_create_customer_location_customer_not_found(tenant_db_session):
    with pytest.raises(Exception) as exc:
        await create_customer_location(
            customer_id=str(uuid.uuid4()),
            payload=CustomerLocationCreateIn(label="Nowhere", address="11 Void Ave"),
            request=_mock_request(),
            _={},
            db=tenant_db_session,
        )
    assert getattr(exc.value, "status_code", None) == 404


# ─── Mobile customer-search fix (2026-07-22) ────────────────────────────────
# Doug: "it is a pain to search customers [on mobile]". The mobile job
# dialog uses GET /api/customers/search, which lacked the digit-stripped
# phone matching that list_customers got — a tech typing digits off caller
# ID against formatted stored numbers matched nothing. It also skipped the
# legacy "(deleted)"-name filter the list applies.


async def test_search_endpoint_digits_query_matches_formatted_phone(tenant_db_session):
    _seed_customer(tenant_db_session, name="Formatted Mobile", phone="(612) 555-1234")
    _seed_customer(tenant_db_session, name="Other Number", phone="555-999-0000")

    hits = await search_customers(q="6125551234", _={}, db=tenant_db_session)
    assert [c.name for c in hits] == ["Formatted Mobile"]

    # No false positives on a non-matching digit string.
    misses = await search_customers(q="6120000001", _={}, db=tenant_db_session)
    assert misses == []


async def test_search_endpoint_short_digit_queries_unchanged(tenant_db_session):
    # <7 digits: the stripped-phone clause must NOT kick in (parity with
    # list_customers) — plain substring semantics still apply.
    _seed_customer(tenant_db_session, name="Area Code Only", phone="(612) 555-1234")
    hits = await search_customers(q="612", _={}, db=tenant_db_session)
    # "612" appears literally in the formatted phone, so plain LIKE finds it —
    # the point is no error and no digit-stripping surprises.
    assert [c.name for c in hits] == ["Area Code Only"]


async def test_search_endpoint_excludes_deleted_marker_names(tenant_db_session):
    _seed_customer(tenant_db_session, name="Keep Me", phone="555-0001")
    _seed_customer(tenant_db_session, name="Old Row (deleted)", phone="555-0002")

    hits = await search_customers(q="row", _={}, db=tenant_db_session)
    assert all("(deleted)" not in c.name.lower() for c in hits)
    assert [c.name for c in hits] == []


async def test_list_customers_bypasses_cache_for_searches(tenant_db_session, monkeypatch):
    """Search staleness fix: q≠'' must go straight to the DB (no 30s cache
    window right when someone is checking whether their new customer
    saved). q='' keeps the cached() path."""
    import gdx_dispatch.routers.customers as customers_mod

    calls: list[str] = []

    async def _spy_cached(tenant_id, key, ttl_seconds, fetcher):
        calls.append(key)
        result = fetcher()
        if hasattr(result, "__await__"):
            result = await result
        return result

    monkeypatch.setattr(customers_mod, "cached", _spy_cached)
    _seed_customer(tenant_db_session, name="Cachey", phone="555-4242")

    await list_customers(request=_mock_request(), q="cachey", page=1, per_page=50, _={}, db=tenant_db_session)
    assert calls == [], "search queries must not flow through cached()"

    await list_customers(request=_mock_request(), q=None, page=1, per_page=50, _={}, db=tenant_db_session)
    assert len(calls) == 1, "the q='' default list should still be cached"
