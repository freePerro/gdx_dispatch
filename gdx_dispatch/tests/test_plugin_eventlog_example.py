"""Sprint 4b — the gdx-plugin-eventlog reference plugin.

Proves the event platform end-to-end with a REAL plugin package (not an inline
manifest): manifest validates, and a POST to plugin-host /internal/events reaches
the plugin's event_handler, which persists the event — the WordPress-model hook,
working.
"""
from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.plugin_api.events import PluginEvent
from gdx_dispatch.plugin_host.app import create_plugin_host

_PLUGIN_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "plugins" / "gdx-plugin-eventlog")
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


@pytest.fixture
def eventlog(monkeypatch):
    """Import the plugin and point its handler at an in-memory table."""
    import gdx_plugin_eventlog
    from gdx_plugin_eventlog import handler as handler_mod
    from gdx_plugin_eventlog.models import EventLogEntry

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    EventLogEntry.__table__.create(engine, checkfirst=True)
    TS = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(handler_mod, "SessionLocal", TS)
    return gdx_plugin_eventlog, EventLogEntry, TS


def test_manifest_valid(eventlog):
    mod, _, _ = eventlog
    m = mod.manifest
    assert m.key == "eventlog"
    assert "events" in m.permissions
    assert m.events == ("*",)
    assert callable(m.event_handler)


def test_handler_records_and_is_idempotent(eventlog):
    mod, EventLogEntry, TS = eventlog
    evt = PluginEvent(name="invoice.paid", data={"invoice_id": "i1"}, tenant_id="t1",
                      occurred_at="2026-08-17T00:00:00Z", delivery_id="d1")
    mod.handle_event(evt)
    mod.handle_event(evt)  # re-delivery (at-least-once) must not dupe
    db = TS()
    rows = db.execute(select(EventLogEntry)).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_name == "invoice.paid"
    assert rows[0].company_id == "t1"


def test_list_events_returns_bare_array_not_envelope(eventlog):
    """The list screen's endpoint MUST return a bare JSON array. The host
    renderer (frontend usePluginScreen.load) assigns the response straight to
    the DataTable value with no envelope unwrap, so an {"items": [...]} wrapper
    silently renders zero rows — the bug this guards against, caught live in the
    browser. Calling list_events directly (kwargs bypass FastAPI's Depends)
    with an empty result set is enough to pin the shape.
    """
    from types import SimpleNamespace

    from gdx_plugin_eventlog.router import list_events

    class _EmptyResult:
        def scalars(self):
            return self

        def all(self):
            return []

    class _DB:
        def execute(self, *a, **k):
            return _EmptyResult()

    out = list_events(ctx=SimpleNamespace(tenant_id="t1"), db=_DB())
    assert isinstance(out, list)  # bare array, NOT {"items": [...]}


def test_dispatch_via_internal_events_reaches_handler(eventlog):
    mod, EventLogEntry, TS = eventlog
    client = TestClient(create_plugin_host(plugins=[mod.manifest]))
    r = client.post("/internal/events", json={
        "event": "job.created", "data": {"job_id": "j1"}, "tenant_id": "t9",
        "occurred_at": "2026-08-17T00:00:00Z", "delivery_id": "d9", "recipients": ["eventlog"],
    })
    assert r.status_code == 200
    assert r.json()["dispatched"] == 1
    db = TS()
    rows = db.execute(select(EventLogEntry).where(EventLogEntry.event_name == "job.created")).scalars().all()
    assert len(rows) == 1
    assert rows[0].company_id == "t9"
