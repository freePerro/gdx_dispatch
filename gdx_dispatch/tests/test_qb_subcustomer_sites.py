"""QuickBooks sub-customers land as saved sites, not as top-level customers.

QB models a project as a sub-customer carrying a `ParentRef` — its UI calls
these "Jobs". The pull read only `DisplayName`, so every one became a
TOP-LEVEL GDX customer: 77 in a single minute on 2026-04-13, six of them under
one lumber-yard account, each inheriting the parent's email. The account's
invoice history stayed on the parent while new estimates were written against
the fragments. See docs/design/qb-subcustomer-flattening-plan.md.

Pinned here, each one a trap an audit named:

1. A sub-customer creates NO customer row — it becomes a `customer_locations`
   row on the parent, mapped as `entity_type="customer_location"`.
2. Those locations are **never** `is_primary`. `core/job_site.py` reads
   `customer.address` only for customers with no primary location and lets a
   primary replace the jobsite for unbound jobs, so a primary here would
   silently repoint every existing job on the account.
3. Parents may arrive AFTER their children — two passes, and an orphan is an
   error row, never silently promoted to a top-level customer.
4. Sub-customer ids stay in `seen_qb_ids`, or the always-on merge-delete probe
   fires a metered QB read per sub-customer on every pull, forever.
5. A sub-customer already flattened by the old code keeps its `customer` map
   until the cleanup migrates it — the pull must not ALSO mint a location, or
   the account carries the same job twice.
6. The leaf name is split off only when the prefix really names the parent, so
   a customer legitimately called "Smith: Auto Body" keeps its name.
7. The sub-customer's own address is read — that is usually the jobsite the
   parent doesn't have.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core.audit import TenantBase
from gdx_dispatch.models import tenant_models  # noqa: F401  (register models)
from gdx_dispatch.models.tenant_models import Customer, CustomerLocation
from gdx_dispatch.modules.quickbooks.sync import (
    _leaf_name,
    _parent_qb_id,
    _qb_address_parts,
    pull_customers,
)

TENANT = "tenant-test"


class _FakeQB:
    """Stands in for QBClient: returns a canned Customer list."""

    def __init__(self, rows):
        self._rows = rows
        self.read_count = 0
        self.gets: list[str] = []

    async def query(self, entity, *a, **k):
        assert entity == "Customer"
        # The real QBClient counts a query as a read (client.py:166). An
        # earlier fake bumped read_count only in `read`, so an
        # `assert read_count == 0` passed by diverging from the class it
        # stands for. Count both; assert on `gets` for probe-specific claims.
        self.read_count += 1
        return list(self._rows)

    async def read(self, entity, qb_id, *a, **k):
        # _detect_qbo_merge_deletes probes here — a live, METERED GET. Record
        # every one so a test can prove sub-customer ids never fall out of
        # seen_qb_ids. Reporting Active keeps the probe a no-op otherwise.
        self.gets.append(f"{entity}/{qb_id}")
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


def _parent(qb_id="35", name="Riverbend Lumber", email="shared@example.invalid"):
    return {"Id": qb_id, "DisplayName": name, "PrimaryEmailAddr": {"Address": email}}


def _child(qb_id, name, parent_id="35", **extra):
    row = {
        "Id": qb_id,
        "DisplayName": name,
        "FullyQualifiedName": f"Riverbend Lumber:{name}",
        "ParentRef": {"value": parent_id},
        "PrimaryEmailAddr": {"Address": "shared@example.invalid"},
    }
    row.update(extra)
    return row


def _run(db, rows):
    qb = _FakeQB(rows)
    result = asyncio.run(pull_customers(TENANT, db, qb))
    return result, qb


def _customers(db):
    return db.execute(
        text("SELECT name FROM customers WHERE deleted_at IS NULL ORDER BY name")
    ).scalars().all()


def _locations(db):
    return db.execute(select_locations()).scalars().all()


def select_locations():
    from sqlalchemy import select

    return select(CustomerLocation).where(CustomerLocation.deleted_at.is_(None))


# ── helper units ────────────────────────────────────────────────────────────


def test_parent_qb_id_reads_parentref():
    assert _parent_qb_id(_child("140", "Site A")) == "35"
    assert _parent_qb_id(_parent()) is None
    assert _parent_qb_id({"Id": "9", "ParentRef": {"value": ""}}) is None


def test_leaf_name_strips_only_a_real_parent_prefix():
    parent = "Riverbend Lumber"
    assert _leaf_name(_child("140", "Site A"), parent) == "Site A"
    # already bare
    assert _leaf_name({"DisplayName": "Site A"}, parent) == "Site A"
    # a name that merely CONTAINS a colon keeps both halves
    kept = _leaf_name({"DisplayName": "Smith: Auto Body"}, parent)
    assert "Smith" in kept and "Auto Body" in kept


def test_qb_address_parts_prefers_billaddr_then_shipaddr():
    row = {"BillAddr": {"Line1": "12 Oak St", "City": "Alexandria",
                        "CountrySubDivisionCode": "MN", "PostalCode": "56308"}}
    assert _qb_address_parts(row) == {
        "address": "12 Oak St", "city": "Alexandria", "state": "MN", "zip": "56308"}
    ship = {"ShipAddr": {"Line1": "9 Elm", "City": "Osakis"}}
    assert _qb_address_parts(ship)["address"] == "9 Elm"
    assert _qb_address_parts({}) == {
        "address": None, "city": None, "state": None, "zip": None}


# ── the behavior that matters ───────────────────────────────────────────────


def test_a_subcustomer_becomes_a_site_not_a_customer(db):
    result, _ = _run(db, [_parent(), _child("140", "Site A")])

    assert _customers(db) == ["Riverbend Lumber"], "the sub-customer must not be a customer"
    locations = _locations(db)
    assert [loc.label for loc in locations] == ["Site A"]
    assert result["sites_created"] == 1
    assert result["created"] == 1  # the parent only
    assert not result["errors"]

    # SQLite stores Customer.id as dash-less hex; customer_locations.customer_id
    # is varchar holding the dashed form the rest of the code writes
    # (routers/customers.py takes it straight off the URL path). Compare as
    # UUIDs so the test pins identity, not formatting.
    parent_id = db.execute(text("SELECT id FROM customers")).scalar()
    assert uuid.UUID(str(locations[0].customer_id)) == uuid.UUID(str(parent_id))


def test_the_site_is_never_primary(db):
    """The single most load-bearing assertion in this file. A primary location
    replaces the jobsite for every unbound job on the account (job_site.py) —
    on an address-less site that turns a real address into 'address missing'."""
    _run(db, [_parent(), _child("140", "Site A"), _child("139", "Site F")])
    assert all(not loc.is_primary for loc in _locations(db))


def test_a_child_arriving_before_its_parent_still_lands(db):
    """QB does not guarantee parents come first."""
    result, _ = _run(db, [_child("140", "Site A"), _parent()])
    assert not result["errors"], result["errors"]
    assert result["sites_created"] == 1
    assert _customers(db) == ["Riverbend Lumber"]


def test_an_orphan_is_an_error_never_a_new_customer(db):
    """Silently promoting an orphan is the exact bug being fixed."""
    result, _ = _run(db, [_child("140", "Site A", parent_id="999")])
    assert result["sites_created"] == 0
    assert _customers(db) == []
    assert len(result["errors"]) == 1
    assert result["errors"][0]["qb_id"] == "140"


def test_the_site_carries_the_subcustomers_own_address(db):
    """A builder makes a sub-customer per project precisely because the project
    has an address the parent doesn't."""
    _run(db, [_parent(), _child("140", "Site A", BillAddr={
        "Line1": "12 Oak St", "City": "Alexandria",
        "CountrySubDivisionCode": "MN", "PostalCode": "56308"})])
    loc = _locations(db)[0]
    assert loc.address == "12 Oak St"
    assert (loc.city, loc.state, loc.zip) == ("Alexandria", "MN", "56308")


