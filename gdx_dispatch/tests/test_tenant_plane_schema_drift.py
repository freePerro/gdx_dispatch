"""Wave H / S15 — schema drift scanner tests (pure-logic; no DB required)."""
from __future__ import annotations

from gdx_dispatch.tools.tenant_plane_schema_drift import diff_schemas


def test_no_drift_when_orm_and_live_match():
    orm = {"calls": {"id", "started_at"}, "msgs": {"id", "body"}}
    live = {"calls": {"id", "started_at"}, "msgs": {"id", "body"}}
    mt, mc, oc = diff_schemas(orm, live)
    assert mt == [] and mc == [] and oc == []


def test_missing_table_flagged():
    orm = {"calls": {"id"}, "msgs": {"id"}}
    live = {"calls": {"id"}}  # msgs missing
    mt, _, _ = diff_schemas(orm, live)
    assert mt == ["msgs"]


def test_missing_column_flagged():
    orm = {"calls": {"id", "started_at", "final_action_target"}}
    live = {"calls": {"id", "started_at"}}  # final_action_target missing
    _, mc, _ = diff_schemas(orm, live)
    assert mc == [("calls", "final_action_target")]


def test_orphan_column_flagged():
    orm = {"calls": {"id"}}
    live = {"calls": {"id", "legacy_company_id"}}  # extra column
    _, _, oc = diff_schemas(orm, live)
    assert oc == [("calls", "legacy_company_id")]


def test_all_three_classes_in_one_run():
    orm = {"a": {"id"}, "b": {"id", "name"}, "c": {"id"}}
    live = {"a": {"id", "leftover"}, "b": {"id"}}  # c missing, b.name missing, a.leftover orphan
    mt, mc, oc = diff_schemas(orm, live)
    assert mt == ["c"]
    assert mc == [("b", "name")]
    assert oc == [("a", "leftover")]


def test_alembic_version_table_not_flagged_when_missing():
    """alembic_version is intentionally missing on tenant DBs — no false positive."""
    from gdx_dispatch.tools.tenant_plane_schema_drift import _TABLE_ALLOWLIST_MISSING

    assert "alembic_version" in _TABLE_ALLOWLIST_MISSING
    orm = {"alembic_version": {"version_num"}, "calls": {"id"}}
    live = {"calls": {"id"}}
    mt, _, _ = diff_schemas(orm, live)
    assert mt == []
