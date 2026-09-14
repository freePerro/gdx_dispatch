"""Stamping a chat read receipt leaves an audit row (#658, invariant #1).

`POST /api/mobile/chat/{id}/read` sets `read_at` / `read_by_user_id`. Since the
read-receipts work (#641) it fires once per unread tech message every time a
dispatcher opens a thread, and it wrote no `audit_logs` row.

Runs through the real handlers against a SQLite file built from the ORM, the
same harness shape as `test_chat_threads_seen_by_656.py`, plus one Postgres
race — the only engine where two requests genuinely interleave.
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from gdx_dispatch.core import audit as core_audit
from gdx_dispatch.core.audit import AuditLog, TenantBase, ensure_audit_table, verify_audit_chain
from gdx_dispatch.models import tenant_models  # noqa: F401  (registers every table)
from gdx_dispatch.models.tenant_models import JobChatMessage
from gdx_dispatch.routers import mobile_chat

_TECH = {"user_id": "user-1", "role": "technician", "tenant_id": "tenant-a"}
_DANA = {"user_id": "dispatcher-dana", "role": "dispatcher", "tenant_id": "tenant-a"}
_SAM = {"user_id": "owner-sam", "role": "owner", "tenant_id": "tenant-a"}


def _req() -> Request:
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    req.state.tenant = {"id": "tenant-a"}
    req.state.tenant_id = "tenant-a"
    return req


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'read_audit.sqlite3'}", connect_args={"check_same_thread": False}
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    now = datetime.now(UTC)
    job_id, customer_id = str(uuid4()), str(uuid4())
    db.execute(
        text("INSERT INTO customers (id, name, address, company_id) VALUES (:id, 'Acme', '1 Main', 'tenant-a')"),
        {"id": customer_id},
    )
    db.execute(
        text("INSERT INTO technicians (id, company_id, user_id, active, created_at) VALUES ('tech-1', 'tenant-a', 'user-1', 1, :n)"),
        {"n": now},
    )
    db.execute(
        text("INSERT INTO jobs (id, company_id, customer_id, title, dispatch_status, assigned_to, scheduled_at, created_at) "
             "VALUES (:id, 'tenant-a', :cid, 'Repair', 'on_site', 'user-1', :n, :n)"),
        {"id": job_id, "cid": customer_id, "n": now},
    )
    db.commit()
    yield db, job_id
    db.close()
    engine.dispose()


def _tech_says(db, job_id: str, body: str) -> str:
    resp = mobile_chat.send_job_chat(
        job_id=job_id, payload=mobile_chat.SendChatIn(kind="text", body=body),
        request=_req(), current_user=_TECH, db=db,
    )
    assert resp.status_code == 201, resp.body
    return json.loads(resp.body)["id"]


def _read(db, message_id: str, who: dict):
    return mobile_chat.mark_chat_read(message_id=message_id, request=_req(), current_user=who, db=db)


def _rows(db, action: str) -> list[AuditLog]:
    return list(db.execute(select(AuditLog).where(AuditLog.action == action)).scalars())


def test_control_the_send_in_the_same_harness_is_audited(env):
    """Without this, "0 read rows" could mean the audit table was never wired
    into this engine. The send path has always audited."""
    db, job_id = env
    msg_id = _tech_says(db, job_id, "Need a cable")
    sent = _rows(db, "mobile_chat_sent")
    assert [r.entity_id for r in sent] == [msg_id]


def test_the_first_read_writes_one_row_naming_the_reader(env):
    db, job_id = env
    msg_id = _tech_says(db, job_id, "Need a cable")

    assert _read(db, msg_id, _DANA).status_code == 200

    rows = _rows(db, "mobile_chat_read")
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == "dispatcher-dana"
    assert row.entity_type == "job_chat_message"
    assert row.entity_id == msg_id
    details = row.details if row.details is not None else (row.payload or {})
    assert details == {"job_id": job_id}


def test_a_repeat_read_writes_nothing_and_keeps_the_first_reader(env):
    """The stamp is write-once: a second dispatcher opening the same thread
    neither restamps it nor adds a row."""
    db, job_id = env
    msg_id = _tech_says(db, job_id, "Need a cable")
    _read(db, msg_id, _DANA)
    _read(db, msg_id, _SAM)

    assert [r.user_id for r in _rows(db, "mobile_chat_read")] == ["dispatcher-dana"]
    db.expire_all()
    stamped = db.get(JobChatMessage, UUID(msg_id))
    assert stamped.read_by_user_id == "dispatcher-dana"


def test_a_refused_read_writes_nothing(env):
    db, job_id = env
    msg_id = _tech_says(db, job_id, "Need a cable")

    assert _read(db, msg_id, _TECH).status_code == 403
    assert _rows(db, "mobile_chat_read") == []


def test_a_failed_audit_write_takes_the_stamp_with_it(env, monkeypatch):
    """Row counts cannot tell "audit then commit" from "commit then audit" —
    both produce one row. This can: the audit write raises, and the stamp must
    not survive it, or the badge clears for every dispatcher with no record of
    who cleared it."""
    db, job_id = env
    msg_id = _tech_says(db, job_id, "Need a cable")

    def _boom(*a, **kw):
        raise RuntimeError("audit backend down")

    monkeypatch.setattr(mobile_chat, "log_audit_event_sync", _boom)
    with pytest.raises(RuntimeError):
        _read(db, msg_id, _DANA)
    db.rollback()  # get_db's teardown closes the session, which discards the same uncommitted work

    read_at = db.execute(
        select(JobChatMessage.read_at).where(JobChatMessage.id == UUID(msg_id))
    ).scalar_one()
    assert read_at is None, "the read stamp committed even though its audit row failed"


def test_a_failed_first_audit_write_on_a_fresh_engine_takes_the_stamp_with_it(tmp_path, monkeypatch):
    """The inline `ensure_audit_table(db)` is load-bearing, and only on the
    FIRST audit write an engine ever sees: that call commits. Fired lazily
    from inside the audit write, it would harden the stamp just before the
    audit row failed. The tests above never reach that case — sending a
    message initializes the engine first — so this one seeds the message
    without the send path and fails the write after the guard has run."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'fresh.sqlite3'}", connect_args={"check_same_thread": False}
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    msg_id = uuid4()
    db.add(JobChatMessage(
        id=msg_id, company_id="tenant-a", job_id=str(uuid4()), sender_user_id="user-1",
        sender_role="tech", kind="text", body="Need a cable", created_at=datetime.now(UTC),
    ))
    db.commit()
    assert engine not in core_audit._AUDIT_GUARD_INITIALIZED, "precondition: nothing has audited on this engine"

    def _boom(*a, **kw):
        raise RuntimeError("audit row refused")

    # Past `ensure_audit_table` inside the audit writer, before the insert.
    monkeypatch.setattr(core_audit, "_sanitize_details", _boom)
    try:
        with pytest.raises(RuntimeError):
            _read(db, str(msg_id), _DANA)
        db.rollback()
        read_at = db.execute(select(JobChatMessage.read_at).where(JobChatMessage.id == msg_id)).scalar_one()
        assert read_at is None, "the audit guard's first-use commit hardened the stamp before its audit row failed"
    finally:
        db.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Postgres: two dispatchers open the same thread at the same moment
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_chat(pg_template_db):
    """A throwaway Postgres database holding just the two tables this handler
    writes. `structure.sql` predates `job_chat_messages`, so the template clone
    cannot be used; depending on `pg_template_db` still gives this test the
    shared reachability gate (skip on a laptop with no test PG)."""
    from gdx_dispatch.tests.fixtures.pg import PG_HOST, PG_PASSWORD, PG_PORT, PG_USER, _create_db, _drop_db

    name = f"chat_read_{uuid4().hex[:12]}"
    _create_db(name)
    engine = create_engine(f"postgresql+psycopg2://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{name}", future=True)
    TenantBase.metadata.create_all(engine, tables=[JobChatMessage.__table__, AuditLog.__table__])
    try:
        yield sessionmaker(bind=engine, future=True)
    finally:
        engine.dispose()
        _drop_db(name)


