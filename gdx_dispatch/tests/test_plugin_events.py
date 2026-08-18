"""Sprint 2 — the plugin event platform (WordPress-model hooks).

Covers: manifest validation (events/schedules require consent-gated permission +
handler), wildcard matching + capability fingerprint, plugin-host /internal/events
dispatch + per-plugin isolation + token gate, and core-side consent recipient
enumeration with fail-closed drift detection.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.plugin_api.events import (
    PluginEvent,
    capability_fingerprint,
    event_matches,
)
from gdx_dispatch.plugin_api.manifest import PluginManifest
from gdx_dispatch.plugin_host.app import create_plugin_host

# ---------------------------------------------------------------------------
# Manifest validation
# ---------------------------------------------------------------------------

def test_events_require_permission_and_handler():
    with pytest.raises(ValueError, match="requires the 'events' permission"):
        PluginManifest(key="x", name="X", events=("invoice.paid",), event_handler=lambda e: None)
    with pytest.raises(ValueError, match="callable event_handler"):
        PluginManifest(key="x", name="X", permissions=("events",), events=("invoice.paid",))
    # valid
    m = PluginManifest(key="x", name="X", permissions=("events",),
                       events=("invoice.paid",), event_handler=lambda e: None)
    assert m.events == ("invoice.paid",)


def test_schedules_require_permission_and_shape():
    with pytest.raises(ValueError, match="requires the 'schedules' permission"):
        PluginManifest(key="x", name="X", schedules=(("poll", "*/5 * * * *", lambda: None),))
    with pytest.raises(ValueError, match="name, cron, callable"):
        PluginManifest(key="x", name="X", permissions=("schedules",), schedules=(("poll",),))
    m = PluginManifest(key="x", name="X", permissions=("schedules",),
                       schedules=(("poll", "*/5 * * * *", lambda: None),))
    assert m.schedules[0][0] == "poll"


def test_ui_nav_icon_and_category_shape(caplog):
    # Valid: one PrimeIcons pair + a lowercase category key — kept verbatim.
    m = PluginManifest(key="x", name="X",
                       ui={"icon": "pi pi-history", "category": "operations", "screens": []})
    assert m.ui["icon"] == "pi pi-history"
    assert m.ui["category"] == "operations"
    # Malformed values are STRIPPED with a warning, never raised: discovery
    # skips a whole plugin on any load error, and a nav-icon typo must not
    # cost a plugin its event delivery (audit 2026-08-18). The frontend
    # falls back to the box icon / Plugins group for the stripped key.
    with caplog.at_level("WARNING"):
        m = PluginManifest(key="x", name="X",
                           ui={"icon": "pi pi-box evil-class", "screens": [{"type": "list"}]})
    assert "icon" not in m.ui
    assert m.ui["screens"] == [{"type": "list"}]  # rest of the ui survives
    assert "ui.icon" in caplog.text
    for bad_icon in ("fa fa-bomb", "pi pi-UPPER", 7):
        assert "icon" not in PluginManifest(key="x", name="X", ui={"icon": bad_icon}).ui
    for bad_category in ("Money Stuff!", "", 42):
        m = PluginManifest(key="x", name="X",
                           ui={"category": bad_category, "icon": "pi pi-bolt"})
        assert "category" not in m.ui
        assert m.ui["icon"] == "pi pi-bolt"  # the valid sibling key survives
    # Both optional; non-dict / absent ui stays untouched (Any by design).
    assert PluginManifest(key="x", name="X", ui={"screens": []}).ui == {"screens": []}
    assert PluginManifest(key="x", name="X", ui=None).ui is None


# ---------------------------------------------------------------------------
# Matching + fingerprint
# ---------------------------------------------------------------------------

def test_event_matches_exact_prefix_star():
    assert event_matches("invoice.paid", ["invoice.paid"])
    assert event_matches("invoice.paid", ["invoice.*"])
    assert event_matches("anything", ["*"])
    assert not event_matches("job.created", ["invoice.*"])
    assert not event_matches("invoice.paid", [])


def test_fingerprint_stable_and_sensitive():
    a = capability_fingerprint(events=["invoice.paid", "job.*"], schedule_names=["poll"])
    b = capability_fingerprint(events=["job.*", "invoice.paid"], schedule_names=["poll"])
    assert a == b  # order-independent
    c = capability_fingerprint(events=["invoice.paid", "job.*", "customer.created"], schedule_names=["poll"])
    assert a != c  # adding an event changes it → re-consent


# ---------------------------------------------------------------------------
# plugin-host /internal/events dispatch
# ---------------------------------------------------------------------------

def _host_with(received, key="n8n", events=("invoice.*",)):
    m = PluginManifest(
        key=key, name=key, permissions=("events",), events=events,
        event_handler=lambda e: received.append(e),
    )
    return create_plugin_host(plugins=[m])


def test_internal_events_dispatches_to_matching_handler():
    received: list[PluginEvent] = []
    client = TestClient(_host_with(received))
    r = client.post("/internal/events", json={
        "event": "invoice.paid", "data": {"invoice_id": "i1"}, "tenant_id": "t",
        "occurred_at": "2026-08-17T00:00:00Z", "delivery_id": "d1", "recipients": ["n8n"],
    })
    assert r.status_code == 200
    assert r.json()["dispatched"] == 1
    assert received[0].name == "invoice.paid"
    assert received[0].data["invoice_id"] == "i1"


def test_internal_events_rechecks_pattern_match():
    # Core named 'n8n' as a recipient, but the event doesn't match its declared
    # patterns → plugin-host refuses to deliver (belt-and-suspenders).
    received: list[PluginEvent] = []
    client = TestClient(_host_with(received, events=("invoice.*",)))
    r = client.post("/internal/events", json={
        "event": "job.created", "data": {}, "tenant_id": "t",
        "occurred_at": "x", "delivery_id": "d", "recipients": ["n8n"],
    })
    assert r.json()["dispatched"] == 0
    assert received == []


def test_internal_events_isolates_handler_failure():
    def boom(_e):
        raise RuntimeError("handler exploded")

    good: list = []
    m_bad = PluginManifest(key="bad", name="bad", permissions=("events",),
                           events=("*",), event_handler=boom)
    m_good = PluginManifest(key="good", name="good", permissions=("events",),
                            events=("*",), event_handler=lambda e: good.append(e))
    client = TestClient(create_plugin_host(plugins=[m_bad, m_good]))
    r = client.post("/internal/events", json={
        "event": "invoice.paid", "data": {}, "tenant_id": "t",
        "occurred_at": "x", "delivery_id": "d", "recipients": ["bad", "good"],
    })
    # bad raises, good still runs — one plugin's failure never starves another
    assert r.json()["dispatched"] == 1
    assert len(good) == 1


# ---------------------------------------------------------------------------
# plugin-host /internal/* token gate
# ---------------------------------------------------------------------------

def test_internal_token_gate_enforced_only_when_set():
    received: list = []
    client = TestClient(_host_with(received))
    body = {"event": "invoice.paid", "data": {}, "tenant_id": "t",
            "occurred_at": "x", "delivery_id": "d", "recipients": ["n8n"]}

    # token unset → open (staged rollout, network-isolation era)
    r = client.post("/internal/events", json=body)
    assert r.status_code == 200

    os.environ["GDX_INTERNAL_TOKEN"] = "s3cr3t-token"
    try:
        r = client.post("/internal/events", json=body)
        assert r.status_code == 401  # missing header
        r = client.post("/internal/events", json=body,
                        headers={"X-GDX-Internal-Token": "s3cr3t-token"})
        assert r.status_code == 200  # correct header
        r = client.post("/internal/events", json=body,
                        headers={"X-GDX-Internal-Token": "wrong"})
        assert r.status_code == 401  # wrong header
    finally:
        del os.environ["GDX_INTERNAL_TOKEN"]


async def _ok_stream(ws, url, key):
    await ws.accept()
    await ws.send_text("ok")
    await ws.close()


def test_internal_ws_token_gate():
    # The audit's BLOCKER: http middleware doesn't run for websocket scope, so
    # the WS must gate itself. Without the token it must close before accept;
    # with it, it passes (stream_browser mocked so no real Chromium launches).
    from starlette.websockets import WebSocketDisconnect

    received: list = []
    client = TestClient(_host_with(received))
    os.environ["GDX_INTERNAL_TOKEN"] = "ws-tok"
    try:
        with pytest.raises(WebSocketDisconnect), client.websocket_connect(
            "/internal/browser/ws?url=https://example.com&key=n8n"
        ) as ws:
            ws.receive_text()  # server closed 1008 → disconnect
        with (
            patch("gdx_dispatch.plugin_host.browser_stream.stream_browser", new=_ok_stream),
            client.websocket_connect(
                "/internal/browser/ws?url=https://example.com&key=n8n",
                headers={"X-GDX-Internal-Token": "ws-tok"},
            ) as ws,
        ):
            assert ws.receive_text() == "ok"
    finally:
        del os.environ["GDX_INTERNAL_TOKEN"]


def test_catalog_exposes_events_and_schedules():
    m = PluginManifest(
        key="n8n", name="n8n", permissions=("events", "schedules"),
        events=("invoice.*", "job.created"), event_handler=lambda e: None,
        schedules=(("health", "*/5 * * * *", lambda: None),),
    )
    client = TestClient(create_plugin_host(plugins=[m]))
    entry = client.get("/api/plugins").json()[0]
    assert entry["events"] == ["invoice.*", "job.created"]
    assert entry["schedules"] == ["health"]  # names only, never the callables


# ---------------------------------------------------------------------------
# Core consent — recipient enumeration + drift
# ---------------------------------------------------------------------------

def _consent_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


def test_event_recipients_enumerates_and_detects_drift():
    import gdx_dispatch.core.plugin_consent as pc

    db = _consent_session()
    catalog_v1 = [{"key": "n8n", "permissions": ["events"],
                   "events": ["invoice.*"], "schedules": [], "services": []}]
    with patch.object(pc, "fetch_catalog", return_value=catalog_v1):
        pc.record_consent(db, "n8n", ["events"], by="owner")
        recips, drifted = pc.event_recipients(db, "invoice.paid")
        assert recips == ["n8n"] and drifted == []
        # a non-matching event → no recipient
        assert pc.event_recipients(db, "job.created") == ([], [])

    # plugin UPGRADES its declared events (adds job.*) → live fingerprint differs
    # from the consented one → fail-closed drift, NOT silent delivery.
    catalog_v2 = [{"key": "n8n", "permissions": ["events"],
                   "events": ["invoice.*", "job.*"], "schedules": [], "services": []}]
    with patch.object(pc, "fetch_catalog", return_value=catalog_v2):
        recips, drifted = pc.event_recipients(db, "invoice.paid")
        assert recips == [] and drifted == ["n8n"]


def test_any_event_consent_gate():
    import gdx_dispatch.core.plugin_consent as pc

    db = _consent_session()
    pc.ensure_consent_table(db)
    assert pc.any_event_consent(db) is False
    with patch.object(pc, "fetch_catalog",
                      return_value=[{"key": "n8n", "events": ["invoice.*"], "schedules": [], "services": []}]):
        pc.record_consent(db, "n8n", ["events"], by="o")
    assert pc.any_event_consent(db) is True


def test_any_event_consent_missing_table_does_not_poison_postgres_txn(pg_test_session):
    # The Postgres-only BLOCKER: on a fresh box plugin_consent doesn't exist yet
    # (lazily created, not an ORM model), so a bare SELECT would raise
    # UndefinedTable and ABORT the caller's money transaction. SQLite can't
    # reproduce this (it doesn't poison), so it must be tested on real Postgres.
    from sqlalchemy import text

    import gdx_dispatch.core.plugin_consent as pc

    db = pg_test_session
    db.execute(text("DROP TABLE IF EXISTS plugin_consent"))
    db.flush()

    # Simulate a business write in flight (as at a choke point), then the emit
    # consent probe against the MISSING table...
    db.execute(text("CREATE TEMP TABLE _biz (id int)"))
    db.execute(text("INSERT INTO _biz VALUES (1)"))
    assert pc.any_event_consent(db) is False  # savepoint contains the UndefinedTable
    # ...and the business transaction is still alive — the write commits.
    db.commit()
    assert db.execute(text("SELECT count(*) FROM _biz")).scalar() == 1
