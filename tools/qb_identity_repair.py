#!/usr/bin/env python3
"""Identity repair for QB-import damage — Phase 1 of the QB paid-status
repair plan (docs/design/qb-import-paid-status-repair-plan.md).

Fixes two kinds of identity loss, both rendered as "Unknown" in the UI:

1. ``unlinked`` — invoices the QB importer created with ``customer_id NULL``.
   The QB invoice still knows its customer (CustomerRef); we recover the
   name live from QB and relink to the matching GDX customer.
2. ``redacted`` — customers renamed "Redacted" + soft-deleted by the GDPR
   endpoint (the 2026-04-08 incident). Their invoices' QB maps still reach
   the real CustomerRef name; every FK row on the husk is repointed to the
   live customer of that name, leaving the husk empty and inert.

Usage (inside the app container — needs DB + QB env)::

    python tools/qb_identity_repair.py                       # dry-run report
    python tools/qb_identity_repair.py --apply --operator doug
    # manual override where QB can't answer or the name is ambiguous:
    python tools/qb_identity_repair.py --apply --operator doug \
        --pick <husk-or-invoice-uuid>=<target-customer-uuid>

Dry-run (the default) performs only reads — DB SELECTs and QB query GETs.
``--apply`` wraps every write in ONE transaction (all-or-nothing) and writes
a hash-chained audit row per action, attributed to ``cli:<operator>``.

QB reads are metered but this is a handful of ``Invoice`` queries, total.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from gdx_dispatch.core.audit import log_audit_event_sync  # noqa: E402
from gdx_dispatch.core.database import SessionLocal  # noqa: E402
from gdx_dispatch.core.tenant import company_id  # noqa: E402

REDACTED_NAME = "Redacted"


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------

@dataclass
class RelinkInvoice:
    invoice_id: str
    invoice_number: str
    qb_customer_name: str
    target_customer_id: str
    target_customer_name: str


@dataclass
class RepointHusk:
    husk_id: str
    qb_customer_name: str
    target_customer_id: str
    target_customer_name: str
    ref_counts: dict[str, int]  # "table.column" -> rows to repoint


@dataclass
class Plan:
    relinks: list[RelinkInvoice] = field(default_factory=list)
    repoints: list[RepointHusk] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Discovery (reads only)
# ---------------------------------------------------------------------------

def customer_fk_columns(db) -> list[tuple[str, str]]:
    """Every (table, column) with a FOREIGN KEY into customers(id), from the
    live catalog — a hardcoded list would silently miss tables added later.
    PostgreSQL only; tests inject their own list."""
    rows = db.execute(text(
        "SELECT tc.table_name, kcu.column_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON tc.constraint_name = kcu.constraint_name "
        "JOIN information_schema.constraint_column_usage ccu "
        "  ON tc.constraint_name = ccu.constraint_name "
        "WHERE tc.constraint_type = 'FOREIGN KEY' "
        "  AND ccu.table_name = 'customers' "
        "ORDER BY 1, 2"
    )).all()
    return [(r[0], r[1]) for r in rows]


def fetch_unlinked_invoices(db) -> list[dict]:
    """QB-mapped invoices with no customer link."""
    rows = db.execute(text(
        "SELECT i.id::text AS invoice_id, i.invoice_number, m.qb_id "
        "FROM invoices i "
        "JOIN qb_entity_maps m ON m.entity_type = 'invoice' "
        "  AND m.local_id = i.id::text "
        "WHERE i.customer_id IS NULL AND i.deleted_at IS NULL "
        "ORDER BY i.invoice_number"
    )).mappings().all()
    return [dict(r) for r in rows]


def fetch_redacted_husks(db, fk_columns: list[tuple[str, str]]) -> list[dict]:
    """Redacted customers with any surviving FK references, plus one
    QB-mapped invoice qb_id per husk (the name-recovery handle)."""
    husks = db.execute(text(
        "SELECT id::text AS husk_id FROM customers WHERE name = :redacted"
    ), {"redacted": REDACTED_NAME}).mappings().all()

    out = []
    for h in husks:
        husk_id = h["husk_id"]
        ref_counts: dict[str, int] = {}
        for table, col in fk_columns:
            n = db.execute(
                text(f"SELECT count(*) FROM {table} WHERE {col} = :hid"),  # noqa: S608 — table/col from the FK catalog, not user input
                {"hid": husk_id},
            ).scalar()
            if n:
                ref_counts[f"{table}.{col}"] = int(n)
        qb_handle = db.execute(text(
            "SELECT m.qb_id FROM invoices i "
            "JOIN qb_entity_maps m ON m.entity_type = 'invoice' "
            "  AND m.local_id = i.id::text "
            "WHERE i.customer_id = :hid LIMIT 1"
        ), {"hid": husk_id}).scalar()
        out.append({"husk_id": husk_id, "ref_counts": ref_counts, "qb_invoice_id": qb_handle})
    return out


def match_live_customers(db, name: str) -> list[dict]:
    """Live (non-deleted, non-redacted) customers whose trimmed name matches
    case-insensitively."""
    rows = db.execute(text(
        "SELECT id::text AS id, name FROM customers "
        "WHERE deleted_at IS NULL AND name <> :redacted "
        "  AND lower(trim(name)) = lower(trim(:name))"
    ), {"redacted": REDACTED_NAME, "name": name}).mappings().all()
    return [dict(r) for r in rows]


async def recover_qb_customer_names(db, qb_invoice_ids: list[str]) -> dict[str, str]:
    """QB invoice id -> CustomerRef.name, via one read-only QB query."""
    if not qb_invoice_ids:
        return {}
    from gdx_dispatch.modules.quickbooks.oauth import get_qb_client  # noqa: PLC0415

    qb = await get_qb_client(company_id(), db)
    async with qb:
        ids = ",".join(f"'{i}'" for i in qb_invoice_ids)
        rows = await qb.query("Invoice", where=f"Id IN ({ids})")
    return {
        str(r["Id"]): (r.get("CustomerRef") or {}).get("name", "").strip()
        for r in rows
        if (r.get("CustomerRef") or {}).get("name")
    }


# ---------------------------------------------------------------------------
# Planning — PURE: every lookup arrives as data, so the decision rules are
# testable without a DB or QB. The CLI precomputes the two lookup dicts.
# ---------------------------------------------------------------------------

def _resolve_target(
    entity: str,
    label: str,
    pick_target: str | None,
    qb_name: str,
    matches_by_name: dict[str, list[dict]],
    live_customers: dict[str, dict],
    issues: list[str],
) -> dict | None:
    """One target-resolution rule for both phases: an explicit --pick wins
    (must be a live customer); otherwise the QB-recovered name must match
    exactly one live customer. Anything else is a human's call."""
    if pick_target:
        target = live_customers.get(pick_target)
        if not target:
            issues.append(f"{entity} {label}: --pick target {pick_target} not a live customer")
            return None
        return target
    if not qb_name:
        issues.append(f"{entity} {label}: QB has no CustomerRef — needs --pick")
        return None
    matches = matches_by_name.get(qb_name.strip().lower(), [])
    if len(matches) != 1:
        issues.append(
            f"{entity} {label}: QB says '{qb_name}' but {len(matches)} "
            f"live customers match — needs --pick")
        return None
    return matches[0]


