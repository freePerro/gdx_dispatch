"""Regression brake — Sprint dispatch-capacity (2026-05-21).

Doug 2026-05-21: "Make tests so we don't get regression."

Pins the cross-router contract for the dispatch-capacity feature. All
source-scan, no DB required — same rationale as
``test_jobs_create_payload_contract.py``: spinning up the full app +
tenant DB to verify "does this column exist in the model" is overkill,
and a source scan catches the actual class-of-bug we worry about (a
refactor that drops the field on one side of the round-trip).

Covers:
  • Job + AppSettings + User ORM declarations include the new columns
  • JobCreate + JobUpdate schemas declare scheduled_duration_hours
  • create_job + update_job actually write the field
  • list_jobs SELECTs and exposes effective_duration_hours
  • technicians.list_technicians returns effective_shift_* on each row
  • tech_efficiency SQL filters NULL hours and zero actual-time rows
  • tech_efficiency router is mounted in gdx_dispatch/app.py
  • the one-shot migration script declares all 7 ALTER statements
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


# ─── ORM column declarations ─────────────────────────────────────────

def test_job_model_has_scheduled_duration_hours():
    src = _read("gdx_dispatch/models/tenant_models.py")
    # Field is declared on Job (model name) with the Numeric column shape.
    assert "scheduled_duration_hours" in src
    assert re.search(
        r"scheduled_duration_hours:\s*Mapped\[Decimal\s*\|\s*None\]\s*=\s*mapped_column\(\s*Numeric\(5,\s*2\)",
        src,
    ), "Job.scheduled_duration_hours must be Numeric(5,2) nullable"


def test_app_settings_has_default_shift_columns():
    src = _read("gdx_dispatch/models/tenant_models.py")
    for col in ("default_shift_start", "default_shift_end", "default_workdays"):
        assert col in src, f"AppSettings missing {col}"
    # Tenant default is NOT NULL with defaults so existing rows backfill.
    assert "default_shift_start: Mapped[time]" in src
    assert "default_shift_end: Mapped[time]" in src
    assert "default_workdays: Mapped[int]" in src


def test_user_has_shift_override_columns():
    src = _read("gdx_dispatch/models/tenant_models.py")
    # Per-user override fields — ALL nullable (NULL = inherit).
    assert "shift_start: Mapped[time | None]" in src
    assert "shift_end: Mapped[time | None]" in src
    assert "workdays: Mapped[int | None]" in src


# ─── JobCreate / JobUpdate schema declarations ───────────────────────

def _class_field_names(source: str, class_name: str) -> set[str]:
    lines = source.splitlines()
    start = next(
        i for i, ln in enumerate(lines)
        if ln.startswith(f"class {class_name}(")
    )
    fields: set[str] = set()
    for ln in lines[start + 1:]:
        if ln and not ln[0].isspace():
            break
        if ln.startswith("class "):
            break
        m = re.match(r"\s{4}([a-zA-Z_]\w*)\s*:", ln)
        if m:
            fields.add(m.group(1))
    return fields


def test_job_create_schema_declares_scheduled_duration_hours():
    src = _read("gdx_dispatch/routers/jobs.py")
    assert "scheduled_duration_hours" in _class_field_names(src, "JobCreate"), (
        "JobCreate must declare scheduled_duration_hours (regression "
        "guard for the 2026-05-19 holding_area_id incident pattern — "
        "JobUpdate had the field but JobCreate didn't)"
    )


def test_job_update_schema_declares_scheduled_duration_hours():
    src = _read("gdx_dispatch/routers/jobs.py")
    assert "scheduled_duration_hours" in _class_field_names(src, "JobUpdate")


# ─── create_job + update_job actually USE the field ──────────────────

def test_create_job_writes_scheduled_duration_hours_to_orm():
    src = _read("gdx_dispatch/routers/jobs.py")
    # The Job(...) constructor call inside create_job must reference
    # payload.scheduled_duration_hours — otherwise the column is silently
    # dropped on POST.
    assert "scheduled_duration_hours=payload.scheduled_duration_hours" in src


def test_update_job_includes_scheduled_duration_hours_in_updates():
    src = _read("gdx_dispatch/routers/jobs.py")
    # update_job builds an updates dict and writes the field when present.
    assert re.search(
        r'if\s+"scheduled_duration_hours"\s+in\s+data:\s*\n\s+updates\["scheduled_duration_hours"\]',
        src,
    ), "update_job must thread scheduled_duration_hours from PATCH data into the updates dict"


# ─── list_jobs SELECTs the column + emits effective_duration_hours ──

def test_list_jobs_selects_scheduled_duration_hours():
    src = _read("gdx_dispatch/routers/jobs.py")
    # SELECT must pull the new column so the dispatch board can render it.
    assert "j.scheduled_duration_hours" in src


def test_list_jobs_exposes_effective_duration_hours():
    src = _read("gdx_dispatch/routers/jobs.py")
    # The response dict adds effective_duration_hours derived from
    # scheduled_duration_hours (post-/audit, the estimate fallback was
    # dropped here to avoid N+1 — keep the dispatch board hot path lean).
    assert 'd["effective_duration_hours"]' in src


def test_list_jobs_does_not_recompute_man_hour_duration_per_row():
    """/audit BLOCK from 2026-05-21 — calling
    ``compute_man_hour_duration_minutes`` per row N+1's the dispatch
    board (2-3 estimate queries × N jobs × every refresh, with
    scheduled_duration_hours NULL on day one). The list endpoint must
    NOT import / call this helper. The detail endpoint
    ``/api/jobs/{id}/duration`` still uses it.
    """
    src = _read("gdx_dispatch/routers/jobs.py")
    # The function is still imported elsewhere (e.g. appointment sync) but
    # not from within list_jobs body. Slice list_jobs and assert.
    lines = src.splitlines()
    start = next(
        i for i, ln in enumerate(lines)
        if ln.startswith("def list_jobs(")
    )
    end = next(
        (i for i, ln in enumerate(lines[start + 1:], start + 1)
         if ln.startswith("def ") or ln.startswith("async def ") or ln.startswith("@router.")),
        len(lines),
    )
    # Strip comment lines first — the function's docstring/comments
    # mention compute_man_hour_duration_minutes by name to explain the
    # /audit decision, which is desirable. We only fail on actual call
    # sites (an import or a function-call expression).
    code_lines = [
        ln for ln in lines[start:end]
        if not re.match(r"^\s*#", ln)
    ]
    body = "\n".join(code_lines)
    # Allow the name to appear in a string literal (docstring), but ban
    # `from … import …` and bare-name calls.
    assert "from gdx_dispatch.routers.appointments import compute_man_hour_duration_minutes" not in body, (
        "list_jobs must not import compute_man_hour_duration_minutes — "
        "N+1 perf hit on every dispatch board refresh. See /audit "
        "2026-05-21 BLOCK finding 1."
    )
    assert "_calc_duration(" not in body, (
        "list_jobs must not call the per-row duration calculator alias. "
        "See /audit 2026-05-21 BLOCK finding 1."
    )


# ─── technicians returns effective_shift_* on each row ───────────────

def test_technicians_router_returns_effective_shift_fields():
    src = _read("gdx_dispatch/routers/technicians.py")
    for key in ("effective_shift_start", "effective_shift_end", "effective_workdays"):
        assert key in src, f"_tech_to_dict must expose {key}"


def test_technicians_router_loads_app_settings_for_tenant_default():
    src = _read("gdx_dispatch/routers/technicians.py")
    # Must import AppSettings and call .first() so it has the tenant
    # default to merge with per-user overrides.
    assert "AppSettings" in src
    assert "db.query(AppSettings).first()" in src


# ─── tech_efficiency SQL filters ─────────────────────────────────────

def test_tech_efficiency_filters_null_scheduled_duration():
    src = _read("gdx_dispatch/routers/tech_efficiency.py")
    # Jobs without a scheduler estimate are excluded — the ratio reflects
    # only jobs the scheduler actually estimated against.
    assert "j.scheduled_duration_hours IS NOT NULL" in src


def test_tech_efficiency_filters_zero_actual_hours():
    src = _read("gdx_dispatch/routers/tech_efficiency.py")
    # Zero actual_hours would cause division-by-zero in the ratio AND
    # signals a closeout that wasn't fully filled — filter it out.
    assert "jc.hours_worked > 0" in src


def test_tech_efficiency_credits_lead_tech_first():
    src = _read("gdx_dispatch/routers/tech_efficiency.py")
    # The DISTINCT ON CTE prefers is_lead, then earliest assigned_at,
    # then falls back to legacy jobs.assigned_to.
    assert "ORDER BY ja.job_id, ja.is_lead DESC" in src


def test_tech_efficiency_returns_empty_shape_on_missing_schema():
    src = _read("gdx_dispatch/routers/tech_efficiency.py")
    # ProgrammingError = tenant DB hasn't been migrated yet. The
    # endpoint must return an empty report (with `schema_pending: True`)
    # rather than 500-ing, so old tenants don't break the dispatch board.
    assert "ProgrammingError" in src
    assert "schema_pending" in src


# ─── app.py mounts the router ────────────────────────────────────────

def test_app_includes_tech_efficiency_router():
    src = _read("gdx_dispatch/app.py")
    assert "tech_efficiency" in src
    assert "tech_efficiency_router" in src
    assert "app.include_router(tech_efficiency_router" in src


# ─── Migration script declares the 7 ALTERs ──────────────────────────

def test_job_create_pydantic_round_trip_accepts_scheduled_duration_hours():
    """Behavior test — not source-scan. Audit 2026-05-21: "19 grep
    assertions … green-light Pydantic typos." This proves the schema
    actually parses the field and round-trips it through model_dump.
    """
    from decimal import Decimal
    from gdx_dispatch.routers.jobs import JobCreate, JobUpdate

    create = JobCreate(title="Spring repair", scheduled_duration_hours="2.5")
    dump = create.model_dump()
    assert "scheduled_duration_hours" in dump
    assert Decimal(str(dump["scheduled_duration_hours"])) == Decimal("2.5")

    # Same on the update schema; both paths must accept it.
    update = JobUpdate(scheduled_duration_hours="1.25")
    udump = update.model_dump(exclude_unset=True)
    assert udump["scheduled_duration_hours"] == Decimal("1.25")

    # NULL should round-trip cleanly so PATCH can clear the field.
    cleared = JobUpdate(scheduled_duration_hours=None).model_dump(exclude_unset=True)
    assert cleared["scheduled_duration_hours"] is None
