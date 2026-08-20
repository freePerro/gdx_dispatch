"""A QuickBooks pull stops overwriting what a human corrected in GDX.

`pull_customers` assigned `customer.name/.email/.phone` unconditionally for
every mapped row. Two consequences, both live in production for months:

  * a QB row with no `PrimaryEmailAddr` **blanked** a good GDX email — QB not
    knowing a value was written as the customer not having one;
  * any correction made in the office was silently overwritten on the next
    sync, with no trace. The only record was one run-level count row
    (`qb_pull_customers`, actor `system`, "260 updated"), which cannot say
    which customer or which field.

Doug's call 2026-08-19: GDX wins on rows a human has edited. `local_edit_at`
is the marker and its PRESENCE decides — see migration 070 for why comparing
it against `qb_entity_maps.synced_at` is broken (that column is re-stamped on
every pull, including no-ops, so the comparison is permanently False after one
sync and "GDX wins" silently stops winning).
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import Generator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.models.tenant_models import Customer
from gdx_dispatch.modules.quickbooks.sync import pull_customers

TENANT = "tenant-test"


class _FakeQB:
    def __init__(self, rows):
        self._rows = rows
        self.read_count = 0

    async def query(self, entity, *a, **k):
        self.read_count += 1
        return list(self._rows)

    async def read(self, entity, qb_id, *a, **k):
        self.read_count += 1
        return {"Active": True}


@pytest.fixture()
def db() -> Generator[Session, None, None]:
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


def _qb_row(qb_id="35", name="Riverbend Lumber", email=None, phone=None):
    row: dict = {"Id": qb_id, "DisplayName": name}
    if email is not None:
        row["PrimaryEmailAddr"] = {"Address": email}
    if phone is not None:
        row["PrimaryPhone"] = {"FreeFormNumber": phone}
    return row


def _seed_mapped(db, *, name, email=None, phone=None, locally_edited=False,
                 owned=("name", "email", "phone"), qb_id="35"):
    cid = uuid.uuid4()
    db.add(Customer(
        id=cid, name=name, email=email, phone=phone, company_id=TENANT,
        local_edit_at=datetime.now(UTC) if locally_edited else None,
        local_edit_fields=sorted(owned) if locally_edited else None,
    ))
    db.flush()
    db.execute(text(
        "INSERT INTO qb_entity_maps (id, tenant_id, entity_type, local_id, qb_id, synced_at) "
        "VALUES (:i, :t, 'customer', :l, :q, :ts)"),
        {"i": uuid.uuid4().hex, "t": TENANT, "l": str(cid), "q": qb_id,
         "ts": datetime.now(UTC)})
    db.commit()
    return cid


def _run(db, rows):
    return asyncio.run(pull_customers(TENANT, db, _FakeQB(rows)))


def _row(db, cid):
    return db.execute(
        text("SELECT name, email, phone FROM customers WHERE id = :i"), {"i": cid.hex}
    ).first()


def _audit_actions(db):
    return db.execute(text("SELECT action FROM audit_logs")).scalars().all()


# ── rule 2: QB never blanks ─────────────────────────────────────────────────


def test_a_qb_row_with_no_email_never_blanks_a_good_one(db):
    """The four-month-old data-loss bug, pinned."""
    cid = _seed_mapped(db, name="Riverbend Lumber", email="front@example.invalid",
                       phone="218-555-0100")
    _run(db, [_qb_row()])  # QB knows neither email nor phone
    row = _row(db, cid)
    assert row.email == "front@example.invalid"
    assert row.phone == "218-555-0100"


def test_qb_still_fills_a_field_gdx_does_not_have(db):
    cid = _seed_mapped(db, name="Riverbend Lumber", email=None)
    _run(db, [_qb_row(email="front@example.invalid")])
    assert _row(db, cid).email == "front@example.invalid"


# ── rule 1: a locally-edited row belongs to GDX ─────────────────────────────


def test_a_locally_edited_customer_is_not_overwritten(db):
    """Doug edits a customer in the office; the next sync leaves it alone."""
    cid = _seed_mapped(db, name="Riverbend Lumber Yard", email="office@example.invalid",
                       phone="218-555-0100", locally_edited=True)
    _run(db, [_qb_row(name="RIVERBEND LUMBER", email="stale@example.invalid",
                      phone="218-555-9999")])
    row = _row(db, cid)
    assert row.name == "Riverbend Lumber Yard"
    assert row.email == "office@example.invalid"
    assert row.phone == "218-555-0100"


def test_qb_still_fills_a_field_the_human_never_touched(db):
    """Ownership is per FIELD. Editing the name does not freeze the email."""
    cid = _seed_mapped(db, name="Riverbend Lumber Yard", email=None,
                       locally_edited=True, owned=("name",))
    _run(db, [_qb_row(name="RIVERBEND", email="front@example.invalid")])
    row = _row(db, cid)
    assert row.name == "Riverbend Lumber Yard", "the owned field held"
    assert row.email == "front@example.invalid", "the unowned field filled"


def test_an_untouched_customer_still_takes_qbs_name(db):
    """Nothing changes for rows no human has edited — the default stays QB."""
    cid = _seed_mapped(db, name="riverbend lumber")
    _run(db, [_qb_row(name="Riverbend Lumber")])
    assert _row(db, cid).name == "Riverbend Lumber"


def test_ownership_does_not_expire_after_a_sync(db):
    """The broken first design compared local_edit_at against
    qb_entity_maps.synced_at, which _upsert_map re-stamps on EVERY pull — so
    after one sync the comparison went permanently False and GDX quietly
    stopped winning. Two pulls in a row must both leave the row alone."""
    cid = _seed_mapped(db, name="Riverbend Lumber Yard", email="office@example.invalid",
                       locally_edited=True)
    _run(db, [_qb_row(name="RIVERBEND", email="stale@example.invalid")])
    _run(db, [_qb_row(name="RIVERBEND", email="stale@example.invalid")])
    row = _row(db, cid)
    assert row.name == "Riverbend Lumber Yard"
    assert row.email == "office@example.invalid"


# ── the trail ───────────────────────────────────────────────────────────────


def test_an_overwrite_leaves_a_per_row_audit_naming_the_fields(db):
    """A run-level count of '260 updated' cannot answer which customer or
    which field, so prior values were unrecoverable after the fact."""
    _seed_mapped(db, name="riverbend lumber")
    _run(db, [_qb_row(name="Riverbend Lumber", email="front@example.invalid")])

    assert "qb_customer_identity_overwritten" in _audit_actions(db)
    details = db.execute(text(
        "SELECT details FROM audit_logs WHERE action = 'qb_customer_identity_overwritten'"
    )).scalar()
    assert "name" in str(details) and "email" in str(details)


def test_the_audit_row_records_field_names_never_values(db):
    _seed_mapped(db, name="riverbend lumber")
    _run(db, [_qb_row(name="Riverbend Lumber", email="secret@example.invalid")])
    blob = " ".join(str(r) for r in db.execute(
        text("SELECT details FROM audit_logs")).scalars().all())
    assert "email" in blob
    assert "secret@example.invalid" not in blob


def test_a_no_op_pull_writes_no_audit_row_and_no_update(db):
    """Auditing an unchanged row would drown the trail it exists to provide."""
    _seed_mapped(db, name="Riverbend Lumber", email="front@example.invalid")
    result = _run(db, [_qb_row(name="Riverbend Lumber", email="front@example.invalid")])
    assert result["updated"] == 0
    assert "qb_customer_identity_overwritten" not in _audit_actions(db)


# ── the writers set the marker (behaviour, not grep) ───────────────────────


def test_the_office_editor_claims_only_the_fields_it_changed(db):
    """Both edit dialogs always SEND name/email/phone, so "is the key
    present" was true on every save — a pricing-class change filed itself as
    a claim of ownership over the customer's identity."""
    import asyncio as _aio
    from types import SimpleNamespace

    from gdx_dispatch.routers.customers import CustomerUpdateIn, update_customer

    cid = _seed_mapped(db, name="Riverbend Lumber", email="front@example.invalid")
    req = SimpleNamespace(state=SimpleNamespace(tenant={"id": TENANT}),
                          headers={}, client=SimpleNamespace(host="127.0.0.1"))
    # same values back, plus an unrelated field
    _aio.run(update_customer(
        str(cid),
        CustomerUpdateIn(name="Riverbend Lumber", email="front@example.invalid",
                         pricing_class="wholesale"),
        req, {"sub": "office"}, db))
    got = db.execute(select(Customer.local_edit_fields).where(Customer.id == cid)).scalar()
    assert not got, f"an unrelated edit must not claim identity fields, got {got}"

    _aio.run(update_customer(
        str(cid), CustomerUpdateIn(email="corrected@example.invalid"),
        req, {"sub": "office"}, db))
    got = db.execute(select(Customer.local_edit_fields).where(Customer.id == cid)).scalar()
    assert got == ["email"], got


