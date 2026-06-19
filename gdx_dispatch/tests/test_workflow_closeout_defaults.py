"""Phase 2 / C5 — closeout-gate defaults contract.

Doug 2026-05-10: when a new tenant signs up, the three closeout gates
(parts/hours/signature_on_complete) should be ON by default. Migration
079 alters the column server_default; the ORM `default=True` matches.
This pin asserts both sides stay in sync.

Existing tenants keep their stored values — the ALTER affects column
default only, not existing rows.
"""
from __future__ import annotations

from pathlib import Path

from gdx_dispatch.control.models import TenantSettings


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_orm_defaults_for_closeout_gates_are_true() -> None:
    """The TenantSettings ORM model defaults the three closeout flags to
    True. SQLAlchemy applies this at insert time when the column isn't
    explicitly set."""
    cols = {c.name: c for c in TenantSettings.__table__.columns}
    for name in (
        "workflow_require_parts_on_complete",
        "workflow_require_hours_on_complete",
        "workflow_require_signature_on_complete",
    ):
        col = cols[name]
        # The Python-side default
        assert col.default is not None, f"{name}: ORM default missing"
        assert col.default.arg is True, (
            f"{name} ORM default is {col.default.arg!r}, expected True. "
            "Migration 079 set the server-side default to true; the ORM "
            "must match so SQLAlchemy-issued INSERTs without the column "
            "land on True."
        )
        # The server-side default expression
        assert col.server_default is not None, f"{name}: server_default missing"
        sdt = str(col.server_default.arg).lower().strip("'\"")
        assert sdt == "true", (
            f"{name} server_default is {sdt!r}, expected 'true'. Migration "
            "079 ALTERed this on the live DB; the ORM model must reflect."
        )


def test_other_workflow_flags_stay_false() -> None:
    """Only parts/hours/signature default true. The other three workflow
    flags (lock_schedule_on_start, post_arrival_event, sms_arrival_notify)
    add side effects (timeline rows, SMS) and remain opt-in."""
    cols = {c.name: c for c in TenantSettings.__table__.columns}
    for name in (
        "workflow_lock_schedule_on_start",
        "workflow_post_arrival_event",
        "workflow_sms_arrival_notify",
    ):
        col = cols[name]
        assert col.default.arg is False, (
            f"{name} ORM default flipped to True — that wasn't C5's intent. "
            "Closeout gates default true; side-effect flags stay opt-in."
        )


def test_migration_079_present_and_alters_three_columns() -> None:
    """The migration file exists, has the right anchor, and ALTERs only
    the three closeout flags. Revision name must be ≤32 chars (the
    alembic_version.version_num column is varchar(32) — first deploy
    blew up with StringDataRightTruncation when the rev was 38 chars)."""
    path = REPO_ROOT / "gdx_dispatch" / "migrations" / "versions" / "079_closeout_defaults_true.py"
    assert path.exists(), "migration 079 not found"
    text = path.read_text(encoding="utf-8")
    assert 'revision = "079_closeout_defaults_true"' in text
    assert 'down_revision = "078_cc_audit_chain_hex"' in text
    # Pin the length guardrail explicitly — anyone bumping this filename
    # has to keep the rev string ≤32 chars.
    import re as _re
    m = _re.search(r'^revision = "([^"]+)"', text, _re.MULTILINE)
    assert m, "couldn't find revision string"
    assert len(m.group(1)) <= 32, (
        f"revision string {m.group(1)!r} is {len(m.group(1))} chars; "
        "alembic_version.version_num is varchar(32). See "
        "memory/project_cc_v2_platform_gotchas.md."
    )
    # The migration loops over _FLAGS, so it has ONE literal SET DEFAULT
    # statement per direction (true on upgrade, false on downgrade).
    assert "SET DEFAULT true" in text, "upgrade missing SET DEFAULT true"
    assert "SET DEFAULT false" in text, "downgrade missing SET DEFAULT false"
    # Must touch ONLY the three closeout flags
    for col in (
        "workflow_require_parts_on_complete",
        "workflow_require_hours_on_complete",
        "workflow_require_signature_on_complete",
    ):
        assert col in text, f"migration 079 missing column {col}"
    # Must NOT touch the other three workflow flags.
    for col in (
        "workflow_lock_schedule_on_start",
        "workflow_post_arrival_event",
        "workflow_sms_arrival_notify",
    ):
        assert col not in text, (
            f"migration 079 references {col} — it shouldn't. Only the "
            "closeout gates default true; side-effect flags stay opt-in."
        )