def test_a_second_pull_updates_the_same_site_rather_than_duplicating(db):
    rows = [_parent(), _child("140", "Site A")]
    _run(db, rows)
    result, _ = _run(db, [_parent(), _child("140", "Site A Renamed")])
    assert len(_locations(db)) == 1, "a re-pull must not mint a second site"
    assert _locations(db)[0].label == "Site A Renamed"
    # "created: 1" on a no-op re-pull reads as work that did not happen, and
    # that number goes into the audit event.
    assert result["sites_created"] == 0
    assert result["sites_updated"] == 1


def test_a_re_pull_never_blanks_a_known_address_with_an_empty_qb_row(db):
    _run(db, [_parent(), _child("140", "Site A", BillAddr={"Line1": "12 Oak St"})])
    _run(db, [_parent(), _child("140", "Site A")])  # QB now has no address
    assert _locations(db)[0].address == "12 Oak St"


def _seed_legacy_flattened(db):
    """The pre-fix state: the sub-customer exists as its OWN top-level customer
    with an entity_type='customer' map, exactly as the old pull left it."""
    parent_id = uuid.uuid4()
    legacy_id = uuid.uuid4()
    db.add_all([
        Customer(id=parent_id, name="Riverbend Lumber", company_id=TENANT),
        Customer(id=legacy_id, name="Site A", company_id=TENANT),
    ])
    db.flush()
    now = datetime.now(UTC)
    db.execute(text(
        "INSERT INTO qb_entity_maps (id, tenant_id, entity_type, local_id, qb_id, synced_at) "
        "VALUES (:i1, :t, 'customer', :l1, '35', :ts), "
        "       (:i2, :t, 'customer', :l2, '140', :ts)"),
        # QBEntityMap.id is Uuid(as_uuid=True); SQLite stores it as dash-less
        # hex, so a dashed literal here inserts a row the ORM's own UPDATE
        # then fails to match (StaleDataError, 0 rows).
        {"i1": uuid.uuid4().hex, "i2": uuid.uuid4().hex, "t": TENANT,
         "l1": str(parent_id), "l2": str(legacy_id), "ts": now})
    db.commit()
    return parent_id, legacy_id


