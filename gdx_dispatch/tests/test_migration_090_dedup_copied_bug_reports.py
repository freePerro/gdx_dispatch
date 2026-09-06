"""Migration 090: a copied bug report is folded into its original (body
appended without the 087 trailer, copy deleted) when an open original for the
same tenant, subject, reporter and day exists; pairs match one-to-one; a copy
with no open original stays; an original is never deleted; a closed copy or a
closed original leaves the pair alone; reruns are no-ops. Both engines."""
from __future__ import annotations

import importlib.util
import logging
import os
import pathlib
import sys
import types
from collections.abc import Generator

import pytest
from sqlalchemy import Engine, create_engine, text

MIGRATION = (
    pathlib.Path(__file__).resolve().parents[1]
    / "migrations/versions/090_dedup_copied_bug_reports.py"
)
MARK = "\n(copied from the retired bug_reports table by migration 087; same id)"
DDL = """CREATE TABLE support_tickets (
    id VARCHAR(36) PRIMARY KEY, tenant_id VARCHAR(36) NOT NULL,
    opened_by_email VARCHAR(200) NOT NULL, opened_by_user_id VARCHAR(36),
    subject VARCHAR(200) NOT NULL, body TEXT NOT NULL,
    category VARCHAR(20) NOT NULL, priority VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL,
    created_at TIMESTAMP NOT NULL, closed_at TIMESTAMP, resolution_summary TEXT)"""
INSERT = text(
    "INSERT INTO support_tickets (id, tenant_id, opened_by_email, opened_by_user_id, subject, body, "
    "category, priority, status, created_at, closed_at, resolution_summary) "
    "VALUES (:id, :tenant, :email, :user, :subject, :body, :category, :priority, :status, :created_at, "
    ":closed_at, :resolution)"
)
U = "11111111-1111-1111-1111-111111111111"


def _row(id, tenant, subject, body, created_at, user=U, closed_at=None, resolution=None, category="bug"):
    return dict(id=id, tenant=tenant, email="a@x", user=user, subject=subject, body=body,
                category=category, priority="medium", status="open", created_at=created_at,
                closed_at=closed_at, resolution=resolution)


