"""D-PE-7 / GDXA-13 — pave_tenant_db.py reload and rebuild guards.

D-PE-7: pave's reload step previously ran psql with `ON_ERROR_STOP=off` and
silently dropped errors containing "does not exist", masking
type-incompatible row loss. Strict mode (now default) flips that to
`ON_ERROR_STOP=on` and aborts the pave on any reload error so the pre-pave
full backup can be restored.

GDXA-13: pave dumped every table in the database and recreated only the ORM's,
so the reload hit `alembic_version` — 4th COPY in the dump, in no ORM — and
`ON_ERROR_STOP=on` stopped there, leaving a 245-table, 0-row database. These
tests cover the pieces of that fix that can be proven with a mock. The one
that cannot — that a real pave of a real migrated database keeps its rows — is
`test_pave_rebuild_pg.py`, which needs a Postgres.
"""
from __future__ import annotations

import subprocess
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


# --------------------------------------------------------------------------
# GDXA-13 — the dump contract and the recreate contract have to agree.
# --------------------------------------------------------------------------


def test_data_dump_excludes_alembic_version():
    """`alembic upgrade head` writes the stamp row itself. Reloading the
    dumped one on top is a duplicate key, and under --strict that aborts the
    whole reload on the 4th COPY — which is how a pave used to end at 0 rows."""
    captured = {}

    def fake_run(cmd, env=None, capture_output=False, text=False):
        captured["cmd"] = cmd
        return _completed(0)

    with patch.object(pave_tenant_db.subprocess, "run", side_effect=fake_run):
        pave_tenant_db.dump_data(DB_INFO, "/tmp/x.sql")

    cmd = captured["cmd"]
    assert "--exclude-table" in cmd
    assert "public.alembic_version" in cmd
    # The exclusion must precede the database name, or pg_dump reads it as a
    # second positional argument.
    assert cmd.index("public.alembic_version") < cmd.index(DB_INFO["dbname"])


def test_unrecreatable_tables_is_everything_create_all_will_not_rebuild():
    """The Alembic base and the plugin tables, together and deliberately —
    step 4c throws away the ones Alembic brings back on its own, so
    over-capturing is free and under-capturing loses a table."""
    orm_table = next(iter(sorted(pave_tenant_db.TenantBase.metadata.tables)))
    live = ["alembic_version", orm_table, "plug_chipricing_catalog", "server_errors"]
    with patch.object(pave_tenant_db, "get_table_names", return_value=live):
        out = pave_tenant_db.unrecreatable_tables(object())
    assert out == ["alembic_version", "plug_chipricing_catalog", "server_errors"]
    assert orm_table not in out  # create_all() rebuilds it; capturing it would collide


def test_capture_refuses_before_anything_is_dropped(caplog):
    """The preflight is the whole point of the ordering: discovery of what
    cannot be rebuilt happens while the database still exists."""
    with patch.object(pave_tenant_db.subprocess, "run",
                      return_value=_completed(1, stderr="pg_dump: error: boom")), \
         pytest.raises(SystemExit) as exc:
        pave_tenant_db.capture_table_schemas(DB_INFO, ["plug_thing"])
    assert exc.value.code == 1
    assert any("NOTHING has been dropped" in r.message for r in caplog.records)


class _FakeConn:
    """Minimal stand-in for `engine.connect()` returning one fixed result."""

    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, _stmt):
        return self

    def fetchall(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _FakeEngine:
    def __init__(self, rows):
        self._rows = rows

    def connect(self):
        return _FakeConn(self._rows)


def test_capture_schema_objects_refuses_when_the_catalog_has_no_ddl(caplog, tmp_path):
    """Half a capture is worse than none: an object we cannot reproduce has to
    stop the pave while the database still exists, not after the DROP."""
    engine = _FakeEngine([("function", "weird_thing()", None)])
    with pytest.raises(SystemExit) as exc:
        pave_tenant_db.capture_schema_objects(DB_INFO, engine)
    assert exc.value.code == 1
    assert any("NOTHING has been dropped" in r.message for r in caplog.records)


def test_capture_schema_objects_writes_one_replayable_file_per_object(tmp_path, monkeypatch):
    """Per-object files are what let step 4c replay a subset."""
    rows = [
        ("function", "audit_logs_immutable_guard()", "CREATE FUNCTION audit_logs_immutable_guard() ..."),
        ("trigger", "audit_logs_no_update ON audit_logs", "CREATE TRIGGER audit_logs_no_update ..."),
    ]
    written = {}

    class _Capture:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, text):
            written[self.path] = text

    monkeypatch.setattr("builtins.open", lambda path, mode="r": _Capture(path))
    captured = pave_tenant_db.capture_schema_objects(DB_INFO, _FakeEngine(rows))

    assert set(captured) == {
        ("function", "audit_logs_immutable_guard()"),
        ("trigger", "audit_logs_no_update ON audit_logs"),
    }
    # Distinct files, and each ends in exactly one statement terminator.
    assert len(set(captured.values())) == 2
    for body in written.values():
        assert body.endswith(";\n")
        assert not body.endswith(";;\n")


