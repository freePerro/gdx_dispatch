"""SS-30 slice A tests — cutover preflight safety checks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import cutover_preflight as cp
from gdx_dispatch.models.platform_ss29_additions import (
    SS29Base,
    ShadowMigrationCheckpoint,
    ShadowMigrationDrift,
    ShadowMigrationState,
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SS29Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    s = S()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _now():
    return datetime.now(timezone.utc)


def _mk_state(db, tenant, table, mode="shadow"):
    row = ShadowMigrationState(
        id=uuid4(),
        tenant_id=tenant,
        old_table=table,
        new_table=table.replace("_v1", "_v2"),
        mode=mode,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def _mk_checkpoint(db, tenant, table):
    row = ShadowMigrationCheckpoint(
        id=uuid4(),
        tenant_id=tenant,
        old_table=table,
        last_row_id=100,
        last_row_pk="100",
        row_count_this_session=100,
        updated_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def _mk_drift(db, tenant, table, when=None):
    row = ShadowMigrationDrift(
        id=uuid4(),
        tenant_id=tenant,
        old_table=table,
        reason="hash_mismatch",
        old_hash="a" * 64,
        new_hash="b" * 64,
        details={},
        created_at=when or _now(),
    )
    db.add(row)
    db.flush()
    return row


def _chain_ok(*args, **kwargs):
    return (True, -1)


def _chain_broken(*args, **kwargs):
    return (False, 3)


def test_happy_path_all_pass(db, monkeypatch):
    _mk_state(db, "t1", "customers_v1", mode="shadow")
    _mk_checkpoint(db, "t1", "customers_v1")
    monkeypatch.setattr(cp, "verify_chain", _chain_ok)

    report = cp.run_preflight(db, tenant_id="t1", old_table="customers_v1")
    assert report.passed is True
    assert len(report.checks) == 4
    assert all(c.passed for c in report.checks)
    assert report.failed_checks == ()


def test_shadow_not_enabled_fails(db, monkeypatch):
    # No state row at all.
    _mk_checkpoint(db, "t1", "customers_v1")
    monkeypatch.setattr(cp, "verify_chain", _chain_ok)

    report = cp.run_preflight(db, tenant_id="t1", old_table="customers_v1")
    assert report.passed is False
    names = {c.name for c in report.failed_checks}
    assert cp.CHECK_SHADOW_ENABLED in names


def test_state_mode_off_fails(db, monkeypatch):
    _mk_state(db, "t1", "customers_v1", mode="off")
    _mk_checkpoint(db, "t1", "customers_v1")
    monkeypatch.setattr(cp, "verify_chain", _chain_ok)

    report = cp.run_preflight(db, tenant_id="t1", old_table="customers_v1")
    assert report.passed is False
    shadow_check = next(c for c in report.checks if c.name == cp.CHECK_SHADOW_ENABLED)
    assert not shadow_check.passed
    assert "mode='off'" in shadow_check.detail or "off" in shadow_check.detail


def test_already_cutover_fails(db, monkeypatch):
    _mk_state(db, "t1", "customers_v1", mode="cutover")
    _mk_checkpoint(db, "t1", "customers_v1")
    monkeypatch.setattr(cp, "verify_chain", _chain_ok)

    report = cp.run_preflight(db, tenant_id="t1", old_table="customers_v1")
    assert report.passed is False


def test_drift_in_window_fails(db, monkeypatch):
    _mk_state(db, "t1", "customers_v1", mode="shadow")
    _mk_checkpoint(db, "t1", "customers_v1")
    _mk_drift(db, "t1", "customers_v1")
    monkeypatch.setattr(cp, "verify_chain", _chain_ok)

    report = cp.run_preflight(db, tenant_id="t1", old_table="customers_v1")
    assert report.passed is False
    drift_check = next(c for c in report.checks if c.name == cp.CHECK_DRIFT_CLEAN)
    assert not drift_check.passed
    assert "1 drift" in drift_check.detail


def test_old_drift_outside_window_passes(db, monkeypatch):
    _mk_state(db, "t1", "customers_v1", mode="shadow")
    _mk_checkpoint(db, "t1", "customers_v1")
    # 48h ago — outside default 24h window.
    _mk_drift(db, "t1", "customers_v1", when=_now() - timedelta(hours=48))
    monkeypatch.setattr(cp, "verify_chain", _chain_ok)

    report = cp.run_preflight(db, tenant_id="t1", old_table="customers_v1")
    assert report.passed is True


def test_chain_break_fails_hard(db, monkeypatch):
    _mk_state(db, "t1", "customers_v1", mode="shadow")
    _mk_checkpoint(db, "t1", "customers_v1")
    monkeypatch.setattr(cp, "verify_chain", _chain_broken)

    report = cp.run_preflight(db, tenant_id="t1", old_table="customers_v1")
    assert report.passed is False
    chain_check = next(
        c for c in report.checks if c.name == cp.CHECK_AUDIT_CHAIN_INTACT
    )
    assert not chain_check.passed
    assert "break" in chain_check.detail or "broken" in chain_check.detail


def test_backfill_missing_fails(db, monkeypatch):
    _mk_state(db, "t1", "customers_v1", mode="shadow")
    # NO checkpoint row.
    monkeypatch.setattr(cp, "verify_chain", _chain_ok)

    report = cp.run_preflight(db, tenant_id="t1", old_table="customers_v1")
    assert report.passed is False
    bf = next(c for c in report.checks if c.name == cp.CHECK_BACKFILL_CAUGHT_UP)
    assert not bf.passed


def test_backfill_check_fn_override_used(db, monkeypatch):
    _mk_state(db, "t1", "customers_v1", mode="shadow")
    monkeypatch.setattr(cp, "verify_chain", _chain_ok)

    calls = []

    def check_fn(db_, tenant, table):
        calls.append((tenant, table))
        return (True, "custom pass: 1000/1000")

    report = cp.run_preflight(
        db, tenant_id="t1", old_table="customers_v1",
        backfill_check_fn=check_fn,
    )
    assert calls == [("t1", "customers_v1")]
    assert report.passed is True
    bf = next(c for c in report.checks if c.name == cp.CHECK_BACKFILL_CAUGHT_UP)
    assert "custom pass" in bf.detail


def test_backfill_check_fn_fail_blocks(db, monkeypatch):
    _mk_state(db, "t1", "customers_v1", mode="shadow")
    monkeypatch.setattr(cp, "verify_chain", _chain_ok)

    report = cp.run_preflight(
        db, tenant_id="t1", old_table="customers_v1",
        backfill_check_fn=lambda *a: (False, "900/1000 rows"),
    )
    assert report.passed is False


def test_to_dict_structure(db, monkeypatch):
    _mk_state(db, "t1", "customers_v1", mode="shadow")
    _mk_checkpoint(db, "t1", "customers_v1")
    monkeypatch.setattr(cp, "verify_chain", _chain_ok)

    report = cp.run_preflight(db, tenant_id="t1", old_table="customers_v1")
    d = report.to_dict()
    assert d["tenant_id"] == "t1"
    assert d["old_table"] == "customers_v1"
    assert d["passed"] is True
    assert len(d["checks"]) == 4
    assert all("name" in c and "passed" in c for c in d["checks"])


def test_missing_tenant_raises(db):
    with pytest.raises(ValueError, match="tenant_id"):
        cp.run_preflight(db, tenant_id="", old_table="customers_v1")


def test_missing_table_raises(db):
    with pytest.raises(ValueError, match="old_table"):
        cp.run_preflight(db, tenant_id="t1", old_table="")