ROWS = [
    # a pair: original + copy (same subject, reporter, day; the copy has the fuller text)
    _row("orig1", "t1", "Job close out", "Will not let me\nBrowser: Chrome", "2026-07-29 09:00:00"),
    _row("copy1", "t1", "Job close out", "Will not let me\n\n---\nPage: /mobile/jobs\nBrowser: Mozilla/5.0 (Linux; Android 14)" + MARK, "2026-07-29 09:00:01"),
    # a copy with no original: stays
    _row("copy2", "t1", "Mobile", "Old report" + MARK, "2026-07-06 10:00:00"),
    # same subject, DIFFERENT day: both stay
    _row("orig3", "t1", "Quick add", "Once", "2026-07-20 10:00:00"),
    _row("copy3", "t1", "Quick add", "Again" + MARK, "2026-07-29 10:00:00"),
    # same subject and day, a different tenant: stays
    _row("copy4", "t2", "Job close out", "Other shop" + MARK, "2026-07-29 12:00:00"),
    # same subject and day, a different reporter: stays
    _row("copy5", "t1", "Job close out", "Someone else" + MARK, "2026-07-29 13:00:00", user="22222222-2222-2222-2222-222222222222"),
    # a pair whose copy someone already closed: both stay as they were
    _row("orig6", "t1", "Closed copy", "Body", "2026-08-02 10:00:00"),
    _row("copy6", "t1", "Closed copy", "Body" + MARK, "2026-08-02 10:00:05", closed_at="2026-08-03 09:00:00", resolution="fixed"),
    # a pair whose ORIGINAL someone already closed: the copy stays (nothing folds into a closed ticket)
    _row("orig9", "t1", "Closed original", "Done", "2026-08-04 10:00:00", closed_at="2026-08-05 09:00:00", resolution="done"),
    _row("copy9", "t1", "Closed original", "Done" + MARK, "2026-08-04 10:00:05"),
    # two pairs sharing one key on one day: each copy folds into ITS OWN original, in order
    _row("origA", "t1", "Twice", "First", "2026-08-10 09:00:00"),
    _row("copyA", "t1", "Twice", "First" + MARK, "2026-08-10 09:00:01"),
    _row("origB", "t1", "Twice", "Second", "2026-08-10 15:00:00"),
    _row("copyB", "t1", "Twice", "Second" + MARK, "2026-08-10 15:00:01"),
    # two originals on the same day, no copy: untouched
    _row("orig7", "t1", "Feature idea", "Please", "2026-08-01 10:00:00"),
    _row("orig8", "t1", "Feature idea", "Please again", "2026-08-01 11:00:00"),
    # an original that merely QUOTES the 087 sentence mid-body is not a copy (the classifier is the exact suffix)
    _row("origQ", "t1", "Quoting", "A note that says (copied from the retired bug_reports table by migration 087; same id) in passing\nmore", "2026-08-11 09:00:00"),
    _row("origQ2", "t1", "Quoting", "A note that says (copied from the retired bug_reports table by migration 087; same id) in passing", "2026-08-11 10:00:00"),
    # same key but a FEATURE ticket earlier that day: the bug copy must not fold into it
    _row("origF", "t1", "Same words", "Idea", "2026-08-12 08:00:00", category="feature"),
    _row("copyF", "t1", "Same words", "Idea" + MARK, "2026-08-12 09:00:00"),
    # same key and category, but the bodies do not open with the same line: not the same report
    _row("origD", "t1", "Differs", "One report", "2026-08-13 08:00:00"),
    _row("copyD", "t1", "Differs", "Another report" + MARK, "2026-08-13 08:00:05"),
]
EXPECTED_IDS = {"orig1", "copy2", "orig3", "copy3", "copy4", "copy5", "orig6", "copy6", "orig9", "copy9",
                "origA", "origB", "orig7", "orig8", "origQ", "origQ2", "origF", "copyF", "origD", "copyD"}


def _seed(conn, ddl: str = DDL) -> None:
    conn.exec_driver_sql(ddl)
    for row in ROWS:
        conn.execute(INSERT, row)