def build_plan(
    unlinked: list[dict],
    husks: list[dict],
    qb_names: dict[str, str],
    picks: dict[str, str],
    matches_by_name: dict[str, list[dict]],
    live_customers: dict[str, dict],
) -> Plan:
    plan = Plan()

    for inv in unlinked:
        qb_name = qb_names.get(str(inv["qb_id"]), "")
        target = _resolve_target(
            "invoice", inv["invoice_number"], picks.get(inv["invoice_id"]),
            qb_name, matches_by_name, live_customers, plan.issues)
        if not target:
            continue
        plan.relinks.append(RelinkInvoice(
            invoice_id=inv["invoice_id"],
            invoice_number=inv["invoice_number"],
            qb_customer_name=qb_name or "(picked)",
            target_customer_id=target["id"],
            target_customer_name=target["name"],
        ))

    for husk in husks:
        if not husk["ref_counts"]:
            continue  # empty husk — nothing to repoint, leave it soft-deleted
        qb_name = qb_names.get(str(husk["qb_invoice_id"] or ""), "")
        target = _resolve_target(
            "husk", husk["husk_id"], picks.get(husk["husk_id"]),
            qb_name, matches_by_name, live_customers, plan.issues)
        if not target:
            if not qb_name and not picks.get(husk["husk_id"]):
                plan.issues[-1] += f" (refs: {husk['ref_counts']})"
            continue
        if target["id"] == husk["husk_id"]:
            plan.issues.append(f"husk {husk['husk_id']}: pick points at the husk itself")
            continue
        plan.repoints.append(RepointHusk(
            husk_id=husk["husk_id"],
            qb_customer_name=qb_name or "(picked)",
            target_customer_id=target["id"],
            target_customer_name=target["name"],
            ref_counts=husk["ref_counts"],
        ))

    return plan


# ---------------------------------------------------------------------------
# Apply (one transaction, audit row per action)
# ---------------------------------------------------------------------------

