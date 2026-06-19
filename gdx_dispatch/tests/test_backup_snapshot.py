"""SS-34 slice A tests — backup_snapshot.

pg_dump is mocked via ``subprocess.run`` patch so these tests run in
CI without a Postgres server.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from gdx_dispatch.core.dr.backup_snapshot import (
    DEFAULT_TIMEOUT_S,
    SnapshotCommandError,
    SnapshotError,
    SnapshotTimeoutError,
    create_snapshot,
)


def _fake_pg_dump_success(target_path: Path, payload: bytes):
    """Build a ``subprocess.run`` replacement that writes ``payload``
    to the ``--file=...`` path discovered in argv, then returns rc=0."""

    def _runner(argv, **kwargs):
        file_arg = next(a for a in argv if a.startswith("--file="))
        out = Path(file_arg.split("=", 1)[1])
        out.write_bytes(payload)
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="", stderr=""
        )

    return _runner


def test_create_snapshot_full_success(tmp_path):
    payload = b"FAKE PGDUMP BYTES " * 100
    target = tmp_path / "dump.pgc"

    with patch(
        "gdx_dispatch.core.dr.backup_snapshot.subprocess.run",
        side_effect=_fake_pg_dump_success(target, payload),
    ) as run:
        m = create_snapshot(
            label="t1",
            source_db_url="postgresql://u@h/db",
            target_location=target,
            scope="full",
        )

    # argv built correctly — custom format, file target, no schema flag.
    argv = run.call_args.args[0]
    assert argv[0] == "pg_dump"
    assert "--format=custom" in argv
    assert f"--file={target}" in argv
    assert not any(a.startswith("--schema=") for a in argv)
    assert argv[-1] == "postgresql://u@h/db"

    # Timeout honoured, check=False.
    assert run.call_args.kwargs["timeout"] == DEFAULT_TIMEOUT_S
    assert run.call_args.kwargs["check"] is False
    assert run.call_args.kwargs["capture_output"] is True

    # Manifest integrity.
    assert m.size_bytes == len(payload)
    assert m.sha256 == hashlib.sha256(payload).hexdigest()
    assert m.scope_description == "full"
    assert m.backup_location == str(target)
    assert m.id.startswith("snap-t1-")
    assert m.created_at.tzinfo is not None


def test_create_snapshot_tenant_requires_selector(tmp_path):
    with pytest.raises(SnapshotError, match="requires scope_selector"):
        create_snapshot(
            label="t2",
            source_db_url="postgresql://u@h/db",
            target_location=tmp_path / "x.pgc",
            scope="tenant",
        )


def test_create_snapshot_tenant_scope_builds_schema_flag(tmp_path):
    target = tmp_path / "t.pgc"
    with patch(
        "gdx_dispatch.core.dr.backup_snapshot.subprocess.run",
        side_effect=_fake_pg_dump_success(target, b"x"),
    ) as run:
        create_snapshot(
            label="t3",
            source_db_url="postgresql://u@h/db",
            target_location=target,
            scope="tenant",
            scope_selector="tenant_abc",
            scope_description="tenant abc only",
        )

    argv = run.call_args.args[0]
    assert "--schema=tenant_abc" in argv


def test_create_snapshot_invalid_scope_raises(tmp_path):
    with pytest.raises(SnapshotError, match="invalid scope"):
        create_snapshot(
            label="t4",
            source_db_url="postgresql://u@h/db",
            target_location=tmp_path / "x.pgc",
            scope="bogus",
        )


def test_create_snapshot_parent_missing_raises(tmp_path):
    with pytest.raises(SnapshotError, match="parent dir"):
        create_snapshot(
            label="t5",
            source_db_url="postgresql://u@h/db",
            target_location=tmp_path / "missing_parent" / "x.pgc",
            scope="full",
        )


def test_create_snapshot_pg_dump_failure_surfaces_stderr(tmp_path):
    target = tmp_path / "f.pgc"

    def _fail(argv, **kwargs):
        return subprocess.CompletedProcess(
            args=argv, returncode=2, stdout="", stderr="pg_dump: connection refused",
        )

    with patch("gdx_dispatch.core.dr.backup_snapshot.subprocess.run", side_effect=_fail):
        with pytest.raises(SnapshotCommandError) as exc:
            create_snapshot(
                label="t6",
                source_db_url="postgresql://u@h/db",
                target_location=target,
                scope="full",
            )
    assert exc.value.rc == 2
    assert "connection refused" in exc.value.stderr


def test_create_snapshot_timeout_cleans_partial(tmp_path):
    target = tmp_path / "to.pgc"
    target.write_bytes(b"partial")

    def _timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    with patch("gdx_dispatch.core.dr.backup_snapshot.subprocess.run", side_effect=_timeout):
        with pytest.raises(SnapshotTimeoutError):
            create_snapshot(
                label="t7",
                source_db_url="postgresql://u@h/db",
                target_location=target,
                scope="full",
                timeout_s=1,
            )
    # Partial artifact cleaned.
    assert not target.exists()


# --- Security: argument-injection hardening (redteam HIGH close-out) ---


def test_rejects_label_with_shell_metachar(tmp_path):
    with pytest.raises(ValueError, match="label="):
        create_snapshot(
            label="foo;rm -rf /",
            source_db_url="postgresql://u@h/db",
            target_location=tmp_path / "x.pgc",
            scope="full",
        )


def test_rejects_label_starting_with_dash(tmp_path):
    with pytest.raises(ValueError, match="label="):
        create_snapshot(
            label="--help",
            source_db_url="postgresql://u@h/db",
            target_location=tmp_path / "x.pgc",
            scope="full",
        )


def test_rejects_target_location_traversal(tmp_path):
    with pytest.raises(ValueError, match="traversal"):
        create_snapshot(
            label="ok",
            source_db_url="postgresql://u@h/db",
            target_location=str(tmp_path / ".." / ".." / "etc" / "passwd"),
            scope="full",
        )


def test_rejects_source_db_url_flag_injection(tmp_path):
    with pytest.raises(ValueError, match="source_db_url"):
        create_snapshot(
            label="ok",
            source_db_url="--version",
            target_location=tmp_path / "x.pgc",
            scope="full",
        )


def test_rejects_scope_selector_with_metachar(tmp_path):
    with pytest.raises(ValueError, match="scope_selector"):
        create_snapshot(
            label="ok",
            source_db_url="postgresql://u@h/db",
            target_location=tmp_path / "x.pgc",
            scope="tenant",
            scope_selector="public; DROP SCHEMA secrets",
        )


def test_create_snapshot_missing_artifact_after_success(tmp_path):
    target = tmp_path / "gone.pgc"

    def _success_no_write(argv, **kwargs):
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="", stderr=""
        )

    with patch(
        "gdx_dispatch.core.dr.backup_snapshot.subprocess.run",
        side_effect=_success_no_write,
    ):
        with pytest.raises(SnapshotError, match="target .* missing"):
            create_snapshot(
                label="t8",
                source_db_url="postgresql://u@h/db",
                target_location=target,
                scope="full",
            )
