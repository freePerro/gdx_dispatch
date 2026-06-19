"""SS-34 slice D tests — drill_orchestrator."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch
from uuid import uuid4

import pytest

from gdx_dispatch.core.dr.backup_snapshot import SnapshotError, SnapshotManifest
from gdx_dispatch.core.dr.drill_orchestrator import (
    DrillReport,
    reset_idempotency_cache,
    run_drill,
)
from gdx_dispatch.core.dr.restore_to_staging import (
    ProductionTargetRefused,
    RestoreError,
    RestoreReport,
)
from gdx_dispatch.core.dr.verification_harness import (
    CheckResult,
    VerificationReport,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_idempotency_cache()
    yield
    reset_idempotency_cache()


def _ok_snapshot(**kw) -> SnapshotManifest:
    return SnapshotManifest(
        id="snap-test-" + uuid4().hex[:8],
        created_at=datetime.now(timezone.utc),
        size_bytes=1024,
        sha256="a" * 64,
        scope_description=kw.get("scope", "full"),
        backup_location="/tmp/snap.pgc",
    )


def _ok_restore(**kw) -> RestoreReport:
    return RestoreReport(
        snapshot_id=kw["manifest"].id,
        staging_db_url_redacted="postgresql://***:***@staging/db",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
        duration_s=12.5,
        integrity_verified=True,
    )


def _ok_verify(**kw) -> VerificationReport:
    r = VerificationReport(run_started_at=datetime.now(timezone.utc))
    r.checks.append(CheckResult(name="rowcount:identities", passed=True, detail="10"))
    r.run_finished_at = datetime.now(timezone.utc)
    return r


def _fail_verify(**kw) -> VerificationReport:
    r = VerificationReport(run_started_at=datetime.now(timezone.utc))
    r.checks.append(CheckResult(name="rowcount:identities", passed=False, detail="0"))
    r.run_finished_at = datetime.now(timezone.utc)
    return r


def _base_kwargs():
    return dict(
        drill_run_id=str(uuid4()),
        scheduled_for=datetime.now(timezone.utc),
        scope="full",
        staging_db_url="postgresql://u:p@staging/db",
        source_db_url="postgresql://u:p@src/db",
        snapshot_target="/tmp/snap.pgc",
    )


def test_happy_path_full_drill_passes():
    events = []
    audits = []
    r = run_drill(
        **_base_kwargs(),
        snapshot_fn=_ok_snapshot,
        restore_fn=_ok_restore,
        verify_fn=_ok_verify,
        emit_event=lambda name, p: events.append((name, p)),
        write_audit=audits.append,
    )
    assert r.passed
    assert r.failure_reason is None
    assert r.snapshot is not None
    assert r.restore is not None
    assert r.verification is not None
    names = [n for n, _ in events]
    assert "gdx_dispatch.dr.drill_scheduled.v1" in names
    assert "gdx_dispatch.dr.drill_started.v1" in names
    assert "gdx_dispatch.dr.drill_completed.v1" in names
    assert "gdx_dispatch.dr.drill_failed.v1" not in names
    assert len(audits) == 1


def test_idempotent_on_drill_run_id():
    kw = _base_kwargs()
    calls = {"snap": 0}

    def counting_snapshot(**k):
        calls["snap"] += 1
        return _ok_snapshot(**k)

    r1 = run_drill(
        **kw,
        snapshot_fn=counting_snapshot,
        restore_fn=_ok_restore,
        verify_fn=_ok_verify,
    )
    r2 = run_drill(
        **kw,
        snapshot_fn=counting_snapshot,
        restore_fn=_ok_restore,
        verify_fn=_ok_verify,
    )
    assert r1 is r2
    assert calls["snap"] == 1  # second call served from cache


def test_dry_run_emits_events_but_no_snapshot():
    calls = {"snap": 0, "restore": 0, "verify": 0}

    def s(**k): calls["snap"] += 1; return _ok_snapshot(**k)
    def r(**k): calls["restore"] += 1; return _ok_restore(**k)
    def v(**k): calls["verify"] += 1; return _ok_verify(**k)

    events = []
    kw = _base_kwargs()
    report = run_drill(
        **kw, dry_run=True,
        snapshot_fn=s, restore_fn=r, verify_fn=v,
        emit_event=lambda n, p: events.append((n, p)),
    )
    assert report.dry_run is True
    assert report.passed is True
    assert calls == {"snap": 0, "restore": 0, "verify": 0}
    names = [n for n, _ in events]
    assert "gdx_dispatch.dr.drill_scheduled.v1" in names
    assert "gdx_dispatch.dr.drill_started.v1" in names
    assert "gdx_dispatch.dr.drill_completed.v1" in names


def test_refuses_when_staging_equals_database_url():
    kw = _base_kwargs()
    with patch.dict(os.environ, {"DATABASE_URL": kw["staging_db_url"]}):
        with pytest.raises(ProductionTargetRefused):
            run_drill(
                **kw,
                snapshot_fn=_ok_snapshot,
                restore_fn=_ok_restore,
                verify_fn=_ok_verify,
            )


def test_snapshot_failure_emits_drill_failed_and_raises():
    events = []
    audits = []

    def failing_snapshot(**k):
        raise SnapshotError("pg_dump not found")

    with pytest.raises(SnapshotError):
        run_drill(
            **_base_kwargs(),
            snapshot_fn=failing_snapshot,
            restore_fn=_ok_restore,
            verify_fn=_ok_verify,
            emit_event=lambda n, p: events.append((n, p)),
            write_audit=audits.append,
        )
    names = [n for n, _ in events]
    assert "gdx_dispatch.dr.drill_failed.v1" in names
    stage_payload = next(p for n, p in events if n == "gdx_dispatch.dr.drill_failed.v1")
    assert stage_payload["stage"] == "snapshot"
    assert len(audits) == 1


def test_restore_failure_emits_drill_failed_and_raises():
    events = []

    def failing_restore(**k):
        raise RestoreError("bogus")

    with pytest.raises(RestoreError):
        run_drill(
            **_base_kwargs(),
            snapshot_fn=_ok_snapshot,
            restore_fn=failing_restore,
            verify_fn=_ok_verify,
            emit_event=lambda n, p: events.append((n, p)),
        )
    names = [n for n, _ in events]
    assert "gdx_dispatch.dr.drill_failed.v1" in names


def test_verification_failure_does_not_raise_returns_report():
    events = []
    r = run_drill(
        **_base_kwargs(),
        snapshot_fn=_ok_snapshot,
        restore_fn=_ok_restore,
        verify_fn=_fail_verify,
        emit_event=lambda n, p: events.append((n, p)),
    )
    assert r.passed is False
    assert r.failure_reason is not None
    assert "verification" in r.failure_reason
    names = [n for n, _ in events]
    assert "gdx_dispatch.dr.drill_failed.v1" in names
    payload = next(p for n, p in events if n == "gdx_dispatch.dr.drill_failed.v1")
    assert payload["stage"] == "verification"
    assert payload["failed_count"] == 1


def test_to_dict_shape():
    r = run_drill(
        **_base_kwargs(),
        snapshot_fn=_ok_snapshot,
        restore_fn=_ok_restore,
        verify_fn=_ok_verify,
    )
    d = r.to_dict()
    assert set(d.keys()) >= {
        "drill_run_id", "scheduled_for", "started_at", "finished_at",
        "scope", "dry_run", "passed", "failure_reason",
        "snapshot", "restore", "verification",
    }
    assert d["snapshot"]["sha256"] == "a" * 64


def test_emit_event_failure_is_swallowed_not_raised():
    def bad_emit(n, p):
        raise RuntimeError("bus down")

    r = run_drill(
        **_base_kwargs(),
        snapshot_fn=_ok_snapshot,
        restore_fn=_ok_restore,
        verify_fn=_ok_verify,
        emit_event=bad_emit,
    )
    # Drill still passed despite event bus failure.
    assert r.passed is True