def apply_plan(db, plan: Plan, fk_columns: list[tuple[str, str]], operator: str) -> None:
    tenant = company_id()
    actor = f"cli:{operator}"

    for r in plan.relinks:
        db.execute(text(
            "UPDATE invoices SET customer_id = CAST(:tid AS uuid) "
            "WHERE id = CAST(:iid AS uuid) AND customer_id IS NULL"
        ), {"tid": r.target_customer_id, "iid": r.invoice_id})
        log_audit_event_sync(
            db, tenant_id=tenant, user_id=actor,
            action="qb_identity_repair_relink_invoice",
            entity_type="invoice", entity_id=r.invoice_id,
            details={
                "invoice_number": r.invoice_number,
                "qb_customer_name": r.qb_customer_name,
                "target_customer_id": r.target_customer_id,
            },
        )

    for h in plan.repoints:
        moved: dict[str, int] = {}
        for table, col in fk_columns:
            key = f"{table}.{col}"
            if key not in h.ref_counts:
                continue
            res = db.execute(
                text(f"UPDATE {table} SET {col} = CAST(:tid AS uuid) WHERE {col} = CAST(:hid AS uuid)"),  # noqa: S608 — identifiers from the FK catalog
                {"tid": h.target_customer_id, "hid": h.husk_id},
            )
            moved[key] = res.rowcount
        log_audit_event_sync(
            db, tenant_id=tenant, user_id=actor,
            action="qb_identity_repair_repoint_husk",
            entity_type="customer", entity_id=h.husk_id,
            details={
                "qb_customer_name": h.qb_customer_name,
                "target_customer_id": h.target_customer_id,
                "rows_moved": moved,
            },
        )

    db.commit()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_plan(plan: Plan) -> None:
    print(f"\n== relink: {len(plan.relinks)} unlinked invoice(s) ==")
    for r in plan.relinks:
        print(f"  {r.invoice_number:<16} → {r.target_customer_name}  "
              f"({r.target_customer_id})  [QB: {r.qb_customer_name}]")
    print(f"\n== repoint: {len(plan.repoints)} redacted husk(s) ==")
    for h in plan.repoints:
        total = sum(h.ref_counts.values())
        print(f"  husk {h.husk_id} → {h.target_customer_name} ({h.target_customer_id})  "
              f"[QB: {h.qb_customer_name}] {total} row(s): {h.ref_counts}")
    if plan.issues:
        print(f"\n  ⚠ {len(plan.issues)} item(s) need a human (--pick or investigation):")
        for issue in plan.issues:
            print(f"    - {issue}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the plan (default: dry-run report)")
    ap.add_argument("--operator", default="",
                    help="who is running this (required with --apply; audited)")
    ap.add_argument("--pick", action="append", default=[], metavar="ID=TARGET",
                    help="manual husk/invoice → live-customer override, repeatable")
    args = ap.parse_args()

    if args.apply and not args.operator.strip():
        ap.error("--apply requires --operator")

    picks: dict[str, str] = {}
    for p in args.pick:
        if "=" not in p:
            ap.error(f"--pick needs ID=TARGET, got {p!r}")
        k, v = p.split("=", 1)
        picks[k.strip()] = v.strip()

    db = SessionLocal()
    try:
        fk_columns = customer_fk_columns(db)
        unlinked = fetch_unlinked_invoices(db)
        husks = fetch_redacted_husks(db, fk_columns)

        qb_handles = [str(i["qb_id"]) for i in unlinked]
        qb_handles += [str(h["qb_invoice_id"]) for h in husks if h["qb_invoice_id"]]
        qb_names = asyncio.run(recover_qb_customer_names(db, sorted(set(qb_handles))))

        matches_by_name = {
            name.strip().lower(): match_live_customers(db, name)
            for name in set(qb_names.values())
        }
        live_customers = {}
        for target_id in set(picks.values()):
            row = db.execute(text(
                "SELECT id::text AS id, name FROM customers "
                "WHERE id = CAST(:tid AS uuid) AND deleted_at IS NULL"
            ), {"tid": target_id}).mappings().first()
            if row:
                live_customers[target_id] = dict(row)

        plan = build_plan(unlinked, husks, qb_names, picks, matches_by_name, live_customers)
        _print_plan(plan)

        if not args.apply:
            print("\nDry run — nothing written. Re-run with --apply --operator <you>.")
            return 0
        if not plan.relinks and not plan.repoints:
            print("\nNothing to apply.")
            return 0

        apply_plan(db, plan, fk_columns, operator=args.operator.strip())
        print(f"\nApplied: {len(plan.relinks)} relink(s), {len(plan.repoints)} repoint(s). "
              "Audit rows written (qb_identity_repair_*).")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
