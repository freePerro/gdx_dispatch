"""Human labels for audit rows — actors and subjects.

``audit_logs`` stores ``entity_type`` + ``entity_id`` and a ``user_id``, all
opaque. Nothing in the table says *which customer* was read or *which person*
read it, so the activity feed could only ever render "Data Accessed (customer)"
against a UUID. This module turns those ids into something a human can read.

Two deliberate design choices:

**Read-time, not write-time.** Labels are resolved when a feed page is served,
not stamped into ``details`` at write time. ``audit_logs`` is append-only and
trigger-enforced (see ``core.audit.ensure_audit_table``) — we could not backfill
``details`` even if we wanted to — and read-time resolution retro-labels the
entire existing history, including rows written before this module existed.

**Batched per type.** Both entry points take the whole page of rows and issue at
most one query per distinct entity type. Never one query per row.

Every lookup degrades to ``None`` rather than raising: a feed must not 500 over
a display field.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

# Actor classes. The feed renders these differently — a customer accepting an
# estimate and a dispatcher accepting one on their behalf are not the same
# event and must not look identical.
ACTOR_STAFF = "staff"
ACTOR_CUSTOMER = "customer"
ACTOR_SYSTEM = "system"
ACTOR_API_KEY = "api_key"
ACTOR_UNKNOWN = "unknown"

_MACHINE_ACTORS = {"system", "anonymous", "", None}

#: The actor id written for an anonymous customer opening a public document
#: link (core/customer_views.py). The token identifies a DOCUMENT, not a
#: person — several people at a company can share the same link — so there is
#: no CustomerUser row to resolve. It still has to classify as a customer, or
#: the UI badge that exists precisely to distinguish customer actions from
#: staff ones never fires.
PUBLIC_CUSTOMER_ACTOR = "customer"


def _is_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _uuid_keys(ids: set[str]) -> list[UUID]:
    """UUID objects for an IN clause against a ``Uuid(as_uuid=True)`` column.

    Two failure modes this guards, both real:
      * non-UUID ids ('system', 'job-1') reach these sets from legacy rows and
        from entity types that use slugs. Postgres refuses to cast them and
        takes the whole query — and therefore the whole feed — down.
      * SQLAlchemy's Uuid type binds ``value.hex``, so a str blows up with
        "'str' object has no attribute 'hex'" before it ever reaches the DB.
    """
    out: list[UUID] = []
    for i in ids:
        if not i:
            continue
        try:
            out.append(UUID(str(i)))
        except (ValueError, TypeError, AttributeError):
            continue
    return out


# ---------------------------------------------------------------------------
# Actors
# ---------------------------------------------------------------------------


def resolve_actors(db: Session, user_ids: set[str]) -> dict[str, dict[str, Any]]:
    """Map audit ``user_id`` -> ``{"name": str, "actor_type": str}``.

    Resolution order matters. A portal login writes ``user_id = customer_user.id``
    (see routers/portal.py), which is NOT a row in ``users`` — before this
    existed, the staff-only lookup missed and the feed rendered a customer's
    action as "Unknown user (a1b2c3d4)". Staff first, then customer users.
    """
    out: dict[str, dict[str, Any]] = {}
    if not user_ids:
        return out

    for raw in user_ids:
        if raw in _MACHINE_ACTORS:
            out[raw] = {"name": "System", "actor_type": ACTOR_SYSTEM}
        elif raw == PUBLIC_CUSTOMER_ACTOR:
            out[raw] = {"name": "Customer", "actor_type": ACTOR_CUSTOMER}

    unresolved = {u for u in user_ids if u and u not in out}

    # Resolution happens against the CANONICAL uuid string, but the caller
    # looks results up by the raw value it passed in. A row whose user_id is a
    # valid-but-non-canonical uuid (uppercase, braced, or unhyphenated — all
    # of which UUID() accepts) would otherwise resolve correctly under the
    # canonical key and then be read back under the raw key, missing, and
    # rendered "Unknown user". Map raw -> canonical and fan the answer back
    # out to every raw spelling at the end.
    raw_by_canonical: dict[str, list[str]] = {}
    for raw in unresolved:
        if _is_uuid(raw):
            raw_by_canonical.setdefault(str(UUID(str(raw))), []).append(raw)
        else:
            # Non-UUID, non-machine actors are API keys ("gdx_live_...") or
            # similar opaque principals — a real actor class, not an error.
            out[raw] = {"name": str(raw), "actor_type": ACTOR_API_KEY}

    candidates = list(raw_by_canonical)
    if candidates:
        resolved: dict[str, dict[str, Any]] = {}
        _resolve_staff(db, candidates, resolved)
        still_missing = [c for c in candidates if c not in resolved]
        if still_missing:
            _resolve_customer_users(db, still_missing, resolved)
        for canonical, raws in raw_by_canonical.items():
            entry = resolved.get(canonical) or {
                # A UUID that matches no user and no customer user: a deleted
                # principal. Say so — never render a bare 36-char UUID.
                "name": f"Unknown user ({canonical[:8]})",
                "actor_type": ACTOR_UNKNOWN,
            }
            for raw in raws:
                out[raw] = entry
    return out


def _resolve_staff(db: Session, ids: list[str], out: dict[str, dict[str, Any]]) -> None:
    try:
        from gdx_dispatch.models.tenant_models import User

        rows = db.execute(
            select(User.id, User.name, User.full_name, User.email).where(
                User.id.in_(_uuid_keys(set(ids)))
            )
        ).all()
        for row in rows:
            # Deliberately NOT falling back to str(id)[:8] like the old
            # audit.py resolver did — an 8-char hex string renders as if it
            # were a person's name. A nameless user is named as such.
            name = row.name or row.full_name or row.email or f"Unnamed user ({str(row.id)[:8]})"
            out[str(row.id)] = {"name": name, "actor_type": ACTOR_STAFF}
    except Exception:
        log.exception("audit_labels.resolve_staff_failed")


def _resolve_customer_users(db: Session, ids: list[str], out: dict[str, dict[str, Any]]) -> None:
    """Portal logins and portal actions are attributed to a CustomerUser."""
    try:
        from gdx_dispatch.models.tenant_models import Customer
        from gdx_dispatch.modules.customer_portal.models import CustomerUser

        rows = db.execute(
            select(CustomerUser.id, CustomerUser.email, Customer.name)
            .join(Customer, Customer.id == CustomerUser.customer_id, isouter=True)
            .where(CustomerUser.id.in_(_uuid_keys(set(ids))))
        ).all()
        for row in rows:
            # The EMAIL is the actor, not the customer name. Using the customer
            # name collapsed every contact at a company to a single actor,
            # which defeats the point of asking "who did this" — and the
            # company is already carried by the row's subject/entity_label, so
            # repeating it here just reads as a stutter.
            name = row.email or row.name or "Portal user"
            out[str(row.id)] = {"name": name, "actor_type": ACTOR_CUSTOMER}
    except Exception:
        log.exception("audit_labels.resolve_customer_users_failed")


# ---------------------------------------------------------------------------
# Subjects
# ---------------------------------------------------------------------------

# Deep-link prefixes for the SPA, in ONE place so a test can cross-check them
# against gdx_dispatch/frontend/src/router/index.js. This module hardcodes
# frontend routes; without the registry + its test, a route rename silently
# turns every activity row into a 404 and nothing in CI notices. That already
# happened once: invoices live at /billing/:id, and /invoices/:id — the
# obvious guess — is a redirect stub that falls through to the catch-all.
ENTITY_ROUTE_PREFIXES = {
    "customer": "/customers",
    "job": "/jobs",
    "estimate": "/estimates",
    "invoice": "/billing",
    "lead": "/leads",
    "landing_lead": "/leads",
}


# Line-item rows point at the line, not the document. The document is what a
# human recognises, so these hop to the parent.
_LINE_PARENTS = {
    "estimate_line": "estimate",
    "invoice_line": "invoice",
}


def resolve_entity_labels(
    db: Session, pairs: set[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    """Map ``(entity_type, entity_id)`` -> ``{"label": str, "url": str|None}``.

    Unknown entity types are simply absent from the result; the caller falls
    back to whatever it rendered before.
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    if not pairs:
        return out

    by_type: dict[str, set[str]] = {}
    for etype, eid in pairs:
        if etype and eid:
            by_type.setdefault(etype, set()).add(str(eid))

    for etype, ids in by_type.items():
        handler = _RESOLVERS.get(etype)
        if handler is None and etype in _LINE_PARENTS:
            handler = _resolve_line
        if handler is None:
            continue
        try:
            handler(db, etype, ids, out)
        except Exception:
            # One bad entity type must not blank the whole feed.
            log.exception("audit_labels.resolve_failed type=%s", etype)
    return out


