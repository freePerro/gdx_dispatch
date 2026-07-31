"""Planning rules for tools/qb_identity_repair.py (QB repair plan Phase 1).

The tool's DB/QB I/O is prod-only (PG catalogs + live QB reads, exercised by
its dry-run against prod before any apply). What must never regress silently
are the DECISION rules — when a relink/repoint is safe to propose vs. when it
demands a human --pick. build_plan is pure, so these run on plain dicts.

All fixture names/ids are fictional — never put real customer data in tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.qb_identity_repair import build_plan  # noqa: E402

ACME = {"id": "target-acme", "name": "Acme Door Co"}
NORTHSIDE = {"id": "target-northside", "name": "Northside Builders"}


def _unlinked(qb_id="101", invoice_id="inv-1", number="90000001"):
    return {"invoice_id": invoice_id, "invoice_number": number, "qb_id": qb_id}


def _husk(husk_id="husk-1", refs=None, qb_invoice_id="202"):
    return {
        "husk_id": husk_id,
        "ref_counts": {"invoices.customer_id": 5} if refs is None else refs,
        "qb_invoice_id": qb_invoice_id,
    }


def test_unlinked_invoice_with_unique_name_match_relinks():
    plan = build_plan(
        unlinked=[_unlinked()],
        husks=[],
        qb_names={"101": "Acme Door Co"},
        picks={},
        matches_by_name={"acme door co": [ACME]},
        live_customers={},
    )
    assert not plan.issues
    assert len(plan.relinks) == 1
    assert plan.relinks[0].target_customer_id == "target-acme"
    assert plan.relinks[0].invoice_number == "90000001"


def test_qb_name_matching_is_case_insensitive_like_the_lookup_key():
    # QB returns a lowercased name; GDX has it title-cased — same customer.
    plan = build_plan(
        unlinked=[_unlinked(qb_id="202")],
        husks=[],
        qb_names={"202": "northside builders"},
        picks={},
        matches_by_name={"northside builders": [NORTHSIDE]},
        live_customers={},
    )
    assert not plan.issues
    assert plan.relinks[0].target_customer_id == "target-northside"


def test_zero_or_multiple_name_matches_become_issues_not_writes():
    # The import minted duplicate customers with identical names — a name
    # matching several live customers must never be guessed at.
    plan = build_plan(
        unlinked=[
            _unlinked(qb_id="10", invoice_id="inv-a", number="A"),
            _unlinked(qb_id="11", invoice_id="inv-b", number="B"),
        ],
        husks=[],
        qb_names={"10": "Nobody Known", "11": "Northside Builders"},
        picks={},
        matches_by_name={
            "nobody known": [],
            "northside builders": [NORTHSIDE, {"id": "target-2", "name": "Northside Builders"}],
        },
        live_customers={},
    )
    assert not plan.relinks
    assert len(plan.issues) == 2
    assert "0 live customers" in plan.issues[0]
    assert "2 live customers" in plan.issues[1]


def test_missing_customer_ref_needs_pick_and_pick_wins():
    no_ref = build_plan(
        unlinked=[_unlinked(qb_id="99")],
        husks=[], qb_names={}, picks={},
        matches_by_name={}, live_customers={},
    )
    assert not no_ref.relinks
    assert "needs --pick" in no_ref.issues[0]

    picked = build_plan(
        unlinked=[_unlinked(qb_id="99")],
        husks=[], qb_names={},
        picks={"inv-1": "target-acme"},
        matches_by_name={},
        live_customers={"target-acme": ACME},
    )
    assert not picked.issues
    assert picked.relinks[0].target_customer_id == "target-acme"
    assert picked.relinks[0].qb_customer_name == "(picked)"


def test_pick_to_dead_or_unknown_target_is_refused():
    plan = build_plan(
        unlinked=[_unlinked()],
        husks=[],
        qb_names={"101": "Acme Door Co"},
        picks={"inv-1": "nonexistent-target"},
        matches_by_name={"acme door co": [ACME]},
        live_customers={},  # pick target not live → not in the lookup
    )
    assert not plan.relinks
    assert "not a live customer" in plan.issues[0]


def test_husk_with_recovered_name_repoints_all_refs():
    refs = {"invoices.customer_id": 5, "jobs.customer_id": 2}
    plan = build_plan(
        unlinked=[],
        husks=[_husk(refs=refs)],
        qb_names={"202": "Northside Builders"},
        picks={},
        matches_by_name={"northside builders": [NORTHSIDE]},
        live_customers={},
    )
    assert not plan.issues
    assert len(plan.repoints) == 1
    assert plan.repoints[0].ref_counts == refs
    assert plan.repoints[0].target_customer_id == "target-northside"


def test_husk_without_qb_handle_reports_refs_and_accepts_pick():
    unrecoverable = build_plan(
        unlinked=[],
        husks=[_husk(qb_invoice_id=None, refs={"estimates.customer_id": 1})],
        qb_names={}, picks={}, matches_by_name={}, live_customers={},
    )
    assert not unrecoverable.repoints
    assert "estimates.customer_id" in unrecoverable.issues[0]

    picked = build_plan(
        unlinked=[],
        husks=[_husk(qb_invoice_id=None)],
        qb_names={},
        picks={"husk-1": "target-northside"},
        matches_by_name={},
        live_customers={"target-northside": NORTHSIDE},
    )
    assert not picked.issues
    assert picked.repoints[0].target_customer_id == "target-northside"


def test_husk_pick_pointing_at_itself_is_refused():
    plan = build_plan(
        unlinked=[],
        husks=[_husk(husk_id="husk-1")],
        qb_names={},
        picks={"husk-1": "husk-1"},
        matches_by_name={},
        live_customers={"husk-1": {"id": "husk-1", "name": "Redacted"}},
    )
    assert not plan.repoints
    assert "husk itself" in plan.issues[0]


def test_empty_husk_is_left_alone():
    plan = build_plan(
        unlinked=[],
        husks=[_husk(refs={})],
        qb_names={"202": "Northside Builders"},
        picks={},
        matches_by_name={"northside builders": [NORTHSIDE]},
        live_customers={},
    )
    assert not plan.repoints
    assert not plan.issues