def test_a_deleted_email_stays_deleted_across_a_sync(db):
    """The inversion the third audit caught, pinned.

    The office clears a wrong address. Ownership keyed on "edited AND
    currently non-empty" reads the now-empty field as unowned and hands it
    straight back to QB, which writes the wrong address in again — failing on
    the single edit most worth protecting.
    """
    import asyncio as _aio
    from types import SimpleNamespace

    from gdx_dispatch.routers.customers import CustomerUpdateIn, update_customer

    cid = _seed_mapped(db, name="Riverbend Lumber", email="wrong@example.invalid")
    req = SimpleNamespace(state=SimpleNamespace(tenant={"id": TENANT}),
                          headers={}, client=SimpleNamespace(host="127.0.0.1"))
    _aio.run(update_customer(str(cid), CustomerUpdateIn(email=""), req, {"sub": "office"}, db))
    assert _row(db, cid).email is None

    _run(db, [_qb_row(email="wrong@example.invalid")])
    assert _row(db, cid).email is None, "QB put back an address a human deleted"


def test_the_mobile_editor_claims_only_the_fields_it_changed(db):
    from gdx_dispatch.routers.mobile import CustomerContactPatch

    # the mobile patch computes `changed` by value comparison already; this
    # pins that the ownership list is built from THAT, not from the payload
    src = __import__("pathlib").Path("gdx_dispatch/routers/mobile.py").read_text()
    assert "set(customer.local_edit_fields or []) | set(changed)" in src
    assert set(CustomerContactPatch.model_fields) <= {"name", "phone", "email"}


