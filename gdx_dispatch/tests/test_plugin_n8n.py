"""The gdx-plugin-n8n Automations console plugin.

Proves the n8n integration surface end-to-end as a REAL plugin package: manifest
validates, a POST to plugin-host /internal/events reaches the handler (which
mirrors the event into its own table), and every declarative `list` screen
endpoint returns a BARE ARRAY (the host renderer contract — an {"items": …}
wrapper renders an empty table, see test_plugin_eventlog_example).
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.plugin_api.events import PluginEvent
from gdx_dispatch.plugin_host.app import create_plugin_host

_PLUGIN_DIR = str(pathlib.Path(__file__).resolve().parents[2] / "plugins" / "gdx-plugin-n8n")
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)


@pytest.fixture
def n8n(monkeypatch):
    """Import the plugin and point its handler at an in-memory table."""
    import gdx_plugin_n8n
    from gdx_plugin_n8n import handler as handler_mod
    from gdx_plugin_n8n.models import N8nEvent

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    N8nEvent.__table__.create(engine, checkfirst=True)
    TS = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(handler_mod, "SessionLocal", TS)
    return gdx_plugin_n8n, N8nEvent, TS


def test_manifest_valid(n8n):
    mod, _, _ = n8n
    m = mod.manifest
    assert m.key == "n8n"
    assert "events" in m.permissions
    assert m.events == ("*",)
    assert callable(m.event_handler)


def test_handler_records_and_is_idempotent(n8n):
    mod, N8nEvent, TS = n8n
    evt = PluginEvent(name="invoice.paid", data={"invoice_id": "i1"}, tenant_id="t1",
                      occurred_at="2026-08-18T00:00:00Z", delivery_id="d1")
    mod.handle_event(evt)
    mod.handle_event(evt)  # re-delivery (at-least-once) must not dupe
    db = TS()
    rows = db.execute(select(N8nEvent)).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_name == "invoice.paid"
    assert rows[0].company_id == "t1"


def test_list_screen_endpoints_return_bare_arrays(n8n):
    """Every `type: list` screen endpoint must return a bare array, not an
    envelope — else the DataTable renders empty despite 200 OK."""
    mod, N8nEvent, TS = n8n
    from gdx_plugin_n8n.router import connection_info, list_catalog, list_events

    class _Empty:
        def scalars(self):
            return self

        def all(self):
            return []

        def scalar(self):  # connection_info counts via COUNT(*).scalar()
            return 0

    class _DB:
        def execute(self, *a, **k):
            return _Empty()

    ctx = SimpleNamespace(tenant_id="t1")
    assert isinstance(list_events(ctx=ctx, db=_DB()), list)
    assert isinstance(list_catalog(ctx=ctx), list)
    assert isinstance(connection_info(ctx=ctx, db=_DB()), list)


def test_setup_copy_is_honest_about_delivery(n8n):
    """The console shows what GDX EMITTED; it does not itself deliver to n8n
    (that's the webhook path). Guard against re-introducing the audit-caught lie
    that Activity is 'the same stream n8n receives'."""
    mod, _, _ = n8n
    from gdx_plugin_n8n.ui import UI

    setup = next(s for s in UI["screens"] if s["type"] == "help")
    blob = " ".join(line for sec in setup["sections"] for line in sec["body"]).lower()
    assert "same stream n8n receives" not in blob
    # It must point delivery confirmation at the webhook surface, not this tab.
    assert "settings → webhooks" in blob


def test_catalog_is_nonempty_and_shaped(n8n):
    from gdx_plugin_n8n.router import list_catalog

    rows = list_catalog(ctx=SimpleNamespace(tenant_id="t1"))
    assert rows, "event catalog must not be empty"
    assert {"event", "fires_when"} <= set(rows[0].keys())
    events = {r["event"] for r in rows}
    assert {"invoice.paid", "customer.created"} <= events


def test_dispatch_via_internal_events_reaches_handler(n8n):
    mod, N8nEvent, TS = n8n
    client = TestClient(create_plugin_host(plugins=[mod.manifest]))
    r = client.post("/internal/events", json={
        "event": "customer.created", "data": {"customer_id": "c1", "name": "Acme"},
        "tenant_id": "t9", "occurred_at": "2026-08-18T00:00:00Z",
        "delivery_id": "d9", "recipients": ["n8n"],
    })
    assert r.status_code == 200
    assert r.json()["dispatched"] == 1
    db = TS()
    rows = db.execute(select(N8nEvent).where(N8nEvent.event_name == "customer.created")).scalars().all()
    assert len(rows) == 1
    assert rows[0].company_id == "t9"