def _resolve_customer(db: Session, etype: str, ids: set[str], out: dict) -> None:
    from gdx_dispatch.models.tenant_models import Customer

    keys = _uuid_keys(ids)
    if not keys:
        return
    for row in db.execute(select(Customer.id, Customer.name).where(Customer.id.in_(keys))).all():
        out[(etype, str(row.id))] = {
            "label": row.name or "Unnamed customer",
            "url": f"{ENTITY_ROUTE_PREFIXES['customer']}/{row.id}",
        }


def _resolve_job(db: Session, etype: str, ids: set[str], out: dict) -> None:
    from gdx_dispatch.models.tenant_models import Job

    keys = _uuid_keys(ids)
    if not keys:
        return
    for row in db.execute(
        select(Job.id, Job.job_number, Job.title).where(Job.id.in_(keys))
    ).all():
        label = f"{row.job_number} — {row.title}" if row.job_number else (row.title or "Job")
        out[(etype, str(row.id))] = {"label": label, "url": f"{ENTITY_ROUTE_PREFIXES['job']}/{row.id}"}


def _resolve_invoice(db: Session, etype: str, ids: set[str], out: dict) -> None:
    from gdx_dispatch.models.tenant_models import Invoice

    keys = _uuid_keys(ids)
    if not keys:
        return
    for row in db.execute(
        select(Invoice.id, Invoice.invoice_number).where(Invoice.id.in_(keys))
    ).all():
        out[(etype, str(row.id))] = {
            "label": row.invoice_number or "Invoice",
            # /billing/:id, NOT /invoices/:id — the latter is a redirect stub
            # in the Vue router and an id under it lands on the 404 catch-all.
            # ENTITY_ROUTE_PREFIXES below is cross-checked against the router.
            "url": f"{ENTITY_ROUTE_PREFIXES['invoice']}/{row.id}",
        }


