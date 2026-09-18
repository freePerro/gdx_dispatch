"""gdx-plugin-cellcomms — personal-cell texts/calls (plugin half).

Covers the load-bearing seams named in the cell-comms design record:
- the plugin's search hash MUST equal core's HashColumn.hash_for_search, or
  every customer match silently misses;
- webhook and backfill land the SAME real-world event in ONE row (exact-key
  and near-dup paths, including the call-enrichment path);
- list endpoints return BARE arrays (the host renderer does no unwrap);
- the customer.created handler links history retroactively.

Schema comes from the ORM (plugin models + core Customer.__table__), never
hand-written DDL — hand-written test DDL once hid a feature that could not be
inserted at all.
"""
from __future__ import annotations

import pathlib
import sys
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

_PLUGIN_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "plugins" / "gdx-plugin-cellcomms")
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

TENANT = "t-cell-1"


@pytest.fixture
def db():
    from gdx_plugin_cellcomms.models import CellCall, CellMessage

    from gdx_dispatch.models.tenant_models import Customer

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    for table in (Customer.__table__, CellMessage.__table__, CellCall.__table__):
        table.create(engine, checkfirst=True)
    # autoflush=False: EXACTLY core/database.py's SessionLocal config — the
    # 2026-09-18 re-audit caught autoflush=True fixtures masking a prod-only
    # IntegrityError path in the backfill.
    TS = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TS()
    yield session
    session.close()


def _ctx():
    from gdx_dispatch.plugin_api.context import PluginContext

    return PluginContext(tenant_id=TENANT, user_id="cell-gateway", role="webhook",
                         enabled_modules=frozenset())


def _seed_customer(db, phone: str, name: str = "Jane Door"):
    from gdx_plugin_cellcomms.matching import normalize_e164, search_hash

    from gdx_dispatch.models.tenant_models import Customer

    c = Customer(id=uuid.uuid4(), name=name, phone=phone,
                 phone_hash=search_hash(normalize_e164(phone)))
    # company scoping column name differs across eras; set whichever exists.
    if hasattr(c, "company_id"):
        c.company_id = TENANT
    db.add(c)
    db.commit()
    return c


SMS_EVENT = {
    "kind": "sms",
    "from": "(320) 555-0134",
    "text": "Garage door is stuck halfway",
    "sentStamp": "1758100000000",
    "receivedStamp": "1758100002000",
    "sim": "sim1",
}

CALL_EVENT = {
    "kind": "call",
    "from": "+13205550134",
    "contact": "Jane D",
    "timestamp": "1758100600000",
    "duration": "0",
}


def test_manifest_valid_and_screens_have_a_way_in():
    import gdx_plugin_cellcomms as mod

    m = mod.manifest
    assert m.key == "cellcomms"
    assert "events" in m.permissions
    assert set(m.events) == {"customer.created", "customer.updated"}
    assert callable(m.event_handler)
    types = [s["type"] for s in m.ui["screens"]]
    # Both feeds need a visible way in: lists for reading, upload for backfill.
    assert types.count("list") == 2
    assert "upload" in types
    upload = next(s for s in m.ui["screens"] if s["type"] == "upload")
    assert upload["endpoint"].startswith("/api/plugins/cellcomms/")


def test_search_hash_matches_core():
    """THE load-bearing parity: plugin hash == core HashColumn.hash_for_search,
    under whatever salt this process actually runs with. A drift here makes
    every customer match silently miss. (No module reload to vary the salt —
    core.pii reads it at import and other modules hold references; the deploy
    check that both CONTAINERS see the same salt lives in the plan doc.)"""
    from gdx_plugin_cellcomms import matching

    from gdx_dispatch.core.pii import HashColumn

    for value in ("+13205550134", "+13205550134 ", "+1320555WEIRD"):
        assert matching.search_hash(value) == HashColumn.hash_for_search(value)


