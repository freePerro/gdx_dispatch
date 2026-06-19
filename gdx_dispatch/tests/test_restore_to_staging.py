"""SS-34 slice B tests — restore_to_staging."""
from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from gdx_dispatch.core.dr.restore_to_staging import (
    IntegrityMismatchError,
    ProductionTargetRefused,
    RestoreCommandError,
    RestoreError,
    RestoreTimeoutError,
    _redact_db_url,
    restore_snapshot_to_staging,
)


@dataclass
class _FakeManifest:
    id: str
    sha256: str
    backup_location: str
    created_at: datetime = datetime.now(timezone.utc)
    size_bytes: int = 0
    scope_description: str = "full"


def _fake_run_success(argv, **kwargs):
    return subprocess.CompletedProcess(
        args=argv, returncode=0, stdout="", stderr=""
    )


def _write_dump(tmp_path: Path, payload: bytes) -> _FakeManifest:
    p = tmp_path / "dump.pgc"
    p.write_bytes(payload)
    return _FakeManifest(
        id="snap-x-deadbeef",
        sha256=hashlib.sha256(payload).hexdigest(),
        backup_location=str(p),
        size_bytes=len(payload),
    )


def test_redact_db_url_handles_creds():
    assert _redact_db_url("postgresql://u:p@h/db") == "postgresql://***:***@h/db"
    assert _redact_db_url("postgresql://u@h/db") == "postgresql://***@h/db"
    assert _redact_db_url("postgresql://h/db") == "postgresql://h/db"


def test_refuses_production_host(tmp_path):
    m = _write_dump(tmp_path, b"x")
    with pytest.raises(ProductionTargetRefused):
        restore_snapshot_to_staging(
            manifest=m,
            staging_db_url="postgresql://u:p@prod.internal/db",
        )
    with pytest.raises(ProductionTargetRefused):
        restore_snapshot_to_staging(
            manifest=m,
            staging_db_url="postgresql://u:p@db-production-1/db",
        )


def test_prod_check_word_boundary_rejects_compound_hosts(tmp_path):
    """0.9-s A7: word-boundary tokenization catches prod-in-any-position."""
    m = _write_dump(tmp_path, b"x")
    # Token 'prod' at position 1 in dotted host.
    with pytest.raises(ProductionTargetRefused):
        restore_snapshot_to_staging(
            manifest=m,
            staging_db_url="postgresql://u:p@replica.prod.example/db",
        )
    # Token 'prod' embedded with dashes.
    with pytest.raises(ProductionTargetRefused):
        restore_snapshot_to_staging(
            manifest=m,
            staging_db_url="postgresql://u:p@cluster-prod-replica/db",
        )
    # Token 'prd' (common abbrev).
    with pytest.raises(ProductionTargetRefused):
        restore_snapshot_to_staging(
            manifest=m,
            staging_db_url="postgresql://u:p@db-prd-01/db",
        )


def test_prod_check_does_not_false_positive_on_word_fragments(tmp_path, monkeypatch):
    """0.9-s A7: 'reproduction' or 'prodigy' must NOT trip the guard."""
    # Stub subprocess so the restore would actually run if it got that far.
    m = _write_dump(tmp_path, b"x")
    monkeypatch.setattr(
        "gdx_dispatch.core.dr.restore_to_staging.subprocess.run",
        _fake_run_success,
    )
    # 'reproduction' contains 'prod' as substring but as part of a longer
    # token, not a whole token. Previous substring match would have fired
    # ('prod' in 'reproduction'); token-based match doesn't.
    res = restore_snapshot_to_staging(
        manifest=m,
        staging_db_url="postgresql://u:p@reproduction-staging/db",
    )
    assert res is not None  # no exception raised


def test_integrity_mismatch_refuses(tmp_path):
    m = _write_dump(tmp_path, b"original payload")
    # Tamper the file; manifest sha is for original.
    Path(m.backup_location).write_bytes(b"tampered payload")
    with patch(
        "gdx_dispatch.core.dr.restore_to_staging.subprocess.run",
        side_effect=_fake_run_success,
    ) as run:
        with pytest.raises(IntegrityMismatchError):
            restore_snapshot_to_staging(
                manifest=m,
                staging_db_url="postgresql://u:p@staging/db",
            )
        # pg_restore MUST NOT run if integrity check fails.
        run.assert_not_called()


def test_missing_file_raises(tmp_path):
    m = _FakeManifest(
        id="x",
        sha256="0" * 64,
        backup_location=str(tmp_path / "nope.pgc"),
    )
    with pytest.raises(RestoreError, match="does not exist"):
        restore_snapshot_to_staging(
            manifest=m,
            staging_db_url="postgresql://u:p@staging/db",
        )


def test_success_no_db_exec(tmp_path):
    m = _write_dump(tmp_path, b"payload-123")
    with patch(
        "gdx_dispatch.core.dr.restore_to_staging.subprocess.run",
        side_effect=_fake_run_success,
    ) as run:
        r = restore_snapshot_to_staging(
            manifest=m,
            staging_db_url="postgresql://u:p@staging/db",
        )
    argv = run.call_args.args[0]
    assert argv[0] == "pg_restore"
    assert "--clean" in argv
    assert "--if-exists" in argv
    assert "--exit-on-error" in argv
    assert f"--dbname=postgresql://u:p@staging/db" in argv
    assert run.call_args.kwargs["check"] is False
    assert run.call_args.kwargs["timeout"] == 14_400

    assert r.integrity_verified is True
    assert r.staging_db_url_redacted == "postgresql://***:***@staging/db"
    assert r.finished_at is not None
    assert r.verification_ready_at is not None
    assert r.rows_by_table == {}


def test_success_with_db_exec_counts_rows(tmp_path):
    m = _write_dump(tmp_path, b"rows")

    def fake_exec(sql: str):
        if "information_schema.tables" in sql:
            return [("public", "customers"), ("public", "jobs")]
        if "customers" in sql:
            return [(42,)]
        if "jobs" in sql:
            return [(7,)]
        return []

    with patch(
        "gdx_dispatch.core.dr.restore_to_staging.subprocess.run",
        side_effect=_fake_run_success,
    ):
        r = restore_snapshot_to_staging(
            manifest=m,
            staging_db_url="postgresql://u:p@staging/db",
            db_exec=fake_exec,
        )
    assert r.rows_by_table == {"public.customers": 42, "public.jobs": 7}
    assert r.errors == []


def test_pg_restore_failure_surfaces(tmp_path):
    m = _write_dump(tmp_path, b"z")

    def fail(argv, **kwargs):
        return subprocess.CompletedProcess(
            args=argv, returncode=1, stdout="", stderr="pg_restore: FATAL"
        )

    with patch("gdx_dispatch.core.dr.restore_to_staging.subprocess.run", side_effect=fail):
        with pytest.raises(RestoreCommandError) as exc:
            restore_snapshot_to_staging(
                manifest=m,
                staging_db_url="postgresql://u:p@staging/db",
            )
    assert exc.value.rc == 1
    assert "FATAL" in exc.value.stderr


def test_pg_restore_timeout(tmp_path):
    m = _write_dump(tmp_path, b"z")

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"])

    with patch(
        "gdx_dispatch.core.dr.restore_to_staging.subprocess.run",
        side_effect=timeout,
    ):
        with pytest.raises(RestoreTimeoutError):
            restore_snapshot_to_staging(
                manifest=m,
                staging_db_url="postgresql://u:p@staging/db",
                timeout_s=1,
            )
