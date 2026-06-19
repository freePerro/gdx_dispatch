"""Regression brake — every ``payload.<attr>`` read inside ``create_job``
must be a declared field on the ``JobCreate`` schema.

2026-05-19 incident: commit 2e41cc45 (2026-05-13, "kill 'lead' job
stage") wired ``payload.holding_area_id`` into ``create_job`` and added
the field to ``JobUpdate`` — but **not** to ``JobCreate``. Pydantic v2
raises ``AttributeError`` on missing-attribute access, so *every*
``POST /api/jobs`` 500'd for ~6 days on prod GDX before anyone noticed
(editing an existing job uses ``JobUpdate``, which still worked).

The single-field assertion below pins the specific fix. The broader
``test_create_job_reads_only_declared_jobcreate_fields`` pins the whole
class of bug: if anyone again reads a payload attribute in ``create_job``
that the request schema doesn't declare, this fires at test time instead
of 500-ing in the field.

Static-source by design — same rationale as
``test_mobile_job_create_contract.py``: spinning up the full app + a
tenant DB for "does this attribute exist on a pydantic model" is
overkill, and a source scan gives the assertion we want reliably.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
JOBS_SRC = (REPO_ROOT / "gdx_dispatch/routers/jobs.py").read_text(encoding="utf-8")


def _class_field_names(source: str, class_name: str) -> set[str]:
    """Collect top-level annotated field names from a pydantic model
    class body (``name: type ...`` lines at the class indent level)."""
    lines = source.splitlines()
    start = next(
        i for i, ln in enumerate(lines)
        if ln.startswith(f"class {class_name}(")
    )
    fields: set[str] = set()
    for ln in lines[start + 1:]:
        if ln and not ln[0].isspace():  # dedented to module level -> class ended
            break
        if ln.startswith("class "):
            break
        m = re.match(r"\s{4}([a-zA-Z_]\w*)\s*:", ln)
        if m:
            fields.add(m.group(1))
    return fields


def _create_job_body(source: str) -> str:
    """Slice the ``create_job`` function source: from its ``def`` line to
    the next module-level ``def`` / ``async def`` / ``@router.`` line."""
    lines = source.splitlines()
    start = next(
        i for i, ln in enumerate(lines)
        if ln.startswith("def create_job(") or ln.startswith("async def create_job(")
    )
    end = next(
        (
            i for i, ln in enumerate(lines[start + 1:], start=start + 1)
            if ln.startswith(("def ", "async def ", "@router."))
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_jobcreate_declares_holding_area_id() -> None:
    """The specific fix for the 2026-05-19 incident."""
    fields = _class_field_names(JOBS_SRC, "JobCreate")
    assert "holding_area_id" in fields, (
        "JobCreate lost `holding_area_id`. create_job reads "
        "`payload.holding_area_id` (jobs.py ~662) — without the field, "
        "every POST /api/jobs raises AttributeError -> 500. Restore "
        "`holding_area_id: str | None = Field(default=None, max_length=36)` "
        "on JobCreate."
    )


def test_create_job_reads_only_declared_jobcreate_fields() -> None:
    """Every `payload.<attr>` in create_job must exist on JobCreate."""
    declared = _class_field_names(JOBS_SRC, "JobCreate")
    body = _create_job_body(JOBS_SRC)
    read_attrs = set(re.findall(r"\bpayload\.([a-zA-Z_]\w*)", body))
    undeclared = sorted(read_attrs - declared)
    assert not undeclared, (
        "create_job reads payload attributes not declared on JobCreate: "
        f"{undeclared}. Each will raise AttributeError -> 500 on every "
        "POST /api/jobs. Add the field(s) to the JobCreate schema "
        "(gdx_dispatch/routers/jobs.py) or stop reading them in create_job. "
        f"Declared fields: {sorted(declared)}"
    )


# --- Runtime assertions (not just source text) -------------------------
# The source scans above prove the field is *declared*. These prove the
# pydantic model and validation helper actually *behave* correctly at
# runtime — addressing the "a static test only proves a string exists"
# gap raised by /audit 2026-05-19.

def test_jobcreate_holding_area_id_roundtrips_at_runtime() -> None:
    from gdx_dispatch.routers.jobs import JobCreate

    assert JobCreate(title="x").holding_area_id is None, (
        "holding_area_id must default to None so callers that don't pass "
        "it don't get a spurious 400 / phantom lane."
    )
    jc = JobCreate(title="x", holding_area_id="ha-123")
    assert jc.holding_area_id == "ha-123", (
        "JobCreate must accept and preserve an explicit holding_area_id "
        "(this is what create_job reads at jobs.py ~672)."
    )


def test_holding_area_exists_filters_soft_deleted_and_unknown() -> None:
    """`_holding_area_exists` must (a) scope to non-deleted rows and
    (b) return False when no row resolves — otherwise a job routes into a
    retired/phantom lane and vanishes from dispatch."""
    from gdx_dispatch.routers import jobs as jobs_mod

    captured: dict[str, str] = {}

    class _Result:
        def __init__(self, row: object) -> None:
            self._row = row

        def first(self) -> object:
            return self._row

    class _FakeDB:
        def __init__(self, row: object) -> None:
            self._row = row

        def execute(self, stmt: object, params: dict | None = None):  # noqa: ANN001
            captured["sql"] = str(stmt)
            captured["params"] = params or {}
            return _Result(self._row)

    assert jobs_mod._holding_area_exists(_FakeDB((1,)), "ha-1") is True
    assert "deleted_at IS NULL" in captured["sql"], (
        "soft-delete guard removed — a job could be routed into a retired "
        f"lane. SQL was: {captured['sql']}"
    )
    assert jobs_mod._holding_area_exists(_FakeDB(None), "nope") is False, (
        "unknown holding_area_id must resolve False so create_job 400s "
        "instead of writing a dangling lane id."
    )


# --- Ordering / presence brakes (the round-2 /audit findings) ----------
# These are deliberately structural source assertions: the bugs they
# guard ARE ordering/presence bugs, which a source scan catches reliably
# (same rationale as test_mobile_job_create_contract.py).

def _func_body(source: str, name: str) -> str:
    lines = source.splitlines()
    start = next(
        i for i, ln in enumerate(lines)
        if ln.startswith((f"def {name}(", f"async def {name}("))
    )
    end = next(
        (
            i for i, ln in enumerate(lines[start + 1:], start=start + 1)
            if ln.startswith(("def ", "async def ", "@router."))
        ),
        len(lines),
    )
    return "\n".join(lines[start:end])


def test_create_job_validates_holding_area_before_job_number_alloc() -> None:
    """The holding_area_id 400 must fire BEFORE `next_job_number`, which
    commits an irreversible job number on the control plane. Rejecting
    after it burns a number and leaves a non-monotonic gap in the tenant's
    job/invoice sequence (QB reconciliation defect). /audit round-2."""
    body = _func_body(JOBS_SRC, "create_job")
    guard = body.find("_holding_area_exists(")
    alloc = body.find("next_job_number(")
    assert guard != -1, "holding_area_id guard missing from create_job"
    assert alloc != -1, "next_job_number call missing from create_job"
    assert guard < alloc, (
        "holding_area_id validation moved AFTER next_job_number — every "
        "rejected POST /api/jobs now burns a control-plane job number, "
        "creating non-monotonic gaps in the job/invoice sequence. Move "
        "the _holding_area_exists check above the next_job_number block."
    )


def test_update_job_also_validates_holding_area_id() -> None:
    """PATCH /api/jobs/{id} has the identical unvalidated-write gap as the
    create path and must guard it too — otherwise the create-path fix is a
    false sense of safety while the update path stays exploitable."""
    body = _func_body(JOBS_SRC, "update_job")
    assert 'if "holding_area_id" in data:' in body, (
        "update_job no longer handles holding_area_id — test is stale, "
        "re-point it."
    )
    assert "_holding_area_exists(" in body, (
        "update_job writes holding_area_id without calling "
        "_holding_area_exists — a PATCH can still route a job into a "
        "phantom/soft-deleted lane and vanish it from dispatch. Add the "
        "same guard as create_job."
    )