def test_ingest_sms_links_customer_and_dedupes(db):
    from gdx_plugin_cellcomms.models import CellMessage
    from gdx_plugin_cellcomms.router import ingest_event

    cust = _seed_customer(db, "320-555-0134")
    r1 = ingest_event(dict(SMS_EVENT), ctx=_ctx(), db=db)
    assert r1["status"] == "ok" and r1["kind"] == "sms"
    r2 = ingest_event(dict(SMS_EVENT), ctx=_ctx(), db=db)  # nomad redelivery
    assert r2["status"] == "duplicate"

    rows = db.execute(select(CellMessage)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.other_number == "+13205550134"  # normalized, not the raw spelling
    assert row.direction == "in"
    assert row.customer_id == str(cust.id)
    assert row.customer_name == "Jane Door"
    assert row.sent_at is not None and row.sent_at.year >= 2025


def test_ingest_call_and_unknown_kind(db):
    from gdx_plugin_cellcomms.models import CellCall
    from gdx_plugin_cellcomms.router import ingest_event

    r = ingest_event(dict(CALL_EVENT), ctx=_ctx(), db=db)
    assert r["status"] == "ok" and r["kind"] == "call"
    row = db.execute(select(CellCall)).scalars().one()
    assert row.call_type == "incoming" and row.direction == "in"
    assert row.contact_name == "Jane D"

    with pytest.raises(HTTPException) as exc:
        ingest_event({"kind": "pigeon"}, ctx=_ctx(), db=db)
    assert exc.value.status_code == 422


SMS_XML = """<?xml version='1.0' encoding='UTF-8'?>
<smses count="4">
  <sms address="+13205550134" date="1758100000000" type="1" body="Garage door is stuck halfway" readable_date="x" />
  <sms address="3205550134" date="1758100100000" type="2" body="On my way, 20 min" />
  <sms address="+13205550199" date="1758100200000" type="3" body="draft never sent" />
  <mms address="+13205550134" date="1758100300000" />
</smses>
"""

CALLS_XML = """<?xml version='1.0' encoding='UTF-8'?>
<calls count="3">
  <call number="+13205550134" duration="184" date="1758100600000" type="1" contact_name="Jane D" />
  <call number="+13205550177" duration="45" date="1758101000000" type="2" />
  <call number="+13205550188" duration="0" date="1758101200000" type="3" />
</calls>
"""


def test_backfill_sms_dedupes_against_webhook_rows(db):
    from gdx_plugin_cellcomms.backfill import ingest_backup_xml
    from gdx_plugin_cellcomms.models import CellMessage
    from gdx_plugin_cellcomms.router import ingest_event

    # Live feed captured the incoming text first (nomad stamps a couple of
    # seconds off the SMS db's `date` — the near-dup window owns that gap).
    ingest_event(dict(SMS_EVENT), ctx=_ctx(), db=db)

    res = ingest_backup_xml(db, TENANT, SMS_XML.encode())
    assert res.messages_added == 1          # the OUTGOING one — webhook can't see those
    assert res.skipped_duplicates == 1      # the incoming one the webhook already has
    assert res.skipped_other == 1           # the draft
    assert res.mms_skipped == 1

    rows = db.execute(select(CellMessage)).scalars().all()
    assert len(rows) == 2
    out = next(r for r in rows if r.direction == "out")
    assert out.other_number == "+13205550134"  # bare 10-digit spelling converged
    assert out.source == "backfill"

    # Re-uploading the same file must be a complete no-op.
    res2 = ingest_backup_xml(db, TENANT, SMS_XML.encode())
    assert res2.messages_added == 0 and res2.skipped_duplicates == 2
    assert len(db.execute(select(CellMessage)).scalars().all()) == 2


def test_backfill_calls_enrich_ring_time_webhook_row(db):
    from gdx_plugin_cellcomms.backfill import ingest_backup_xml
    from gdx_plugin_cellcomms.models import CellCall
    from gdx_plugin_cellcomms.router import ingest_event

    # Webhook fired at ring time: duration 0, outcome unknown.
    ingest_event(dict(CALL_EVENT), ctx=_ctx(), db=db)

    res = ingest_backup_xml(db, TENANT, CALLS_XML.encode())
    # Incoming call ENRICHES the ring-time row; outgoing + missed are new.
    assert res.calls_enriched == 1
    assert res.calls_added == 2

    rows = db.execute(select(CellCall)).scalars().all()
    assert len(rows) == 3
    enriched = next(r for r in rows if r.other_number == "+13205550134")
    assert enriched.source == "webhook"       # same row, upgraded in place
    assert enriched.duration_s == 184         # the call log knew the real duration
    missed = next(r for r in rows if r.other_number == "+13205550188")
    assert missed.call_type == "missed" and missed.direction == "in"
    outgoing = next(r for r in rows if r.other_number == "+13205550177")
    assert outgoing.direction == "out"


def test_backfill_malformed_xml_imports_nothing(db):
    import xml.etree.ElementTree as ET

    from gdx_plugin_cellcomms.backfill import ingest_backup_xml
    from gdx_plugin_cellcomms.models import CellMessage

    with pytest.raises(ET.ParseError):
        ingest_backup_xml(db, TENANT, b"<smses><sms address='+1320555")
    db.rollback()
    assert db.execute(select(CellMessage)).scalars().all() == []


def test_list_endpoints_return_bare_arrays(db):
    """The host renderer assigns the response straight to the DataTable —
    an {"items": [...]} envelope renders zero rows (eventlog's guard, same)."""
    from gdx_plugin_cellcomms.router import ingest_event, list_calls, list_messages

    ingest_event(dict(SMS_EVENT), ctx=_ctx(), db=db)
    ingest_event(dict(CALL_EVENT), ctx=_ctx(), db=db)

    msgs = list_messages(q=None, ctx=_ctx(), db=db)
    calls = list_calls(q=None, ctx=_ctx(), db=db)
    assert isinstance(msgs, list) and isinstance(calls, list)
    assert set(msgs[0]) == {"when", "direction", "number", "customer", "body"}
    assert set(calls[0]) == {"when", "type", "number", "customer", "duration"}
    # server-side search actually filters
    assert list_messages(q="stuck", ctx=_ctx(), db=db)
    assert list_messages(q="no-such-text", ctx=_ctx(), db=db) == []


def test_customer_created_handler_links_history(db, monkeypatch):
    """A customer created AFTER their first text claims it — the retro-match
    hole the sms-caller-identity plan documents, closed event-driven here."""
    from gdx_plugin_cellcomms import handler as handler_mod
    from gdx_plugin_cellcomms.models import CellMessage
    from gdx_plugin_cellcomms.router import ingest_event

    from gdx_dispatch.plugin_api.events import PluginEvent

    ingest_event(dict(SMS_EVENT), ctx=_ctx(), db=db)
    row = db.execute(select(CellMessage)).scalars().one()
    assert row.customer_id is None  # nobody to match yet

    cust = _seed_customer(db, "+13205550134", name="Late Arrival")

    class _KeepOpenSession:
        """The handler close()s its session; the test still needs it."""

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            if name == "close":
                return lambda: None
            return getattr(self._real, name)

    monkeypatch.setattr(handler_mod, "SessionLocal", lambda: _KeepOpenSession(db))
    handler_mod.handle_customer_event(
        PluginEvent(name="customer.created", data={"customer_id": str(cust.id)},
                    tenant_id=TENANT, occurred_at="2026-09-18T00:00:00Z", delivery_id="d1")
    )

    db.expire_all()
    row = db.execute(select(CellMessage)).scalars().one()
    assert row.customer_id == str(cust.id)
    assert row.customer_name == "Late Arrival"


def test_two_calls_same_number_in_window_enrich_the_right_rows(db):
    """Audit falsifier 2026-09-18: 'called, missed, called right back'. Two
    webhook rows 120s apart; the backup stamps drift +10s and carry
    missed-then-answered outcomes. A naive nearest-.first() enriched the SAME
    first row twice — erasing the missed call and putting the duration on the
    wrong event."""
    from gdx_plugin_cellcomms.backfill import ingest_backup_xml
    from gdx_plugin_cellcomms.models import CellCall
    from gdx_plugin_cellcomms.router import ingest_event

    t0 = 1758100600  # epoch s
    for ts in (t0, t0 + 120):
        ingest_event(
            {"kind": "call", "from": "+13205550134", "contact": "Jane D",
             "timestamp": str(ts * 1000), "duration": "0"},
            ctx=_ctx(), db=db,
        )

    xml = f"""<calls count="2">
      <call number="+13205550134" duration="0" date="{(t0 + 10) * 1000}" type="3" />
      <call number="+13205550134" duration="184" date="{(t0 + 130) * 1000}" type="1" />
    </calls>"""
    res = ingest_backup_xml(db, TENANT, xml.encode())
    assert res.calls_enriched == 2 and res.calls_added == 0

    rows = sorted(db.execute(select(CellCall)).scalars().all(), key=lambda r: r.started_at)
    assert len(rows) == 2
    assert rows[0].call_type == "missed" and (rows[0].duration_s or 0) == 0
    assert rows[1].call_type == "incoming" and rows[1].duration_s == 184


def test_second_identical_text_is_not_absorbed_by_one_webhook_row(db):
    """One live row absorbs at most one backup element: two identical texts
    minutes apart, only the first caught live — the second must import."""
    from gdx_plugin_cellcomms.backfill import ingest_backup_xml
    from gdx_plugin_cellcomms.models import CellMessage
    from gdx_plugin_cellcomms.router import ingest_event

    t0 = 1758100000
    ingest_event(
        {"kind": "sms", "from": "+13205550134", "text": "Yes",
         "sentStamp": str(t0 * 1000)},
        ctx=_ctx(), db=db,
    )
    xml = f"""<smses count="2">
      <sms address="+13205550134" date="{(t0 + 5) * 1000}" type="1" body="Yes" />
      <sms address="+13205550134" date="{(t0 + 90) * 1000}" type="1" body="Yes" />
    </smses>"""
    res = ingest_backup_xml(db, TENANT, xml.encode())
    assert res.skipped_duplicates == 1 and res.messages_added == 1
    assert len(db.execute(select(CellMessage)).scalars().all()) == 2


def test_unreadable_stamps_do_not_collapse_distinct_texts(db):
    """Audit blind-spot: with sent_at NULL the old key omitted the stamp, so
    two DIFFERENT texts (same number, same body, both unreadable stamps)
    hashed identically. The raw stamp string now keeps them distinct — while a
    true redelivery (same raw stamp) still dedupes."""
    from gdx_plugin_cellcomms.models import CellMessage
    from gdx_plugin_cellcomms.router import ingest_event

    e1 = {"kind": "sms", "from": "+13205550134", "text": "Yes", "sentStamp": "not-a-date-A"}
    e2 = {"kind": "sms", "from": "+13205550134", "text": "Yes", "sentStamp": "not-a-date-B"}
    assert ingest_event(dict(e1), ctx=_ctx(), db=db)["status"] == "ok"
    assert ingest_event(dict(e2), ctx=_ctx(), db=db)["status"] == "ok"
    assert ingest_event(dict(e1), ctx=_ctx(), db=db)["status"] == "duplicate"
    assert len(db.execute(select(CellMessage)).scalars().all()) == 2


def test_reupload_after_enrichment_is_idempotent(db):
    """Re-audit falsifier 1 (2026-09-18): enrichment must persist the backup
    element's key (backfill_key), or re-uploading the same cumulative backup —
    which the README tells the operator to do — re-imports every enriched call
    as a fresh row."""
    from gdx_plugin_cellcomms.backfill import ingest_backup_xml
    from gdx_plugin_cellcomms.models import CellCall
    from gdx_plugin_cellcomms.router import ingest_event

    t0 = 1758100600
    # Two webhook ring-time rows: one answered later, one missed; the backup
    # stamps drift +10s (and one exact-stamp variant via t0 itself).
    ingest_event({"kind": "call", "from": "+13205550134", "timestamp": str(t0 * 1000),
                  "duration": "0"}, ctx=_ctx(), db=db)
    ingest_event({"kind": "call", "from": "+13205550199", "timestamp": str((t0 + 600) * 1000),
                  "duration": "0"}, ctx=_ctx(), db=db)
    xml = f"""<calls count="2">
      <call number="+13205550134" duration="184" date="{t0 * 1000}" type="1" />
      <call number="+13205550199" duration="0" date="{(t0 + 610) * 1000}" type="3" />
    </calls>"""

    res1 = ingest_backup_xml(db, TENANT, xml.encode())
    assert res1.calls_enriched == 2 and res1.calls_added == 0

    res2 = ingest_backup_xml(db, TENANT, xml.encode())
    assert res2.calls_added == 0 and res2.calls_enriched == 0
    assert res2.skipped_duplicates == 2

    rows = db.execute(select(CellCall)).scalars().all()
    assert len(rows) == 2
    assert {r.call_type for r in rows} == {"incoming", "missed"}


def test_in_file_duplicate_is_counted_not_500(db):
    """Re-audit falsifier 2: under prod's autoflush=False session (this
    fixture's config), an in-file duplicate element must count as a duplicate
    — the old per-element-query version relied on autoflush and raised
    IntegrityError at commit, 500ing the whole import."""
    from gdx_plugin_cellcomms.backfill import ingest_backup_xml
    from gdx_plugin_cellcomms.models import CellMessage

    xml = """<smses count="2">
      <sms address="+13205550134" date="1758100000000" type="1" body="dup me" />
      <sms address="+13205550134" date="1758100000000" type="1" body="dup me" />
    </smses>"""
    res = ingest_backup_xml(db, TENANT, xml.encode())
    assert res.messages_added == 1 and res.skipped_duplicates == 1
    assert len(db.execute(select(CellMessage)).scalars().all()) == 1


def test_backfill_is_query_bounded(db):
    """Re-audit falsifier 3: the proxy times a plugin call out at 30s, and a
    per-element-query backfill measured 47s on a realistic file. Pin the
    contract instead of a wall clock: SELECT count must scale with DISTINCT
    NUMBERS (memoized customer lookups) + fixed preloads — never with element
    count."""
    from gdx_plugin_cellcomms.backfill import ingest_backup_xml
    from sqlalchemy import event

    # ALL-DISTINCT numbers on purpose: round-3 audit caught an `i % 3` version
    # of this test that memoization satisfied while a real (distinct-heavy)
    # call log did one SELECT per number. Distinct numbers are the shape that
    # breaks caching — the test must fail for that defect or it proves nothing.
    parts = [
        f'<sms address="+1612555{i:04d}" date="{(1758100000 + i) * 1000}" type="1" body="m{i}" />'
        for i in range(200)
    ]
    xml = f'<smses count="200">{"".join(parts)}</smses>'

    selects = []
    engine = db.get_bind()

    def _count(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        res = ingest_backup_xml(db, TENANT, xml.encode())
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert res.messages_added == 200
    # Fixed preloads only (keys, webhook rows, customer map) + slack for
    # dialect bookkeeping. 200 all-distinct elements must NOT mean 200+
    # queries — neither per-element nor per-distinct-number scaling.
    assert len(selects) <= 10, f"{len(selects)} SELECTs for 200 elements:\n" + "\n".join(selects[:20])


def test_rematch_refreshes_renamed_customer(db):
    """Re-audit falsifier 4: the code claimed rematch refreshes the
    customer_name snapshot; it only linked NULLs. Rename → rematch must
    propagate, and the row must stay linked to the same customer."""
    from gdx_plugin_cellcomms.matching import rematch_unlinked
    from gdx_plugin_cellcomms.models import CellMessage
    from gdx_plugin_cellcomms.router import ingest_event

    cust = _seed_customer(db, "+13205550134", name="Old Name")
    ingest_event(dict(SMS_EVENT), ctx=_ctx(), db=db)
    row = db.execute(select(CellMessage)).scalars().one()
    assert row.customer_name == "Old Name"

    cust.name = "New Name"
    db.commit()

    out = rematch_unlinked(db, TENANT)
    assert out["names_refreshed"] == 1
    db.expire_all()
    row = db.execute(select(CellMessage)).scalars().one()
    assert row.customer_name == "New Name"
    assert row.customer_id == str(cust.id)
