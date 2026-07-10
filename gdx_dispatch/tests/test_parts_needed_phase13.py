"""Phase 1.3 (sprint_tech_mobile) — Parts Requests test suite.

Covers C1 (modal-shaped create), C3 (tech-edit-while-needed gate),
C6 (ETA), C7 (SKU autocomplete from parts + door_catalog with free-text
fallback), and C8 (role gate on status changes).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.modules.inventory import models as _inv  # noqa: F401  (register Part)
from gdx_dispatch.routers import parts_needed as pr


_TENANT_ID = "tenant-a"
_JOB_ID = uuid4().hex
_TECH = {"user_id": "tech-1", "role": "technician"}
_DISPATCH = {"user_id": "disp-1", "role": "dispatcher"}
_OWNER = {"user_id": "owner-1", "role": "owner"}


def _request() -> Request:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    req.state.tenant = {"id": _TENANT_ID}
    return req


@pytest.fixture()
def db(tmp_path):
    eng = create_engine(
        f"sqlite:///{tmp_path / 'parts.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    TenantBase.metadata.create_all(eng, checkfirst=True)
    Session = sessionmaker(bind=eng, autoflush=False, autocommit=False)
    s = Session()
    # Minimal customer + job rows so audit/list calls have something to hang on.
    s.execute(text(
        "INSERT INTO customers (id, name, company_id) VALUES (:i, 'Acme', :t)"
    ), {"i": uuid4().hex, "t": _TENANT_ID})
    yield s
    s.close()
    eng.dispose()


def _create(db, user=_TECH, **overrides):
    payload = pr.PartNeededIn(
        part_name=overrides.get("part_name", "Torsion spring 2in 27c"),
        quantity=overrides.get("quantity", 2),
        supplier=overrides.get("supplier", "DDM"),
        urgency=overrides.get("urgency", "normal"),
        notes=overrides.get("notes", ""),
        sku=overrides.get("sku"),
        photo_url=overrides.get("photo_url"),
        unit_price=overrides.get("unit_price"),
    )
    return pr.add_part_needed(_JOB_ID, _request(), payload, user=user, db=db)


# ---------------------------------------------------------------------------
# C1 — modal-shaped create stamps SKU, photo_url, requested_by_user_id.
# ---------------------------------------------------------------------------

def test_c1_create_stamps_attribution_sku_photo(db):
    out = _create(db, sku="TS-2-27", photo_url="https://r2/photo.jpg")
    assert out["sku"] == "TS-2-27"
    assert out["photo_url"] == "https://r2/photo.jpg"
    assert out["requested_by_user_id"] == "tech-1"
    assert out["status"] == "needed"


def test_c1_freetext_fallback_leaves_sku_null(db):
    out = _create(db, sku=None, part_name="weird custom thing")
    assert out["sku"] is None
    assert out["part_name"] == "weird custom thing"


# ---------------------------------------------------------------------------
# Catalog intake — a part queued from the catalog picker carries the catalog
# SELL price so the invoice-create checklist prefills the line unit_price.
# ---------------------------------------------------------------------------

def test_catalog_create_persists_unit_price(db):
    out = _create(db, part_name="Belt Drive Opener", sku="BD-450", unit_price=450)
    assert out["unit_price"] == 450.0
    # Round-trips through the list view the checklist reads, not just the
    # create response.
    rows = pr.list_job_parts(_JOB_ID, _request(), user=_DISPATCH, db=db)
    match = next(r for r in rows if r["id"] == out["id"])
    assert match["unit_price"] == 450.0


def test_catalog_unit_price_optional_defaults_null(db):
    # Free-text / manual "+ Order Part" flow sends no price → NULL (office
    # prices it on the invoice), unchanged from the legacy contract.
    out = _create(db, part_name="mystery part")
    assert out["unit_price"] is None


# ---------------------------------------------------------------------------
# C6 — dispatcher PATCH /status accepts eta_at; tech sees it on list.
# ---------------------------------------------------------------------------

def test_c6_dispatcher_sets_eta_and_status(db):
    created = _create(db)
    eta = datetime.now(timezone.utc) + timedelta(days=2)
    out = pr.update_part_status(
        created["id"], _request(),
        pr.PartStatusUpdate(status="ordered", eta_at=eta),
        user=_DISPATCH, db=db,
    )
    assert out["status"] == "ordered"
    assert out["eta_at"] is not None

    listing = pr.list_job_parts(_JOB_ID, _request(), user=_TECH, db=db)
    assert listing[0]["status"] == "ordered"
    assert listing[0]["eta_at"] is not None


# ---------------------------------------------------------------------------
# C8 — role gate: techs cannot flip status; dispatcher / admin / owner can.
# ---------------------------------------------------------------------------

def test_c8_tech_cannot_flip_status(db):
    created = _create(db)
    with pytest.raises(HTTPException) as exc:
        pr.update_part_status(
            created["id"], _request(),
            pr.PartStatusUpdate(status="ordered"),
            user=_TECH, db=db,
        )
    assert exc.value.status_code == 403


def test_c8_owner_can_flip_status(db):
    created = _create(db)
    out = pr.update_part_status(
        created["id"], _request(),
        pr.PartStatusUpdate(status="received"),
        user=_OWNER, db=db,
    )
    assert out["status"] == "received"


# ---------------------------------------------------------------------------
# C3 — tech edits while status='needed', locked once dispatch flips it.
# ---------------------------------------------------------------------------

def test_c3_tech_edit_while_needed_succeeds(db):
    created = _create(db, quantity=1)
    out = pr.tech_edit_part(
        created["id"], _request(),
        pr.PartNeededTechUpdate(quantity=4, notes="bring two extras"),
        user=_TECH, db=db,
    )
    assert out["quantity"] == 4
    assert out["notes"] == "bring two extras"


def test_c3_tech_edit_locked_once_ordered(db):
    created = _create(db)
    pr.update_part_status(
        created["id"], _request(),
        pr.PartStatusUpdate(status="ordered"),
        user=_DISPATCH, db=db,
    )
    with pytest.raises(HTTPException) as exc:
        pr.tech_edit_part(
            created["id"], _request(),
            pr.PartNeededTechUpdate(quantity=99),
            user=_TECH, db=db,
        )
    assert exc.value.status_code == 409


def test_c3_dispatch_can_still_edit_after_ordered(db):
    """Office can correct typos for the tech even after flipping status."""
    created = _create(db)
    pr.update_part_status(
        created["id"], _request(),
        pr.PartStatusUpdate(status="ordered"),
        user=_DISPATCH, db=db,
    )
    out = pr.tech_edit_part(
        created["id"], _request(),
        pr.PartNeededTechUpdate(notes="vendor confirmed via phone"),
        user=_DISPATCH, db=db,
    )
    assert "vendor confirmed" in out["notes"]


# ---------------------------------------------------------------------------
# C7 — SKU autocomplete merges parts + door_catalog, scoped to tenant.
# ---------------------------------------------------------------------------

def _seed_part(db, sku, name, vendor_sku=None):
    db.execute(text(
        "INSERT INTO parts (id, sku, name, unit_cost, unit_price, qty_on_hand, "
        "reorder_point, vendor_sku, created_at) "
        "VALUES (:i, :s, :n, 0, 0, 5, 0, :v, :ca)"
    ), {
        "i": uuid4().hex, "s": sku, "n": name,
        "v": vendor_sku, "ca": datetime.now(timezone.utc),
    })
    db.commit()


def _seed_door(db, sku, model, desc):
    db.execute(text(
        "INSERT INTO chi_door_catalog (id, sku, model_number, description, "
        "is_custom, is_active, imported_at) "
        "VALUES (:i, :s, :m, :d, 0, 1, :ia)"
    ), {
        "i": uuid4().hex, "s": sku, "m": model, "d": desc,
        "ia": datetime.now(timezone.utc),
    })
    db.commit()


def test_c7_sku_suggest_returns_parts_first(db):
    _seed_part(db, "TS-2-27", "Torsion spring 2in 27 coil")
    _seed_part(db, "TS-2-29", "Torsion spring 2in 29 coil")
    _seed_door(db, "CHI-3220", "3220", "16x7 Insulated steel door")

    out = pr.sku_suggest(_request(), q="ts-2", limit=10, user=_TECH, db=db)
    assert any(s["source"] == "parts" and s["sku"] == "TS-2-27" for s in out)
    # The 'ts-2' query shouldn't match the door catalog SKU/model/desc.
    assert all(s["source"] == "parts" for s in out)


def test_c7_sku_suggest_falls_through_to_door_catalog(db):
    _seed_door(db, "CHI-3220", "3220", "16x7 Insulated steel door")
    out = pr.sku_suggest(_request(), q="3220", limit=10, user=_TECH, db=db)
    assert len(out) == 1
    assert out[0]["source"] == "door_catalog"
    assert out[0]["sku"] == "CHI-3220"


def test_c7_sku_suggest_empty_query_returns_empty(db):
    _seed_part(db, "TS-2-27", "Torsion spring")
    assert pr.sku_suggest(_request(), q="", limit=10, user=_TECH, db=db) == []


def test_c7_sku_suggest_no_match_returns_empty_for_freetext_fallback(db):
    out = pr.sku_suggest(_request(), q="zzznoexist", limit=10, user=_TECH, db=db)
    assert out == []


# ---------------------------------------------------------------------------
# C5 — dispatch-config exposes the audible-ping setting for the tenant.
# ---------------------------------------------------------------------------

def test_c5_dispatch_config_default_audible_true(db):
    """Tenant with no override → catalog default (True)."""
    out = pr.dispatch_config(_request(), user=_DISPATCH, db=db)
    assert out == {"audible_critical": True}


def test_c5_dispatch_config_respects_override(db):
    """When the tenant flips the setting off, the endpoint reports it."""
    from gdx_dispatch.models.tenant_models import AppSettings
    db.add(AppSettings(
        company_name="Acme",
        address="-",
        tenant_mobile_settings={"tech_mobile.critical_part_audible": False},
    ))
    db.commit()
    out = pr.dispatch_config(_request(), user=_DISPATCH, db=db)
    assert out == {"audible_critical": False}


# ---------------------------------------------------------------------------
# S122 — list_job_parts status + unbilled filters.
# ---------------------------------------------------------------------------

def _flip_status(db, part_id: str, new_status: str) -> None:
    pr.update_part_status(
        part_id, _request(),
        pr.PartStatusUpdate(status=new_status),
        user=_DISPATCH, db=db,
    )


def test_s122_list_status_filter_single(db):
    """status='received' returns only received parts."""
    p1 = _create(db, part_name="Spring 1")
    p2 = _create(db, part_name="Spring 2")
    _flip_status(db, p1["id"], "received")

    received_only = pr.list_job_parts(_JOB_ID, _request(), user=_TECH, db=db, status="received")
    assert [r["part_name"] for r in received_only] == ["Spring 1"]


def test_s122_list_status_filter_multi(db):
    """status='ordered,received' returns both, no needed."""
    p1 = _create(db, part_name="A")
    p2 = _create(db, part_name="B")
    p3 = _create(db, part_name="C")
    _flip_status(db, p1["id"], "ordered")
    _flip_status(db, p2["id"], "received")
    # p3 stays "needed"

    out = pr.list_job_parts(_JOB_ID, _request(), user=_TECH, db=db, status="ordered,received")
    names = sorted(r["part_name"] for r in out)
    assert names == ["A", "B"]


def test_s122_list_unbilled_excludes_billed(db):
    """unbilled=True excludes parts with billed_invoice_id set."""
    from uuid import uuid4

    from gdx_dispatch.models.tenant_models import JobPartNeeded

    p1 = _create(db, part_name="Unbilled")
    p2 = _create(db, part_name="Billed")
    # Mark p2 as billed.
    row = db.query(JobPartNeeded).filter(JobPartNeeded.id == p2["id"]).one()
    row.billed_invoice_id = uuid4()
    db.commit()

    all_rows = pr.list_job_parts(_JOB_ID, _request(), user=_TECH, db=db)
    unbilled_only = pr.list_job_parts(_JOB_ID, _request(), user=_TECH, db=db, unbilled=True)

    assert len(all_rows) == 2
    assert [r["part_name"] for r in unbilled_only] == ["Unbilled"]


def test_s122_list_combined_status_and_unbilled(db):
    """status='received' AND unbilled=True applied together."""
    from uuid import uuid4

    from gdx_dispatch.models.tenant_models import JobPartNeeded

    p1 = _create(db, part_name="R-unbilled")
    p2 = _create(db, part_name="R-billed")
    p3 = _create(db, part_name="N-unbilled")
    _flip_status(db, p1["id"], "received")
    _flip_status(db, p2["id"], "received")
    # p3 stays needed
    row = db.query(JobPartNeeded).filter(JobPartNeeded.id == p2["id"]).one()
    row.billed_invoice_id = uuid4()
    db.commit()

    out = pr.list_job_parts(
        _JOB_ID, _request(), user=_TECH, db=db,
        status="ordered,received", unbilled=True,
    )
    assert [r["part_name"] for r in out] == ["R-unbilled"]


def test_s122_serialize_exposes_billed_invoice_id(db):
    """The serializer surfaces billed_invoice_id so the frontend can render it."""
    from uuid import uuid4

    from gdx_dispatch.models.tenant_models import JobPartNeeded

    p1 = _create(db, part_name="Tagged")
    inv_id = uuid4()
    row = db.query(JobPartNeeded).filter(JobPartNeeded.id == p1["id"]).one()
    row.billed_invoice_id = inv_id
    db.commit()

    out = pr.list_job_parts(_JOB_ID, _request(), user=_TECH, db=db)
    assert out[0]["billed_invoice_id"] == str(inv_id)


def test_s122_serialize_billed_invoice_id_null_default(db):
    """Default state — no billed_invoice_id surfaces None."""
    _create(db, part_name="Clean")
    out = pr.list_job_parts(_JOB_ID, _request(), user=_TECH, db=db)
    assert out[0]["billed_invoice_id"] is None
