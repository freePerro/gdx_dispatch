"""Tests for the service_agreements router."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.core.database import get_db
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models on TenantBase.metadata)
from gdx_dispatch.models.tenant_models import Customer, ServiceAgreementTemplate
from gdx_dispatch.routers.auth import get_current_user
from gdx_dispatch.routers.service_agreements import router

CUSTOMER_ID = "5a3c1e2d-0b4f-4a6e-9c7d-8e1f2a3b4c5d"
CUSTOMER_NAME = "Jane Customer"


def _make_client(tenant_id: str = "tenant-test") -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    setup = Session()
    setup.execute(
        text(
            """
            INSERT OR IGNORE INTO company_module_grants (id, company_id, module_key, granted_at, created_at)
            VALUES (:id, :tid, 'jobs', datetime('now'), datetime('now'))
            """
        ),
        {"id": f"g2-{tenant_id}", "tid": tenant_id},
    )
    # #684: an agreement must point at a real customer, so the helper payload's
    # customer exists. Through the ORM — SQLite stores Uuid columns dashless, and
    # a raw INSERT of the dashed string would never match.
    setup.add(Customer(id=UUID(CUSTOMER_ID), name=CUSTOMER_NAME, company_id=tenant_id))
    setup.commit()
    setup.close()

    def _override_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()

    @app.middleware("http")
    async def inject_tenant(request, call_next):
        request.state.tenant = {"id": tenant_id}
        return await call_next(request)

    app.include_router(router)
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user] = lambda: {
        "user_id": "user-1",
        "sub": "user-1",
        "role": "admin",
        "tenant_id": tenant_id,
    }

    tc = TestClient(app, raise_server_exceptions=True)
    tc._engine = engine  # type: ignore[attr-defined]
    return tc


@pytest.fixture()
def client():
    tc = _make_client()
    yield tc
    tc.app.dependency_overrides.clear()
    tc._engine.dispose()  # type: ignore[attr-defined]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _agreement_payload(**overrides) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    base = {
        "customer_id": CUSTOMER_ID,
        "name": "Annual Maintenance",
        "start_date": _iso(now),
        "end_date": _iso(now + timedelta(days=365)),
        "price": 299.0,
        "services_included": ["Spring inspection", "Lube service"],
        "notes": "Signed on-site",
    }
    base.update(overrides)
    return base


def _template_payload(**overrides) -> dict:
    base = {
        "name": "Gold Maintenance",
        "description": "Annual preventive maintenance",
        "default_duration_months": 12,
        "default_price": 299.0,
        "services_included": ["Spring inspection", "Lube service", "Safety check"],
    }
    base.update(overrides)
    return base


def test_create_template(client: TestClient):
    r = client.post("/api/service-agreements/templates", json=_template_payload())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"]
    assert data["name"] == "Gold Maintenance"
    assert data["default_duration_months"] == 12
    assert data["default_price"] == 299.0
    assert data["services_included"] == [
        "Spring inspection",
        "Lube service",
        "Safety check",
    ]
    assert data["company_id"] == "tenant-test"

    listed = client.get("/api/service-agreements/templates").json()
    assert len(listed) == 1
    assert listed[0]["id"] == data["id"]


# ── PATCH /templates/{id} ────────────────────────────────────────────────
# Added 2026-08-12. This path used to be served by a `ui_compat` handler whose
# whole body was `return {"ok": True}` — it won route arbitration over this
# router, so the Vue showed "Template updated" and the edit was discarded.
# Contract-gap class C6, 2026-08-12.


def _create_template(client: TestClient) -> dict:
    r = client.post("/api/service-agreements/templates", json=_template_payload())
    assert r.status_code == 201, r.text
    return r.json()


def test_patch_template_persists_name(client: TestClient):
    tpl = _create_template(client)
    r = client.patch(
        f"/api/service-agreements/templates/{tpl['id']}", json={"name": "Platinum"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Platinum"
    # re-read: the write must survive, not just echo back
    listed = client.get("/api/service-agreements/templates").json()
    assert listed[0]["name"] == "Platinum"


def test_patch_template_accepts_the_frontends_price_field(client: TestClient):
    """ServiceAgreementsView sends `price`; the column is `default_price`."""
    tpl = _create_template(client)
    r = client.patch(f"/api/service-agreements/templates/{tpl['id']}", json={"price": 450.0})
    assert r.status_code == 200, r.text
    assert r.json()["default_price"] == 450.0
    assert client.get("/api/service-agreements/templates").json()[0]["default_price"] == 450.0


def test_patch_template_explicit_default_price_wins(client: TestClient):
    tpl = _create_template(client)
    r = client.patch(
        f"/api/service-agreements/templates/{tpl['id']}",
        json={"price": 1.0, "default_price": 777.0},
    )
    assert r.status_code == 200, r.text
    assert r.json()["default_price"] == 777.0


def test_patch_template_leaves_unsent_fields_alone(client: TestClient):
    tpl = _create_template(client)
    r = client.patch(f"/api/service-agreements/templates/{tpl['id']}", json={"name": "Silver"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["description"] == "Annual preventive maintenance"
    assert body["default_price"] == 299.0
    assert body["default_duration_months"] == 12


def test_patch_template_services_included_round_trips(client: TestClient):
    tpl = _create_template(client)
    r = client.patch(
        f"/api/service-agreements/templates/{tpl['id']}",
        json={"services_included": ["Tune-up", "Spring check"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["services_included"] == ["Tune-up", "Spring check"]


def test_patch_template_empty_body_is_422_not_silent_ok(client: TestClient):
    """The bug this replaced was 'reports success, changes nothing'."""
    tpl = _create_template(client)
    r = client.patch(f"/api/service-agreements/templates/{tpl['id']}", json={})
    assert r.status_code == 422, r.text


def test_patch_template_unknown_id_is_404(client: TestClient):
    r = client.patch(
        f"/api/service-agreements/templates/{uuid4()}", json={"name": "Nope"}
    )
    assert r.status_code == 404, r.text


# ── DELETE /templates/{id} ───────────────────────────────────────────────
# Added 2026-09-07 (#455). The Vue's trash button on every template row has
# always sent this DELETE; only PATCH was registered on the path, so the
# confirm dialog was followed by a 405 (contract-gap class C2).


def _audit_rows(client: TestClient, action: str) -> list[dict]:
    with client._engine.connect() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            text("SELECT entity_id, details FROM audit_logs WHERE action = :a"),
            {"a": action},
        ).mappings().all()
    return [dict(r) for r in rows]


def test_delete_template_soft_deletes_and_drops_it_from_the_list(client: TestClient):
    tpl = _create_template(client)
    assert len(client.get("/api/service-agreements/templates").json()) == 1

    r = client.delete(f"/api/service-agreements/templates/{tpl['id']}")
    assert r.status_code == 204, r.text
    assert client.get("/api/service-agreements/templates").json() == []

    # Invariant #2: the row is still there, stamped — not hard-deleted.
    # Read it through the ORM: SQLAlchemy's Uuid type stores dash-less hex on
    # SQLite, so a raw-SQL compare against the dashed API id finds nothing.
    with Session(client._engine) as session:  # type: ignore[attr-defined]
        row = session.execute(
            select(ServiceAgreementTemplate).where(
                ServiceAgreementTemplate.id == UUID(tpl["id"])
            )
        ).scalar_one()
        assert row.name == "Gold Maintenance"
        assert row.deleted_at is not None


def test_delete_template_writes_an_audit_row(client: TestClient):
    """Invariant #1. Only a read of the table proves the row actually landed."""
    tpl = _create_template(client)
    client.delete(f"/api/service-agreements/templates/{tpl['id']}")

    rows = _audit_rows(client, "service_agreement_template_deleted")
    assert len(rows) == 1, rows
    assert rows[0]["entity_id"] == tpl["id"]
    details = rows[0]["details"]
    if isinstance(details, str):
        details = json.loads(details)
    assert details["name"] == "Gold Maintenance"
    assert details["agreements_referencing"] == 0


