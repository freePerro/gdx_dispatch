"""Effective-jobsite resolution — the ONE precedence rule, server-side.

Desktop JobDetailView.vue (pickedLocation/customerAddress computeds) defined
the rule; this module is that rule in Python so mobile serializers cannot
drift from the office screen:

1. Bound location (``jobs.location_id`` → non-deleted ``customer_locations``
   row): its address wins. A bound row with NO address surfaces as
   ``address_missing=True`` and NEVER falls through to the customer — the
   tech would see "Warehouse #3" on the label and drive to the HQ (the
   desktop /audit 2026-05-21 rule).
2. No binding (or the bound row is soft-deleted): the customer's
   EXPLICITLY-PRIMARY location, if one exists. A primary with no address is
   MISSING (same D2 honesty as a bound row). Non-primary rows are jobsites
   of OTHER jobs — never authority for a null-location job: the earlier
   first-created-row fallback (desktop parity) meant the first "New
   address…" save re-addressed every existing null-location job for that
   customer (post-code audit PR 2 §1 — the rule had never met real rows
   because the create endpoint was broken on Postgres until PR 2).
   Access notes likewise come only from the explicit primary.
3. Zero location rows: ``customer.address`` (fetched via the ORM so
   ``EncryptedString`` decrypts — never add a raw-SQL read here).

Batched: three bounded queries per call regardless of item count. List
endpoints must call :func:`resolve_job_sites` once per page, never
:func:`resolve_job_site` per row (the /today hot path is the reason this
module exists as a batch).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import bindparam
from sqlalchemy import text as _text


@dataclass(frozen=True)
class JobSite:
    """Where the work actually is, plus how sure we are.

    ``address_missing`` is True only when a location row was selected but
    carries no address — a different state from "customer has nothing on
    file" (``source='customer'`` with ``address=None``), because the first
    means "a specific site exists, go ask which", the second "no data".

    ``lat``/``lng`` are the BOUND location row's stored coordinates (post-code
    audit 2026-08-18 §1: the correct map pin for a bound site exists in
    ``customer_locations`` and must beat any appointment-geocoded guess).
    Fallback sources leave them None — only a bound row is specific enough
    to re-pin from.
    """

    label: str | None = None
    address: str | None = None
    address_missing: bool = False
    access_notes: str | None = None
    # "location" (bound row) | "customer_location" (primary/first fallback)
    # | "customer" (customers.address) | "none" (no customer on the job)
    source: str = "none"
    lat: float | None = None
    lng: float | None = None


def normalize_address(value: str | None) -> str:
    """Casefolded, whitespace/punctuation-collapsed form for EQUALITY only.

    "9 Dock St." and "9  dock st" are the same place; a naive `!=` between
    them is how the pin guard mis-fired in review. Never store this form.
    """
    if not value:
        return ""
    out = "".join(ch for ch in value.casefold() if ch not in ".,#")
    return " ".join(out.split())


_EMPTY = JobSite()


def _s(value: Any) -> str | None:
    return str(value) if value is not None else None


def _cs(value: Any) -> str | None:
    """Canonical (dashed) UUID string for CUSTOMER ids.

    Raw-SQL rows on SQLite hand back ``jobs.customer_id`` as 32-char undashed
    hex, while ``customer_locations.customer_id`` rows and ORM ``str(UUID)``
    keys are dashed — an id in the wrong dress silently misses every lookup
    (caught by test_mobile_api.py::test_get_job_detail_success). Job ids stay
    caller-form (``_s``) so ``sites.get(str(row_id))`` always hits.
    """
    if value is None:
        return None
    try:
        from uuid import UUID  # noqa: PLC0415

        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return str(value)


def resolve_job_sites(
    db: Any,
    items: Iterable[tuple[Any, Any, Any]],
) -> dict[str, JobSite]:
    """Resolve effective sites for ``(job_id, location_id, customer_id)`` rows.

    Returns ``{str(job_id): JobSite}`` with an entry for every input row.
    IDs may be UUIDs or strings; ``customer_locations`` keys are String(36)
    while ``jobs.customer_id`` is a UUID column, so everything is compared
    as ``str`` (PG raises ``varchar = uuid`` otherwise).
    """
    triples = [(_s(j), _s(loc), _cs(cust)) for j, loc, cust in items]
    out: dict[str, JobSite] = {}
    if not triples:
        return out

    bound_ids = sorted({loc for _, loc, _ in triples if loc})
    bound_rows: dict[str, dict[str, Any]] = {}
    if bound_ids:
        stmt = _text(
            "SELECT id, label, address, access_notes, lat, lng "
            "FROM customer_locations "
            "WHERE id IN :ids AND deleted_at IS NULL"
        ).bindparams(bindparam("ids", expanding=True))
        for r in db.execute(stmt, {"ids": bound_ids}).mappings().all():
            bound_rows[str(r["id"])] = dict(r)

    # Customers that will need the fallback chain: unbound jobs, plus jobs
    # whose bound row vanished (soft-deleted) — desktop treats those as
    # unbound too (pickedLocation resolves to null and the chain continues).
    fallback_customers = sorted({
        cust
        for _, loc, cust in triples
        if cust and (not loc or loc not in bound_rows)
    })

    locs_by_customer: dict[str, list[dict[str, Any]]] = {}
    if fallback_customers:
        stmt = _text(
            "SELECT customer_id, label, address, access_notes, is_primary "
            "FROM customer_locations "
            "WHERE customer_id IN :cids AND deleted_at IS NULL "
            "ORDER BY created_at ASC"
        ).bindparams(bindparam("cids", expanding=True))
        for r in db.execute(stmt, {"cids": fallback_customers}).mappings().all():
            locs_by_customer.setdefault(str(r["customer_id"]), []).append(dict(r))

    # customer.address only for customers with ZERO location rows — via the
    # ORM so EncryptedString decrypts (raw SQL here would resurrect the
    # 2026-07-16 "gAAAA… where the address should be" bug class).
    # customer.address is needed for zero-location customers AND for
    # customers whose rows have no explicit primary (rule 2).
    bare_customers = [
        c for c in fallback_customers
        if not any(r.get("is_primary") for r in locs_by_customer.get(c, []))
    ]
    customer_addr: dict[str, str | None] = {}
    if bare_customers:
        from uuid import UUID  # noqa: PLC0415

        from gdx_dispatch.models.tenant_models import Customer  # noqa: PLC0415

        # Customer.id is a Uuid column — string keys don't bind. Anything
        # non-UUID (test fixtures, legacy rows) simply can't match and is
        # dropped rather than blowing up the whole page.
        keys = []
        for c in bare_customers:
            try:
                keys.append(UUID(c))
            except (ValueError, AttributeError, TypeError):
                continue
        if keys:
            for cid, addr in (
                db.query(Customer.id, Customer.address)
                .filter(Customer.id.in_(keys))
                .all()
            ):
                customer_addr[str(cid)] = addr

    for job_id, loc_id, cust_id in triples:
        if job_id is None:
            continue
        row = bound_rows.get(loc_id) if loc_id else None
        if row is not None:
            addr = (row.get("address") or "").strip() or None
            lat = lng = None
            if row.get("lat") is not None and row.get("lng") is not None:
                try:
                    lat, lng = float(row["lat"]), float(row["lng"])
                except (TypeError, ValueError):
                    lat = lng = None
            out[job_id] = JobSite(
                label=row.get("label"),
                address=addr,
                address_missing=addr is None,
                access_notes=row.get("access_notes"),
                source="location",
                lat=lat,
                lng=lng,
            )
            continue
        locs = locs_by_customer.get(cust_id or "", [])
        primary = next((r for r in locs if r.get("is_primary")), None)
        if primary is not None:
            addr = (primary.get("address") or "").strip() or None
            out[job_id] = JobSite(
                label=primary.get("label"),
                address=addr,
                address_missing=addr is None,
                access_notes=primary.get("access_notes") or None,
                source="customer_location",
            )
            continue
        if cust_id:
            addr = (customer_addr.get(cust_id) or "").strip() or None
            out[job_id] = JobSite(address=addr, source="customer")
            continue
        out[job_id] = _EMPTY
    return out


def resolve_job_site(db: Any, job_id: Any, location_id: Any, customer_id: Any) -> JobSite:
    """Single-job convenience for detail endpoints. Lists use the batch."""
    return resolve_job_sites(db, [(job_id, location_id, customer_id)]).get(
        _s(job_id) or "", _EMPTY
    )