def _resolve_estimate(db: Session, etype: str, ids: set[str], out: dict) -> None:
    from gdx_dispatch.modules.proposals.models import Estimate

    keys = _uuid_keys(ids)
    if not keys:
        return
    for row in db.execute(
        select(Estimate.id, Estimate.estimate_number, Estimate.label).where(Estimate.id.in_(keys))
    ).all():
        label = row.estimate_number or "Estimate"
        if row.label:
            label = f"{label} — {row.label}"
        out[(etype, str(row.id))] = {"label": label, "url": f"{ENTITY_ROUTE_PREFIXES['estimate']}/{row.id}"}


def _resolve_line(db: Session, etype: str, ids: set[str], out: dict) -> None:
    """A line edit is only meaningful as "a line on <document>"."""
    parent_type = _LINE_PARENTS[etype]
    keys = _uuid_keys(ids)
    if not keys:
        return

    if etype == "estimate_line":
        from gdx_dispatch.modules.proposals.models import EstimateLine

        rows = db.execute(
            select(EstimateLine.id, EstimateLine.estimate_id).where(EstimateLine.id.in_(keys))
        ).all()
    else:
        from gdx_dispatch.models.tenant_models import InvoiceLine

        rows = db.execute(
            select(InvoiceLine.id, InvoiceLine.invoice_id).where(InvoiceLine.id.in_(keys))
        ).all()

    parent_ids = {str(r[1]) for r in rows if r[1]}
    if not parent_ids:
        return
    parents: dict[tuple[str, str], dict[str, Any]] = {}
    _RESOLVERS[parent_type](db, parent_type, parent_ids, parents)
    for line_id, parent_id in rows:
        parent = parents.get((parent_type, str(parent_id)))
        if parent:
            out[(etype, str(line_id))] = dict(parent)


