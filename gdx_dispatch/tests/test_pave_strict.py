"""D-PE-7 — pave_tenant_db.py strict-mode reload guard.

Pave's reload step previously ran psql with `ON_ERROR_STOP=off` and silently
dropped errors containing "does not exist", masking type-incompatible row
loss. Strict mode (now default) flips that to `ON_ERROR_STOP=on` and aborts
the pave on any reload error so the pre-pave full backup can be restored.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gdx_dispatch.tools import pave_tenant_db


DB_INFO = {
    "host": "localhost",
    "port": "5432",
    "user": "u",
    "password": "p",
    "dbname": "test_pave",
}


def _completed(returncode: int, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout="", stderr=stderr)


def test_strict_uses_on_error_stop_on():
    captured = {}

    def fake_run(cmd, env=None, capture_output=False, text=False):
        captured["cmd"] = cmd
        return _completed(0)

    with patch.object(pave_tenant_db.subprocess, "run", side_effect=fake_run):
        pave_tenant_db.reload_data(DB_INFO, "/tmp/x.sql", strict=True)

    assert "ON_ERROR_STOP=on" in captured["cmd"]
    assert "ON_ERROR_STOP=off" not in captured["cmd"]


def test_no_strict_uses_on_error_stop_off():
    captured = {}

    def fake_run(cmd, env=None, capture_output=False, text=False):
        captured["cmd"] = cmd
        return _completed(0)

    with patch.object(pave_tenant_db.subprocess, "run", side_effect=fake_run):
        pave_tenant_db.reload_data(DB_INFO, "/tmp/x.sql", strict=False)

    assert "ON_ERROR_STOP=off" in captured["cmd"]


def test_strict_aborts_on_psql_failure(caplog):
    """Non-zero psql return code under strict mode must sys.exit(1) so the
    pave doesn't proceed to verify_counts and report success on a busted DB."""
    err = "psql:/tmp/x.sql:42: ERROR:  unrecognized configuration parameter \"transaction_timeout\""
    with patch.object(pave_tenant_db.subprocess, "run", return_value=_completed(3, stderr=err)):
        with pytest.raises(SystemExit) as exc:
            pave_tenant_db.reload_data(
                DB_INFO, "/tmp/x.sql", strict=True, full_backup_path="/tmp/test_pave_full.sql"
            )
    assert exc.value.code == 1
    # Backup-restore hint is the load-bearing line for the operator.
    assert any("/tmp/test_pave_full.sql" in r.message for r in caplog.records)


def test_strict_aborts_on_error_lines_even_with_zero_returncode(caplog):
    """Defense in depth: some psql builds emit ERROR but exit 0 when a meta
    command fails. Strict mode treats any ERROR line as fatal."""
    err = "psql:/tmp/x.sql:1: ERROR:  relation \"customers\" does not exist"
    with patch.object(pave_tenant_db.subprocess, "run", return_value=_completed(0, stderr=err)):
        with pytest.raises(SystemExit):
            pave_tenant_db.reload_data(DB_INFO, "/tmp/x.sql", strict=True)


def test_no_strict_continues_on_failure(caplog):
    """Legacy behavior: --no-strict logs loud but does not exit, so an
    operator who knowingly accepts row loss can still pave."""
    err = "psql:/tmp/x.sql:42: ERROR:  some recoverable issue"
    with patch.object(pave_tenant_db.subprocess, "run", return_value=_completed(3, stderr=err)):
        # Must not raise.
        pave_tenant_db.reload_data(DB_INFO, "/tmp/x.sql", strict=False)
    assert any("Data reload had" in r.message or "Data reload FAILED" in r.message
               for r in caplog.records)