def test_the_csv_import_claims_ownership_too(db):
    """A person uploading a corrected customer list is editing as
    deliberately as one typing in the dialog."""
    src = __import__("pathlib").Path("gdx_dispatch/routers/admin_ops.py").read_text()
    assert "existing.local_edit_fields = sorted(claimed)" in src


def test_the_legacy_duplicate_pull_is_guarded_too(db):
    """core/quickbooks.py holds an older copy of pull_customers with the same
    unconditional assignment. An unguarded copy of a fixed bug is the bug."""
    src = __import__("pathlib").Path("gdx_dispatch/core/quickbooks.py").read_text()
    assert "if not value or field in owned:" in src


def test_the_migration_is_additive_and_runs_on_both_dialects():
    src = __import__("pathlib").Path(
        "gdx_dispatch/migrations/versions/070_customer_local_edit_at.py").read_text()
    assert 'down_revision = "069_invoice_line_includes_labor"' in src
    assert "TIMESTAMPTZ NULL" in src and "JSONB NULL" in src          # postgres
    assert "ADD COLUMN local_edit_at TIMESTAMP NULL" in src           # sqlite
    assert "ADD COLUMN local_edit_fields TEXT NULL" in src            # sqlite
    assert "DROP COLUMN IF EXISTS local_edit_fields" in src           # rollback


def test_ownership_is_empty_for_untouched_rows(db):
    cid = _seed_mapped(db, name="Riverbend Lumber")
    assert not db.execute(
        select(Customer.local_edit_fields).where(Customer.id == cid)).scalar()