def _resolve_user_entity(db: Session, etype: str, ids: set[str], out: dict) -> None:
    from gdx_dispatch.models.tenant_models import User

    keys = _uuid_keys(ids)
    if not keys:
        return
    for row in db.execute(
        select(User.id, User.name, User.full_name, User.email).where(User.id.in_(keys))
    ).all():
        out[(etype, str(row.id))] = {
            "label": row.name or row.full_name or row.email or "User",
            "url": None,
        }


def _resolve_lead(db: Session, etype: str, ids: set[str], out: dict) -> None:
    from gdx_dispatch.models.tenant_models import LandingLead, Lead

    model = Lead if etype == "lead" else LandingLead
    keys = _uuid_keys(ids)
    if not keys:
        return
    for row in db.execute(select(model.id, model.name, model.email).where(model.id.in_(keys))).all():
        out[(etype, str(row.id))] = {
            "label": row.name or row.email or "Lead",
            "url": ENTITY_ROUTE_PREFIXES[etype],
        }


def _resolve_vendor(db: Session, etype: str, ids: set[str], out: dict) -> None:
    from gdx_dispatch.models.tenant_models import Vendor

    keys = _uuid_keys(ids)
    if not keys:
        return
    for row in db.execute(select(Vendor.id, Vendor.name).where(Vendor.id.in_(keys))).all():
        out[(etype, str(row.id))] = {"label": row.name or "Vendor", "url": None}


def _resolve_customer_user_entity(db: Session, etype: str, ids: set[str], out: dict) -> None:
    """`customer_user` rows are portal logins — the subject is the customer."""
    from gdx_dispatch.models.tenant_models import Customer
    from gdx_dispatch.modules.customer_portal.models import CustomerUser

    keys = _uuid_keys(ids)
    if not keys:
        return
    for row in db.execute(
        select(CustomerUser.id, CustomerUser.email, CustomerUser.customer_id, Customer.name)
        .join(Customer, Customer.id == CustomerUser.customer_id, isouter=True)
        .where(CustomerUser.id.in_(keys))
    ).all():
        out[(etype, str(row.id))] = {
            "label": row.name or row.email or "Portal user",
            "url": f"{ENTITY_ROUTE_PREFIXES['customer']}/{row.customer_id}" if row.customer_id else None,
        }


_RESOLVERS = {
    "customer": _resolve_customer,
    "job": _resolve_job,
    "invoice": _resolve_invoice,
    "estimate": _resolve_estimate,
    "user": _resolve_user_entity,
    "lead": _resolve_lead,
    "landing_lead": _resolve_lead,
    "vendor": _resolve_vendor,
    "customer_user": _resolve_customer_user_entity,
}


def decorate_rows(db: Session, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``user_name`` / ``actor_type`` / ``entity_label`` / ``entity_url``
    to a page of serialized audit rows, in place. Batched; safe on failure."""
    if not rows:
        return rows
    try:
        actors = resolve_actors(db, {str(r.get("user_id") or "") for r in rows})
    except Exception:
        log.exception("audit_labels.decorate_actors_failed")
        actors = {}
    try:
        labels = resolve_entity_labels(
            db,
            {
                (str(r.get("entity_type") or ""), str(r.get("entity_id") or ""))
                for r in rows
                if r.get("entity_type") and r.get("entity_id")
            },
        )
    except Exception:
        log.exception("audit_labels.decorate_labels_failed")
        labels = {}

    for r in rows:
        uid = str(r.get("user_id") or "")
        actor = actors.get(uid) or {}
        r["user_name"] = actor.get("name") or uid or "System"
        r["actor_type"] = actor.get("actor_type") or ACTOR_UNKNOWN
        ent = labels.get((str(r.get("entity_type") or ""), str(r.get("entity_id") or "")))
        r["entity_label"] = ent.get("label") if ent else None
        r["entity_url"] = ent.get("url") if ent else None
    return rows