def test_delete_template_leaves_existing_agreements_alone(client: TestClient):
    """Agreements copy price/services at create time; only new ones lose the template."""
    tpl = _create_template(client)
    agreement = client.post(
        "/api/service-agreements",
        json=_agreement_payload(template_id=tpl["id"], name="Gold for a customer"),
    ).json()

    assert client.delete(f"/api/service-agreements/templates/{tpl['id']}").status_code == 204

    still = client.get(f"/api/service-agreements/{agreement['id']}").json()
    assert still["template_id"] == tpl["id"]
    assert still["price"] == 299.0
    assert still["services_included"] == ["Spring inspection", "Lube service"]

    # ...and the count of what pointed at it is on the audit row.
    details = _audit_rows(client, "service_agreement_template_deleted")[0]["details"]
    if isinstance(details, str):
        details = json.loads(details)
    assert details["agreements_referencing"] == 1


def test_delete_template_then_new_agreement_cannot_use_it(client: TestClient):
    tpl = _create_template(client)
    client.delete(f"/api/service-agreements/templates/{tpl['id']}")
    r = client.post(
        "/api/service-agreements", json=_agreement_payload(template_id=tpl["id"])
    )
    assert r.status_code == 404, r.text


def test_delete_template_rolls_back_when_the_audit_row_cannot_be_written(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """An unaudited destructive change is worse than a failed one.

    This router's own ``_audit`` helper runs after ``db.commit()`` and swallows
    its exception — with it, a failing audit left the template deleted, no
    trail, and a 204 on the wire. delete_template uses ``audit_or_rollback``
    instead, so this test fails the moment anyone puts ``_audit`` back.
    """
    tpl = _create_template(client)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("audit table unavailable")

    monkeypatch.setattr(
        "gdx_dispatch.core.audit.log_audit_event_sync", _boom, raising=True
    )

    r = client.delete(f"/api/service-agreements/templates/{tpl['id']}")
    assert r.status_code == 500, r.text

    # The template must still be there, and still usable.
    listed = client.get("/api/service-agreements/templates").json()
    assert [t["id"] for t in listed] == [tpl["id"]]
    with Session(client._engine) as session:  # type: ignore[attr-defined]
        row = session.execute(
            select(ServiceAgreementTemplate).where(
                ServiceAgreementTemplate.id == UUID(tpl["id"])
            )
        ).scalar_one()
        assert row.deleted_at is None


def test_delete_template_unknown_id_is_404(client: TestClient):
    r = client.delete(f"/api/service-agreements/templates/{uuid4()}")
    assert r.status_code == 404, r.text


def test_delete_template_twice_is_404_not_a_second_success(client: TestClient):
    tpl = _create_template(client)
    assert client.delete(f"/api/service-agreements/templates/{tpl['id']}").status_code == 204
    assert client.delete(f"/api/service-agreements/templates/{tpl['id']}").status_code == 404
    assert len(_audit_rows(client, "service_agreement_template_deleted")) == 1


def test_delete_template_malformed_id_is_422_not_a_500(client: TestClient):
    """A str path param would blow up inside SQLAlchemy's Uuid bind processor."""
    r = client.delete("/api/service-agreements/templates/not-a-uuid")
    assert r.status_code == 422, r.text


def test_create_agreement_from_template(client: TestClient):
    tpl = client.post(
        "/api/service-agreements/templates", json=_template_payload()
    ).json()
    payload = _agreement_payload(template_id=tpl["id"], name="Gold for Jane")
    r = client.post("/api/service-agreements", json=payload)
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["template_id"] == tpl["id"]
    assert data["status"] == "active"
    assert data["name"] == "Gold for Jane"
    assert data["company_id"] == "tenant-test"


def test_list_agreements_tenant_scoped():
    c1 = _make_client(tenant_id="tenant-a")
    c2 = _make_client(tenant_id="tenant-b")
    try:
        r1 = c1.post("/api/service-agreements", json=_agreement_payload(name="A-plan"))
        assert r1.status_code == 201, r1.text
        r2 = c2.post("/api/service-agreements", json=_agreement_payload(name="B-plan"))
        assert r2.status_code == 201, r2.text

        list_a = c1.get("/api/service-agreements").json()
        list_b = c2.get("/api/service-agreements").json()
        assert len(list_a) == 1 and list_a[0]["name"] == "A-plan"
        assert len(list_b) == 1 and list_b[0]["name"] == "B-plan"

        cross = c1.get(f"/api/service-agreements/{list_b[0]['id']}")
        assert cross.status_code == 404
    finally:
        c1.app.dependency_overrides.clear()
        c2.app.dependency_overrides.clear()
        c1._engine.dispose()  # type: ignore[attr-defined]
        c2._engine.dispose()  # type: ignore[attr-defined]


def test_expiring_filter(client: TestClient):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    near = client.post(
        "/api/service-agreements",
        json=_agreement_payload(
            name="Expires in 10 days",
            start_date=_iso(now - timedelta(days=355)),
            end_date=_iso(now + timedelta(days=10)),
        ),
    ).json()
    far = client.post(
        "/api/service-agreements",
        json=_agreement_payload(
            name="Expires in 60 days",
            start_date=_iso(now - timedelta(days=305)),
            end_date=_iso(now + timedelta(days=60)),
        ),
    ).json()

    r = client.get("/api/service-agreements/expiring", params={"days": 30})
    assert r.status_code == 200, r.text
    rows = r.json()
    ids = {row["id"] for row in rows}
    assert near["id"] in ids
    assert far["id"] not in ids


def test_cancel_agreement(client: TestClient):
    created = client.post(
        "/api/service-agreements", json=_agreement_payload()
    ).json()
    r = client.post(f"/api/service-agreements/{created['id']}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"

    # Second cancel = 400
    r2 = client.post(f"/api/service-agreements/{created['id']}/cancel")
    assert r2.status_code == 400


def test_reject_end_before_start(client: TestClient):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = _agreement_payload(
        start_date=_iso(now + timedelta(days=10)),
        end_date=_iso(now + timedelta(days=5)),
    )
    r = client.post("/api/service-agreements", json=payload)
    assert r.status_code == 422


def test_patch_status(client: TestClient):
    created = client.post(
        "/api/service-agreements", json=_agreement_payload()
    ).json()
    r = client.patch(
        f"/api/service-agreements/{created['id']}",
        json={"status": "expired", "price": 399.0},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "expired"
    assert data["price"] == 399.0

    # Invalid status rejected by pattern
    bad = client.patch(
        f"/api/service-agreements/{created['id']}",
        json={"status": "bogus"},
    )
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# #684 — the New Agreement dialog could not create anything
# ---------------------------------------------------------------------------


def test_create_with_exactly_what_the_dialog_now_sends(client: TestClient):
    """The dialog's payload, key for key: a picked customer id, calendar dates
    (YYYY-MM-DD, no time) and the price the user entered — the dialog requires
    one, so a blank never turns into a silent $0. Before #684 the dialog sent
    customer_id=null, a free-text customer_name the model never declared and a
    null price, so every create was a 422."""
    r = client.post(
        "/api/service-agreements",
        json={
            "customer_id": CUSTOMER_ID,
            "template_id": None,
            "name": "Spring tune-up plan",
            "start_date": "2026-09-10",
            "end_date": "2027-09-10",
            "price": 249.0,
            "services_included": ["Spring inspection"],
            "notes": "",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["customer_id"] == CUSTOMER_ID
    assert body["customer_name"] == CUSTOMER_NAME
    assert body["price"] == 249.0
    assert body["start_date"].startswith("2026-09-10")
    assert body["status"] == "active"


def test_create_for_a_customer_that_does_not_exist_is_422(client: TestClient):
    r = client.post(
        "/api/service-agreements", json=_agreement_payload(customer_id=str(uuid4()))
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "Customer not found"
    assert client.get("/api/service-agreements").json() == []


def test_every_agreement_response_names_its_customer(client: TestClient):
    """The list's Customer column bound `customer_name`, which no agreement
    response carried, so it read blank on every row."""
    created = client.post("/api/service-agreements", json=_agreement_payload()).json()
    assert created["customer_name"] == CUSTOMER_NAME

    listed = client.get("/api/service-agreements").json()
    assert [row["customer_name"] for row in listed] == [CUSTOMER_NAME]
    assert client.get(f"/api/service-agreements/{created['id']}").json()["customer_name"] == CUSTOMER_NAME
    patched = client.patch(f"/api/service-agreements/{created['id']}", json={"name": "Renamed"}).json()
    assert patched["customer_name"] == CUSTOMER_NAME
    cancelled = client.post(f"/api/service-agreements/{created['id']}/cancel").json()
    assert cancelled["customer_name"] == CUSTOMER_NAME


def test_relinking_to_a_customer_that_does_not_exist_is_422(client: TestClient):
    created = client.post("/api/service-agreements", json=_agreement_payload()).json()
    r = client.patch(
        f"/api/service-agreements/{created['id']}", json={"customer_id": str(uuid4())}
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "Customer not found"
    assert client.get(f"/api/service-agreements/{created['id']}").json()["customer_id"] == CUSTOMER_ID


def test_an_agreement_stays_editable_after_its_customer_is_deleted(client: TestClient):
    """#684 audit: a caller may re-send the current customer_id (the first
    version of the dialog did, on every save), so checking it unconditionally
    locked the agreement of any soft-deleted customer out of renewals,
    re-pricing and expiry. Only a change of customer is checked."""
    created = client.post("/api/service-agreements", json=_agreement_payload()).json()
    Session = sessionmaker(bind=client._engine)  # type: ignore[attr-defined]
    with Session() as db:
        cust = db.get(Customer, UUID(CUSTOMER_ID))
        cust.deleted_at = datetime.now(timezone.utc)
        db.commit()

    r = client.patch(
        f"/api/service-agreements/{created['id']}",
        json={"customer_id": CUSTOMER_ID, "status": "expired", "price": 350.0},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "expired"
    # The list still says whose agreement it is.
    assert r.json()["customer_name"] == CUSTOMER_NAME


def test_relinking_to_a_deleted_customer_is_422(client: TestClient):
    created = client.post("/api/service-agreements", json=_agreement_payload()).json()
    Session = sessionmaker(bind=client._engine)  # type: ignore[attr-defined]
    other_id = uuid4()
    with Session() as db:
        db.add(Customer(id=other_id, name="Gone Customer", company_id="tenant-test",
                        deleted_at=datetime.now(timezone.utc)))
        db.commit()
    r = client.patch(
        f"/api/service-agreements/{created['id']}", json={"customer_id": str(other_id)}
    )
    assert r.status_code == 422


def test_the_response_says_when_the_customer_is_deleted(client: TestClient):
    """The edit dialog labels a gone customer from this flag, not from a guess
    against its own live-only customer list (which fails, caps at 1000, and
    filters names)."""
    created = client.post("/api/service-agreements", json=_agreement_payload()).json()
    assert created["customer_deleted"] is False
    Session = sessionmaker(bind=client._engine)  # type: ignore[attr-defined]
    with Session() as db:
        db.get(Customer, UUID(CUSTOMER_ID)).deleted_at = datetime.now(timezone.utc)
        db.commit()
    listed = client.get("/api/service-agreements").json()
    assert listed[0]["customer_deleted"] is True
    assert listed[0]["customer_name"] == CUSTOMER_NAME


def _updated_rows(client: TestClient) -> list[dict]:
    from gdx_dispatch.core.audit import AuditLog

    Session = sessionmaker(bind=client._engine)  # type: ignore[attr-defined]
    with Session() as db:
        rows = db.execute(
            select(AuditLog)
            .where(AuditLog.action == "service_agreement_updated")
            .order_by(AuditLog.created_at, AuditLog.id)
        ).scalars().all()
        return [r.details for r in rows]


def test_an_edit_audit_row_says_what_changed_old_to_new(client: TestClient):
    """#684 audit. The row used to list the SENT keys, so any save that sent
    more than it changed reported fields nobody touched. It now carries what
    changed, from and to. The payloads are the dialog's real ones: it sends
    only the fields the user changed."""
    created = client.post("/api/service-agreements", json=_agreement_payload()).json()

    r1 = client.patch(f"/api/service-agreements/{created['id']}", json={"notes": "Gate code 4411"})
    assert r1.status_code == 200, r1.text
    r2 = client.patch(f"/api/service-agreements/{created['id']}", json={"price": 0})
    assert r2.status_code == 200, r2.text

    notes_only, repriced = _updated_rows(client)
    assert notes_only["fields"] == ["notes"]
    assert notes_only["changes"]["notes"] == {"from": "Signed on-site", "to": "Gate code 4411"}
    assert repriced["fields"] == ["price"]
    assert repriced["changes"]["price"] == {"from": 299.0, "to": 0.0}


def test_resending_unchanged_values_records_no_changes(client: TestClient):
    """A caller that re-sends the stored values (same timestamps, same notes)
    changed nothing, and the row must say so rather than invent edits."""
    created = client.post("/api/service-agreements", json=_agreement_payload()).json()
    same = {k: created[k] for k in ("name", "start_date", "end_date", "price", "notes", "status")}
    r = client.patch(f"/api/service-agreements/{created['id']}", json=same)
    assert r.status_code == 200, r.text
    assert _updated_rows(client)[-1]["changes"] == {}


def test_re_dating_only_the_end_is_not_a_500(client: TestClient):
    """#684 audit: the end_date branch compared the payload's datetime with the
    stored start_date directly. One naive and one aware is a TypeError — on
    Postgres (aware start) for a date-only end, and here (naive SQLite start)
    for an explicit-offset end. The dialog now sends only a changed end date,
    so this is the path a renewal takes."""
    created = client.post("/api/service-agreements", json=_agreement_payload()).json()
    r = client.patch(
        f"/api/service-agreements/{created['id']}",
        json={"end_date": "2030-01-01T00:00:00+00:00"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["end_date"].startswith("2030-01-01")


def test_create_without_a_price_is_422_not_a_silent_zero(client: TestClient):
    payload = _agreement_payload()
    del payload["price"]
    r = client.post("/api/service-agreements", json=payload)
    assert r.status_code == 422
    assert client.get("/api/service-agreements").json() == []


def test_relinking_to_a_template_that_does_not_exist_is_404(client: TestClient):
    tpl = _create_template(client)
    created = client.post(
        "/api/service-agreements", json=_agreement_payload(template_id=tpl["id"])
    ).json()
    r = client.patch(
        f"/api/service-agreements/{created['id']}", json={"template_id": str(uuid4())}
    )
    assert r.status_code == 404
    assert client.get(f"/api/service-agreements/{created['id']}").json()["template_id"] == tpl["id"]
    # Re-sending the current template is not a relink, even once it is deleted.
    assert client.delete(f"/api/service-agreements/templates/{tpl['id']}").status_code in (200, 204)
    r2 = client.patch(f"/api/service-agreements/{created['id']}", json={"template_id": tpl["id"]})
    assert r2.status_code == 200, r2.text


def test_an_agreement_whose_customer_row_is_gone_reads_as_deleted(client: TestClient):
    """No customer row at all is as gone as a soft-deleted one — the edit
    dialog must not present it as a live customer."""
    created = client.post("/api/service-agreements", json=_agreement_payload()).json()
    Session = sessionmaker(bind=client._engine)  # type: ignore[attr-defined]
    with Session() as db:
        db.delete(db.get(Customer, UUID(CUSTOMER_ID)))
        db.commit()
    row = client.get(f"/api/service-agreements/{created['id']}").json()
    assert row["customer_name"] is None
    assert row["customer_deleted"] is True


def test_create_with_mixed_date_formats_is_not_a_500(client: TestClient):
    """#684 audit: one date-only and one offset date compared naive against
    aware — a TypeError before the handler could answer."""
    r = client.post(
        "/api/service-agreements",
        json=_agreement_payload(start_date="2026-09-10", end_date="2027-09-10T00:00:00+00:00"),
    )
    assert r.status_code == 201, r.text
    bad = client.post(
        "/api/service-agreements",
        json=_agreement_payload(start_date="2027-09-10T00:00:00+00:00", end_date="2026-09-10"),
    )
    assert bad.status_code == 422