def test_replay_retries_an_object_whose_first_pass_failed():
    """Nothing in the captured set has a reliable creation order: a trigger
    needs its function, a view needs its table, and `pg_dump -t` gives no
    cross-table ordering at all — a captured plugin table can carry a foreign
    key to another captured plugin table that sorts after it. The loop has to
    converge rather than give up on pass one.

    Two tables, child first, because that is the one ordering `_replay_order`
    cannot help with: it puts tables ahead of other kinds, not ahead of each
    other."""
    calls = []

    def fake_run(cmd, env=None, capture_output=False, text=False):
        target = cmd[cmd.index("-f") + 1]
        calls.append(target)
        # The child's FK needs the parent: it fails until the parent is applied.
        if target.endswith("aline.sql") and not any(c.endswith("catalog.sql") for c in calls[:-1]):
            return _completed(
                3, stderr='ERROR:  relation "plug_fake_catalog" does not exist')
        return _completed(0)

    captured = {
        ("table", "plug_fake_aline"): "/tmp/s_aline.sql",
        ("table", "plug_fake_catalog"): "/tmp/s_catalog.sql",
    }
    with patch.object(pave_tenant_db, "schema_inventory", return_value=set()), \
         patch.object(pave_tenant_db.subprocess, "run", side_effect=fake_run):
        pave_tenant_db.replay_table_schemas(DB_INFO, object(), captured)

    assert calls.count("/tmp/s_aline.sql") == 2  # failed, then retried and applied


def test_replay_fails_loudly_when_a_pass_makes_no_progress(caplog):
    """A dependency cycle must abort, not half-restore the schema."""
    captured = {("table", "plug_a"): "/tmp/s_plug_a.sql", ("table", "plug_b"): "/tmp/s_plug_b.sql"}
    with patch.object(pave_tenant_db, "schema_inventory", return_value=set()), \
         patch.object(pave_tenant_db.subprocess, "run",
                      return_value=_completed(3, stderr="ERROR:  nope")), \
         pytest.raises(SystemExit) as exc:
        pave_tenant_db.replay_table_schemas(DB_INFO, object(), captured)
    assert exc.value.code == 1
    assert any("made no progress" in r.message for r in caplog.records)


def test_replay_skips_objects_the_rebuild_already_brought_back():
    """Over-capture has to be free, or the capture list becomes load-bearing:
    the Alembic-base tables and the ORM's enums come back on their own and
    must not be replayed on top of themselves."""
    captured = {
        ("table", "alembic_version"): "/tmp/s_alembic_version.sql",
        ("enum", "invoice_status"): "/tmp/s_invoice_status.sql",
    }
    present = {("table", "alembic_version"), ("enum", "invoice_status")}
    with patch.object(pave_tenant_db, "schema_inventory", return_value=present), \
         patch.object(pave_tenant_db.subprocess, "run") as run:
        pave_tenant_db.replay_table_schemas(DB_INFO, object(), captured)
    run.assert_not_called()


def test_replay_does_not_reapply_an_object_another_file_brought_with_it():
    """`pg_dump -t <table>` carries that table's own triggers, so applying the
    table's file can make a separately-captured trigger present. Replaying it
    anyway is `CREATE TRIGGER ... already exists`, which fails identically on
    every pass — the loop would burn to "no progress" and exit with the schema
    already dropped. Verified on PostgreSQL 16.15, 2026-09-24."""
    calls = []
    inventories = [
        set(),                                                         # before pass 1
        {("table", "plug_a"), ("trigger", "plug_a_touch ON plug_a")},  # after pass 1
    ]

    def fake_run(cmd, env=None, capture_output=False, text=False):
        target = cmd[cmd.index("-f") + 1]
        calls.append(target)
        if target.endswith("trigger.sql"):
            return _completed(
                3, stderr='ERROR:  trigger "plug_a_touch" for relation "plug_a" already exists')
        return _completed(0)

    captured = {
        ("trigger", "plug_a_touch ON plug_a"): "/tmp/s_trigger.sql",
        ("table", "plug_a"): "/tmp/s_plug_a.sql",
    }
    with patch.object(pave_tenant_db, "schema_inventory", side_effect=inventories), \
         patch.object(pave_tenant_db.subprocess, "run", side_effect=fake_run):
        pave_tenant_db.replay_table_schemas(DB_INFO, object(), captured)

    # Tables first; the trigger's one failed attempt is then settled by reading
    # the catalog, not by a retry that can only fail the same way.
    assert calls == ["/tmp/s_plug_a.sql", "/tmp/s_trigger.sql"]


def test_replay_order_puts_tables_before_everything_else():
    """A table's file brings its indexes, constraints and triggers along, so
    doing tables first spares most of the rest a failed attempt."""
    items = [
        ("trigger", "t ON plug_a"),
        ("function", "f()"),
        ("table", "plug_z"),
        ("table", "plug_a"),
        ("view", "v"),
    ]
    assert pave_tenant_db._replay_order(items)[:2] == [("table", "plug_a"), ("table", "plug_z")]