def _load(conn):
    spec = importlib.util.spec_from_file_location("m090", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    fake = types.ModuleType("alembic")

    class _Op:
        def get_bind(self):
            return conn

    fake.op = _Op()
    saved = sys.modules.get("alembic")
    sys.modules["alembic"] = fake
    try:
        spec.loader.exec_module(mod)
    finally:
        if saved is not None:
            sys.modules["alembic"] = saved
        else:
            sys.modules.pop("alembic", None)
    return mod


def _ids(conn) -> set[str]:
    return {r[0] for r in conn.exec_driver_sql("SELECT id FROM support_tickets").fetchall()}


def _body(conn, id_: str) -> str:
    return conn.execute(text("SELECT body FROM support_tickets WHERE id = :id"), {"id": id_}).scalar()


def _assert_folded(conn) -> None:
    assert _ids(conn) == EXPECTED_IDS
    merged = _body(conn, "orig1")
    assert merged.startswith("Will not let me\nBrowser: Chrome"), "the original's own text leads"
    assert "folded in by migration 090" in merged
    assert "Mozilla/5.0 (Linux; Android 14)" in merged and "Page: /mobile/jobs" in merged, "the copy's fuller text survives"
    assert "migration 087" not in merged, "the 087 trailer is stripped, so a folded original never reads as a copy"
    assert _body(conn, "origA").startswith("First") and "First" in _body(conn, "origA").split("folded in")[1]
    assert _body(conn, "origB").startswith("Second") and "Second" in _body(conn, "origB").split("folded in")[1]
    assert _body(conn, "orig6") == "Body", "a closed copy is not folded — its original stays as it was"
    assert _body(conn, "orig9") == "Done" and _body(conn, "copy9").endswith(MARK), "nothing folds into a closed original"
    assert _body(conn, "copy2").endswith(MARK), "a lone copy is left exactly as it was"
    assert _body(conn, "origQ").endswith("more") and _body(conn, "origQ2").endswith("in passing"), "a quoting original is not a copy"
    assert _body(conn, "origF") == "Idea" and _body(conn, "copyF").endswith(MARK), "category is part of the key"
    assert _body(conn, "origD") == "One report" and _body(conn, "copyD").endswith(MARK), "bodies must open alike"


def test_it_chains_onto_089() -> None:
    source = MIGRATION.read_text()
    assert 'revision = "090_dedup_copied_bug_reports"' in source
    assert 'down_revision = "089_fold_superadmin_into_owner"' in source
    assert len("090_dedup_copied_bug_reports") <= 32


@pytest.fixture()
def sqlite_conn(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'm090.db'}", future=True)
    with eng.begin() as c:
        _seed(c)
    with eng.begin() as c:
        yield c
    eng.dispose()


def test_sqlite_folds_each_pair_and_logs_the_ids(sqlite_conn, caplog):
    m = _load(sqlite_conn)
    with caplog.at_level(logging.INFO, logger="alembic.runtime.migration"):
        m.upgrade()
    _assert_folded(sqlite_conn)
    msgs = [r.getMessage() for r in caplog.records]
    assert any("folded copied ticket copy1 into original orig1" in x for x in msgs)
    assert any("folded copied ticket copyA into original origA" in x for x in msgs)
    assert any("folded copied ticket copyB into original origB" in x for x in msgs)
    assert any("3 copied bug report(s) folded into their originals, 7 left as is" in x for x in msgs)
    assert any("copied ticket copyF has no open original" in x for x in msgs)


def test_sqlite_rerun_and_downgrade_touch_nothing(sqlite_conn, caplog):
    """The rerun is the case the first cut got wrong: a folded original carried
    the copy's 087 trailer and matched as a copy the second time."""
    m = _load(sqlite_conn)
    m.upgrade()
    before = {i: _body(sqlite_conn, i) for i in EXPECTED_IDS}
    with caplog.at_level(logging.INFO, logger="alembic.runtime.migration"):
        m.upgrade()
    assert any("0 copied bug report(s) folded into their originals, 7 left as is" in r.getMessage() for r in caplog.records)
    m.downgrade()
    assert _ids(sqlite_conn) == EXPECTED_IDS
    assert {i: _body(sqlite_conn, i) for i in EXPECTED_IDS} == before


def test_sqlite_fresh_install_without_the_table(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", future=True)
    with eng.begin() as c:
        _load(c).upgrade()
    eng.dispose()


_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
_requires_pg = pytest.mark.skipif(
    "postgresql" not in _URL,
    reason="the Postgres arm of 090 needs a real Postgres; set DATABASE_URL "
    "or TEST_DATABASE_URL to a postgres url",
)


def test_these_tests_actually_run_in_ci() -> None:
    if os.environ.get("CI"):
        assert "postgresql" in _URL, "CI is set but no postgres URL — the Postgres arm would skip."


@pytest.fixture()
def pg_conn() -> Generator[Engine, None, None]:
    schema = "m090_scratch"
    admin = create_engine(_URL, future=True)
    with admin.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        c.exec_driver_sql(f"CREATE SCHEMA {schema}")
    admin.dispose()
    eng = create_engine(_URL, future=True, connect_args={"options": f"-c search_path={schema}"})
    with eng.begin() as c:
        _seed(c, DDL.replace("TIMESTAMP NOT NULL", "TIMESTAMP WITH TIME ZONE NOT NULL").replace("closed_at TIMESTAMP", "closed_at TIMESTAMP WITH TIME ZONE"))
    with eng.begin() as c:
        yield c
    eng.dispose()
    admin = create_engine(_URL, future=True)
    with admin.begin() as c:
        c.exec_driver_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    admin.dispose()


@_requires_pg
def test_pg_folds_each_pair_and_reruns_clean(pg_conn):
    m = _load(pg_conn)
    m.upgrade()
    _assert_folded(pg_conn)
    before = {i: _body(pg_conn, i) for i in EXPECTED_IDS}
    m.upgrade()
    assert {i: _body(pg_conn, i) for i in EXPECTED_IDS} == before
