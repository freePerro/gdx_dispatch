"""Phase 2 / C2 contract pins for POST /api/jobs/{id}/closeout.

Doug 2026-05-10: Phase 2 of the completion-gate fix. The closeout sheet is
the new completion path — replaces the bare `/complete` status flip with a
single transaction that captures parts + hours + signature + notes.

This file pins the **contract** (route shape, payload validation, surface
existence) without spinning up the full FastAPI app. Behavior verification
is done via browser walk on prod after C3 (mobile dialog) lands. The
contract-pin pattern follows test_mobile_job_create_contract.py.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from gdx_dispatch.routers.jobs import CloseoutPart, CloseoutPayload, router

REPO_ROOT = Path(__file__).resolve().parents[2]


def _route_for(path_suffix: str, method: str = "POST") -> APIRoute:
    """Find an APIRoute registered on the jobs router by path + method.
    The router carries its prefix on each route's `.path`, so we match
    against the full mounted path."""
    full = "/api/jobs" + path_suffix
    for r in router.routes:
        if not isinstance(r, APIRoute):
            continue
        if r.path == full and method in r.methods:
            return r
    raise AssertionError(f"route {method} {full} not registered on jobs router")


def test_closeout_route_registered() -> None:
    """POST /api/jobs/{job_id}/closeout exists on the jobs APIRouter."""
    route = _route_for("/{job_id}/closeout", "POST")
    assert route.endpoint.__name__ == "closeout_job"


def test_closeout_route_returns_201_status() -> None:
    """Per FastAPI convention the route was registered with status_code=201
    so the success path emits 201 (CREATE), matching POST /api/customers
    and POST /api/jobs/{id}/parts-needed."""
    route = _route_for("/{job_id}/closeout", "POST")
    assert route.status_code == 201, (
        f"expected 201, got {route.status_code}. The closeout creates a new "
        "JobCloseout row; 201 is the conventional success code."
    )


def test_closeout_payload_validates_minimum_shape() -> None:
    """An empty closeout (no parts, no hours, no signature, no notes) is
    valid at the schema level — the tenant gates decide whether to 422."""
    payload = CloseoutPayload(parts=[], hours=0)
    assert payload.parts == []
    assert payload.hours == 0


def test_closeout_payload_rejects_oversized_strings() -> None:
    """Pydantic max_length on signature_data / notes / signed_by fires
    cleanly. Pre-fix the body was unbounded; signature blobs can be ~30KB
    base64 PNGs but a 200KB cap leaves headroom + caps abuse."""
    with pytest.raises(ValidationError):
        CloseoutPayload(parts=[], hours=0, notes="x" * 4_001)
    with pytest.raises(ValidationError):
        CloseoutPayload(parts=[], hours=0, signature_data="d" * 200_001)


def test_closeout_part_validates_qty_range() -> None:
    """Per-part qty is bounded [1, 999] — tech can't accidentally log
    qty=0 (would deadlock into a perpetually-incomplete closeout) or
    qty=10000 (typo / fat-finger)."""
    with pytest.raises(ValidationError):
        CloseoutPart(name="X", qty=0)
    with pytest.raises(ValidationError):
        CloseoutPart(name="X", qty=1000)
    # Valid edges
    assert CloseoutPart(name="X", qty=1).qty == 1
    assert CloseoutPart(name="X", qty=999).qty == 999


def _closeout_function_body() -> str:
    """Return the source text of the `closeout_job` function up to the
    next top-level `def` / `@router.` line. Used by the static-source
    contract pins below."""
    src = (REPO_ROOT / "gdx_dispatch" / "routers" / "jobs.py").read_text(encoding="utf-8")
    start = src.find("def closeout_job(")
    assert start > 0, "closeout_job function not found in jobs.py"
    after = src[start:]
    # Walk to the next top-level definition / decorator.
    next_def = re.search(r"\n(?:def |@router\.)", after[1:])
    end = (next_def.start() + 1) if next_def else len(after)
    return after[:end]


def test_closeout_handler_uses_existing_workflow_flags() -> None:
    """The closeout handler must reuse `_load_workflow_flags` so the SAME
    three tenant-toggleable gates that apply to /complete (parts, hours,
    signature) apply to /closeout. If the closeout handler grew its own
    flag-loading logic, the gates could drift between paths."""
    span = _closeout_function_body()
    assert re.search(r"flags\s*=\s*_load_workflow_flags\(", span), (
        "closeout_job doesn't call _load_workflow_flags. The completion "
        "gates must stay shared between /complete and /closeout."
    )


def test_closeout_handler_validates_part_id_before_jobpart_insert() -> None:
    """job_parts has a NOT NULL FK to parts.id. Inserting a synthetic UUID
    triggers psycopg2 ForeignKeyViolation at commit. The closeout handler
    must verify Part exists before adding a JobPart row; free-text closeout
    lines (no real part_id) live ONLY in JobCloseout.parts_used JSONB.

    Smoke-tested against prod 2026-05-10: pre-fix the synthetic UUID
    triggered "Key (part_id)=... is not present in table parts" 500."""
    span = _closeout_function_body()
    # Must select the Part before inserting a JobPart. (PR4-billing-capture
    # widened select(Part.id) to select(Part) — the row is needed for the
    # sell price + stock decrement; the existence check is unchanged.)
    assert re.search(r"select\(\s*Part(\.id)?\s*\)", span), (
        "closeout_job doesn't verify Part.id before inserting a JobPart "
        "row. Free-text closeout lines will trigger a FK violation."
    )


def test_closeout_handler_writes_audit_row() -> None:
    """Every state-changing endpoint must write an audit row. /complete
    does (action=job_completed); /closeout must too (action=job_closeout)."""
    span = _closeout_function_body()
    assert "log_audit_event_sync" in span, (
        "closeout_job doesn't emit log_audit_event_sync — every state-"
        "changing endpoint must write an audit trail."
    )
    assert re.search(r'action\s*=\s*["\']job_closeout["\']', span), (
        "closeout audit action must be 'job_closeout' (not 'job_completed' "
        "or anything else) so reports can split closeout submissions from "
        "legacy /complete flips."
    )