class _RecordingConn:
    """engine.connect() that answers the matview query and records the rest."""

    def __init__(self, rows, executed):
        self._rows = rows
        self._executed = executed

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, stmt):
        self._executed.append(str(stmt))
        return self

    def fetchall(self):
        return self._rows

    def commit(self):
        pass


class _RecordingEngine:
    def __init__(self, rows):
        self._rows = rows
        self.executed: list[str] = []

    def connect(self):
        return _RecordingConn(self._rows, self.executed)


def test_refresh_skips_a_matview_that_was_already_unpopulated():
    """Restoring the prior state, not improving on it. Refreshing a matview the
    database had left empty makes step 7 report a mismatch for a difference the
    pave itself introduced — a loud false alarm on a destructive tool."""
    engine = _RecordingEngine([("mv_live", False), ("mv_never_refreshed", False)])
    failures = pave_tenant_db.refresh_matviews(engine, {"mv_live"})

    refreshes = [s for s in engine.executed if "REFRESH" in s]
    assert failures == []
    assert len(refreshes) == 1
    assert "mv_live" in refreshes[0]
    assert "mv_never_refreshed" not in refreshes[0]


def test_main_exits_non_zero_when_the_pave_finished_with_mismatches():
    """`⚠ PAVE COMPLETE WITH MISMATCHES` used to exit 0, so `pave && restart`
    treated lost rows, an unset sequence or an empty matview as success."""
    with patch.object(pave_tenant_db.sys, "argv", ["pave", "--yes", "postgresql:///x"]), \
         patch.object(pave_tenant_db, "pave_one", return_value=False), \
         pytest.raises(SystemExit) as exc:
        pave_tenant_db.main()
    assert exc.value.code == 1


def test_main_exits_zero_on_a_clean_pave():
    with patch.object(pave_tenant_db.sys, "argv", ["pave", "--yes", "postgresql:///x"]), \
         patch.object(pave_tenant_db, "pave_one", return_value=True):
        pave_tenant_db.main()  # no SystemExit


def test_alembic_upgrade_head_aborts_the_pave_when_it_fails(caplog):
    """This runs after the DROP. A silent failure here is an empty database."""
    with patch.object(pave_tenant_db.subprocess, "run",
                      return_value=_completed(1, stderr="FAILED: Can't locate revision")), \
         pytest.raises(SystemExit) as exc:
        pave_tenant_db.alembic_upgrade_head("postgresql://u:p@h:5432/d")
    assert exc.value.code == 1
    assert any("alembic upgrade head FAILED" in r.message for r in caplog.records)


def test_alembic_upgrade_head_points_alembic_at_the_target_database():
    """Not at whatever DATABASE_URL the container happens to carry — pave can
    be given an explicit URL, and `_bypass_proxy_host` may have rewritten it."""
    captured = {}

    def fake_run(cmd, cwd=None, env=None, capture_output=False, text=False):
        captured["cmd"], captured["cwd"], captured["env"] = cmd, cwd, env
        return _completed(0)

    with patch.object(pave_tenant_db.subprocess, "run", side_effect=fake_run):
        pave_tenant_db.alembic_upgrade_head("postgresql://u:p@tenant-db:5432/gdx")

    assert captured["cmd"][1:] == ["-m", "alembic", "-c", "alembic.ini", "upgrade", "head"]
    assert captured["env"]["ALEMBIC_DATABASE_URL"] == "postgresql://u:p@tenant-db:5432/gdx"
    # script_location in alembic.ini is relative, so cwd has to be the package.
    assert captured["cwd"].endswith("gdx_dispatch")


def test_verify_counts_fails_when_an_EMPTY_table_vanished(caplog):
    """The delta that matters.

    The old check compared row counts under a `pre > 0` guard, so a dropped
    table that had held rows already failed it (as "MISMATCH … 139 -> 0"). A
    dropped **empty** table passed clean — `pre == 0`, nothing to mismatch —
    and the drop itself was only an INFO line. That is the case this asserts;
    running the old implementation on these inputs returns True.
    """
    pre = {"customers": 5, "plug_consent": 0}
    post = {"customers": 5}
    assert pave_tenant_db.verify_counts(pre, post) is False
    assert any("MISSING TABLE plug_consent" in r.message for r in caplog.records)


def test_verify_counts_names_a_vanished_table_as_a_table_not_as_lost_rows(caplog):
    """A non-empty vanished table failed before too, but as a row mismatch.
    The operator needs to be told the table is gone."""
    assert pave_tenant_db.verify_counts({"server_errors": 139}, {}) is False
    assert any("MISSING TABLE server_errors" in r.message for r in caplog.records)


def test_verify_counts_passes_when_the_table_list_and_counts_match():
    counts = {"customers": 5, "server_errors": 139}
    assert pave_tenant_db.verify_counts(counts, dict(counts)) is True