def test_an_already_flattened_subcustomer_is_left_for_the_cleanup(db):
    """Legacy rows keep their `customer` map until the PR-5 migration. The pull
    must not ALSO mint a site, or the account carries the job twice."""
    _seed_legacy_flattened(db)

    result, _ = _run(db, [_parent(), _child("140", "Site A")])
    assert result["legacy_subs"] == 1
    assert result["sites_created"] == 0
    assert _locations(db) == [], "no site while the legacy customer row still stands"
    assert sorted(_customers(db)) == ["Riverbend Lumber", "Site A"]


def test_a_legacy_subcustomer_id_stays_in_the_seen_set(db):
    """_detect_qbo_merge_deletes is always-on with no feature flag: it probes
    every entity_type='customer' map missing from seen_qb_ids with a live,
    METERED read. A sub-customer that is still flattened HAS such a map, so
    dropping its id from the seen set bills an extra read on every pull —
    and, with delete-sync enabled, would soft-delete the row.

    This has to be set up as a LEGACY row: once a sub-customer is a saved site
    its map is entity_type='customer_location', which this probe never looks
    at, so a version of this test without the legacy map passes either way and
    proves nothing. It did, until a mutation run caught it.
    """
    _seed_legacy_flattened(db)
    _, qb = _run(db, [_parent(), _child("140", "Site A")])
    assert qb.gets == [], f"metered probe fired for a sub-customer present in QB: {qb.gets}"


def test_top_level_customers_still_pull_exactly_as_before(db):
    """The two-pass split must not disturb the ordinary path."""
    result, _ = _run(db, [
        _parent(),
        {"Id": "77", "DisplayName": "Someone Else",
         "PrimaryEmailAddr": {"Address": "someone@example.invalid"},
         "PrimaryPhone": {"FreeFormNumber": "218-555-0100"}},
    ])
    assert result["created"] == 2
    assert result["sites_created"] == 0
    assert sorted(_customers(db)) == ["Riverbend Lumber", "Someone Else"]
    phone = db.execute(
        text("SELECT phone FROM customers WHERE name = 'Someone Else'")).scalar()
    assert phone == "218-555-0100"


def test_a_legacy_subcustomer_still_gets_its_name_email_and_phone_refresh(db):
    """The regression the third audit caught, pinned.

    An earlier draft wrote only `synced_at` for already-flattened rows while a
    comment claimed it touched them "exactly as before". On this tenant every
    real sub-customer takes this branch, so that draft's only production
    effect would have been to silently freeze their contact details.
    """
    _, legacy_id = _seed_legacy_flattened(db)

    result, _ = _run(db, [_parent(), _child(
        "140", "Site A Renamed",
        PrimaryEmailAddr={"Address": "site.a@example.invalid"},
        PrimaryPhone={"FreeFormNumber": "218-555-0142"})])

    row = db.execute(
        text("SELECT name, email, phone FROM customers WHERE id = :i"),
        {"i": legacy_id.hex},
    ).first()
    assert row.name == "Site A Renamed"
    assert row.email == "site.a@example.invalid"
    assert row.phone == "218-555-0142"
    assert result["legacy_subs"] == 1
    assert result["updated"] >= 1, "a refreshed legacy row counts as an update"


def test_a_nested_subcustomer_lands_on_the_root_customer(db):
    """QBO nests Jobs under Jobs. A one-level parent lookup calls every
    grandchild an orphan and raises on it on every pull, forever."""
    grandchild = _child("200", "Unit 3", parent_id="140")
    result, _ = _run(db, [_parent(), _child("140", "Site A"), grandchild])

    assert not result["errors"], result["errors"]
    assert _customers(db) == ["Riverbend Lumber"]
    labels = sorted(loc.label for loc in _locations(db))
    assert labels == ["Site A", "Site A / Unit 3"], labels


def test_a_parentref_cycle_is_an_error_not_a_hang(db):
    a = _child("300", "A", parent_id="301")
    b = _child("301", "B", parent_id="300")
    result, _ = _run(db, [_parent(), a, b])
    assert len(result["errors"]) == 2
    assert _locations(db) == []


def test_a_site_a_human_deleted_is_not_resurrected(db):
    _run(db, [_parent(), _child("140", "Site A")])
    loc = _locations(db)[0]
    db.execute(text("UPDATE customer_locations SET deleted_at = :ts WHERE id = :i"),
               {"ts": datetime.now(UTC), "i": str(loc.id)})
    db.commit()

    result, _ = _run(db, [_parent(), _child("140", "Site A")])
    assert result["sites_skipped_deleted"] == 1
    assert result["sites_created"] == 0
    assert _locations(db) == [], "QB must not undo a human's delete"


def test_creating_a_site_writes_an_audit_row(db):
    """Invariant #1 — a run-level count cannot answer which site, whose
    account, when."""
    _run(db, [_parent(), _child("140", "Site A")])
    actions = db.execute(text("SELECT action FROM audit_logs")).scalars().all()
    assert "qb_subcustomer_site_created" in actions

    _run(db, [_parent(), _child("140", "Site A")])
    actions = db.execute(text("SELECT action FROM audit_logs")).scalars().all()
    assert "qb_subcustomer_site_updated" in actions