def test_two_dispatchers_racing_on_one_message_leave_one_row_and_an_intact_chain(pg_chat):
    """Check-then-write let both racers see `read_at IS NULL`: each stamped,
    each audited, and the two audit rows read the same previous hash. Measured
    before the fix: 30 of 30 barrier-synchronized trials wrote two rows.

    Scope, stated plainly: this is two dispatchers on the SAME message. The
    loser now writes nothing, so that pair cannot fork the chain. Two audited
    writes for DIFFERENT messages at the same moment still fork it, through
    core/audit.py's unlocked read-then-insert — a core race other writers
    already hit, not fixed here. The dialog sends one receipt at a time so it
    does not add bursts of those.

    Runs in CI once `GDX_TEST_PG_*` is wired there (#440); until then it skips
    on the CI runner like every other `pg_template_db` test."""
    Session = pg_chat
    with Session() as s:
        # Initialize the audit guard up front so the race below is the stamp,
        # not two threads installing DDL.
        ensure_audit_table(s)
        s.commit()

    trials, errors = 10, []
    for _ in range(trials):
        msg_id = uuid4()
        with Session() as s:
            s.add(JobChatMessage(
                id=msg_id, company_id="tenant-a", job_id=str(uuid4()), sender_user_id="user-1",
                sender_role="tech", kind="text", body="on site", created_at=datetime.now(UTC),
            ))
            s.commit()
        barrier = threading.Barrier(2)

        def read(who, msg_id=msg_id, barrier=barrier):
            try:
                with Session() as s:
                    barrier.wait(timeout=10)
                    resp = _read(s, str(msg_id), who)
                    assert resp.status_code == 200, resp.body
            except Exception as exc:  # noqa: BLE001 — surfaced below, threads swallow it
                errors.append(exc)

        threads = [threading.Thread(target=read, args=(who,)) for who in (_DANA, _SAM)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    assert not errors, errors

    with Session() as s:
        per_message = dict(s.execute(
            select(AuditLog.entity_id, func.count())
            .where(AuditLog.action == "mobile_chat_read")
            .group_by(AuditLog.entity_id)
        ).all())
        assert len(per_message) == trials
        assert set(per_message.values()) == {1}, per_message
        for row in s.execute(select(AuditLog).where(AuditLog.action == "mobile_chat_read")).scalars():
            stamp = s.get(JobChatMessage, UUID(row.entity_id))
            assert stamp.read_by_user_id == row.user_id, "the audit row names someone other than the stamp"
        assert verify_audit_chain(s) is True
