"""Consent drift reaches the owner, and re-consent clears it (GDXA-462).

A plugin upgrade that changes its declared events/schedules/services makes both
dispatchers fail closed until the owner re-consents. Before this, the only trace
was an ERROR log and a `plugin_consent_drift` AIAction row that nothing read and
nothing ever moved out of `pending`, so the throttled alarm went quiet for good
after the first drift. These tests drive the real consent rows and the real
route handlers; only the plugin-host catalog (an HTTP call) is replaced.

Imports the router module (core.database etc.) → runs in the docker image.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from gdx_dispatch.core import audit as audit_core
from gdx_dispatch.core import plugin_consent as pc
from gdx_dispatch.core import plugin_events as pe
from gdx_dispatch.core.audit import AuditLog, TenantBase
from gdx_dispatch.core.plugin_events import _signal_consent_drift
from gdx_dispatch.core.webhooks.models import AIAction
from gdx_dispatch.routers import admin_plugins as ap

OWNER = {"sub": "owner-1", "role": "owner"}


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    TenantBase.metadata.create_all(engine, checkfirst=True)
    session = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    audit_core.audit_ready_db(session)
    yield session
    session.close()
    engine.dispose()


def _entry(key, perms, events=(), schedules=(), services=()):
    return {
        "key": key, "name": key.title(), "permissions": list(perms),
        "events": list(events), "schedules": list(schedules),
        "schedule_specs": [{"name": s, "cron": "* * * * *"} for s in schedules],
        "services": list(services),
    }


@pytest.fixture(autouse=True)
def _rearm_unthrottled(monkeypatch):
    monkeypatch.setattr(pe, "_last_rearm_check", float("-inf"))


@pytest.fixture
def catalog(monkeypatch):
    """A mutable stand-in for plugin-host's /api/plugins, seen by every caller."""
    live: list[dict] = []
    monkeypatch.setattr(pc, "fetch_catalog", lambda: list(live))
    monkeypatch.setattr(ap, "fetch_catalog", lambda: list(live))
    monkeypatch.setattr(ap, "fetch_permissions", lambda key: next(
        (e["permissions"] for e in live if e["key"] == key), []))
    return live


def _consent(db, catalog, entry):
    catalog[:] = [e for e in catalog if e["key"] != entry["key"]] + [entry]
    ap.consent_plugin(entry["key"], request=None, user=OWNER, db=db)


def _reconsent(db, key):
    """Re-consent the way the dialog does: pinned to the drift row shown."""
    row = next(d for d in ap.plugin_consent_drift(None, OWNER, db)["drifted"] if d["key"] == key)
    ap.consent_plugin(key, request=None, user=OWNER, db=db,
                      body=ap.ConsentGrant(fingerprint=row["fingerprint"]))


def _pending(db):
    return db.execute(select(AIAction).where(
        AIAction.action_type == "plugin_consent_drift",
        AIAction.status == "pending")).scalars().all()


def test_drift_lists_exactly_the_plugins_whose_automation_is_paused(db, catalog):
    _consent(db, catalog, _entry("hooks", ["events"], events=["invoice.paid"]))
    _consent(db, catalog, _entry("ticker", ["schedules"], schedules=["poll"]))
    _consent(db, catalog, _entry("viewer", ["browser"]))
    _consent(db, catalog, _entry("steady", ["events"], events=["job.created"]))
    _consent(db, catalog, _entry("gone", ["events"], events=["job.created"]))
    assert ap.plugin_consent_drift(None, OWNER, db)["drifted"] == []

    # Every one of the first four upgrades; "gone" is unloaded.
    catalog[:] = [
        _entry("hooks", ["events"], events=["invoice.paid", "invoice.voided"]),
        _entry("ticker", ["schedules"], schedules=["poll", "sweep"]),
        # Fingerprint moves, but nothing automatic was ever consented: nothing
        # is paused, so the banner must not say it is.
        _entry("viewer", ["browser"], services=["sidecar"]),
        _entry("steady", ["events"], events=["job.created"]),
    ]
    out = ap.plugin_consent_drift(None, OWNER, db)
    assert out["catalog_reachable"] is True
    got = {d["key"]: d["paused"] for d in out["drifted"]}
    assert got == {"hooks": ["events"], "ticker": ["schedules"]}
    hooks = next(d for d in out["drifted"] if d["key"] == "hooks")
    assert hooks["name"] == "Hooks"
    assert hooks["consented_by"] == "owner-1"


def test_banner_and_dispatcher_agree_on_who_is_drifted(db, catalog):
    """The banner must not invent drift the dispatchers would not act on, nor
    miss drift they do act on: compare against event_recipients itself."""
    _consent(db, catalog, _entry("hooks", ["events"], events=["invoice.paid"]))
    catalog[:] = [_entry("hooks", ["events"], events=["invoice.paid", "x.y"])]
    _, drifted = pc.event_recipients(db, "invoice.paid")
    assert drifted == ["hooks"]
    assert [d["key"] for d in ap.plugin_consent_drift(None, OWNER, db)["drifted"]] == ["hooks"]


def test_unreachable_plugin_host_is_not_reported_as_no_drift(db, catalog):
    catalog.clear()
    assert ap.plugin_consent_drift(None, OWNER, db) == {"catalog_reachable": False, "drifted": []}


def test_reconsent_clears_the_banner_resolves_the_alarm_and_rearms_it(db, catalog):
    _consent(db, catalog, _entry("hooks", ["events"], events=["invoice.paid"]))
    catalog[:] = [_entry("hooks", ["events"], events=["invoice.paid", "x.y"])]
    _signal_consent_drift(db, ["hooks"], "invoice.paid")
    assert len(_pending(db)) == 1

    _reconsent(db, "hooks")

    assert ap.plugin_consent_drift(None, OWNER, db)["drifted"] == []
    assert _pending(db) == []
    rows = db.execute(select(AuditLog).where(
        AuditLog.action == "plugin.consent_drift_resolved")).scalars().all()
    assert len(rows) == 1 and rows[0].user_id == "owner-1"
    assert rows[0].details["key"] == "hooks"
    assert [s["plugins"] for s in rows[0].details["signals"]] == [["hooks"]]

    # Re-armed: the next drift raises a fresh alarm instead of being swallowed
    # by the throttle on a row nobody could clear.
    catalog[:] = [_entry("hooks", ["events"], events=["invoice.paid", "x.y", "z.w"])]
    _signal_consent_drift(db, ["hooks"], "invoice.paid")
    assert len(_pending(db)) == 1


def test_alarm_stays_pending_while_another_plugin_is_still_drifted(db, catalog):
    _consent(db, catalog, _entry("hooks", ["events"], events=["a.b"]))
    _consent(db, catalog, _entry("ticker", ["schedules"], schedules=["poll"]))
    catalog[:] = [_entry("hooks", ["events"], events=["a.b", "c.d"]),
                  _entry("ticker", ["schedules"], schedules=["poll", "sweep"])]
    _signal_consent_drift(db, ["hooks", "ticker"], "a.b")

    _reconsent(db, "hooks")

    assert [d["key"] for d in ap.plugin_consent_drift(None, OWNER, db)["drifted"]] == ["ticker"]
    assert len(_pending(db)) == 1


def test_an_upgrade_dropping_every_permission_can_be_reconsented(db, catalog):
    """The upgrade retires the plugin's automation. Refusing that grant (400,
    "declares no permissions") left the drift, the banner and the alarm row
    unclearable, and the pending row muted every other plugin's drift alarm."""
    _consent(db, catalog, _entry("hooks", ["events"], events=["job.created"]))
    _consent(db, catalog, _entry("zap", ["events"], events=["job.created"]))
    catalog[:] = [_entry("hooks", [], events=[]),
                  _entry("zap", ["events"], events=["job.created"])]
    _signal_consent_drift(db, ["hooks"], "job.created")

    _reconsent(db, "hooks")

    assert ap.plugin_consent_drift(None, OWNER, db)["drifted"] == []
    assert _pending(db) == []
    catalog[:] = [_entry("hooks", [], events=[]),
                  _entry("zap", ["events"], events=["job.created", "invoice.paid"])]
    _signal_consent_drift(db, ["zap"], "job.created")
    assert [r.payload["plugins"] for r in _pending(db)] == [["zap"]]


def test_a_plugin_that_never_asked_for_anything_is_still_refused(db, catalog):
    catalog[:] = [_entry("quiet", [])]
    with pytest.raises(ap.HTTPException) as ei:
        ap.consent_plugin("quiet", request=None, user=OWNER, db=db)
    assert ei.value.status_code == 400


def test_empty_catalog_leaves_the_alarm_alone(db, catalog):
    _signal_consent_drift(db, ["hooks"], "a.b")
    assert pc.resolve_drift_signals(db, []) == []
    assert len(_pending(db)) == 1


def _drift_hooks_then(db, catalog, monkeypatch, after, ready):
    """hooks drifts and raises the alarm; then the catalog becomes ``after``
    and plugin-host's /ready answers ``ready``. No grant from here on."""
    monkeypatch.setattr(pc, "_host_fully_ready", lambda: ready)
    _consent(db, catalog, _entry("hooks", ["events"], events=["a.b"]))
    _consent(db, catalog, _entry("other", ["events"], events=["x.y"]))
    catalog[:] = [_entry("hooks", ["events"], events=["a.b", "c.d"]),
                  _entry("other", ["events"], events=["x.y"])]
    _signal_consent_drift(db, ["hooks"], "a.b")
    assert len(_pending(db)) == 1
    catalog[:] = after + [_entry("other", ["events"], events=["x.y", "z.w"])]


def test_the_drift_read_writes_nothing(db, catalog, monkeypatch):
    """A page load or prefetch must not close alarms (second audit, 5)."""
    _drift_hooks_then(db, catalog, monkeypatch, [], ready=True)
    assert [d["key"] for d in ap.plugin_consent_drift(None, OWNER, db)["drifted"]] == ["other"]
    assert len(_pending(db)) == 1
    assert db.execute(select(AuditLog).where(
        AuditLog.action == "plugin.consent_drift_resolved")).first() is None


@pytest.mark.parametrize("after", ["uninstall", "rollback"])
def test_drift_ended_without_reconsent_rearms_on_the_next_drift(db, catalog, monkeypatch, after):
    """Uninstalling or rolling back a drifted plugin ends its drift; its row
    must not mute every later drift (first audit, 1). The next drift closes it
    and raises its own alarm."""
    gone = [] if after == "uninstall" else [_entry("hooks", ["events"], events=["a.b"])]
    _drift_hooks_then(db, catalog, monkeypatch, gone, ready=True)

    _signal_consent_drift(db, ["other"], "x.y")

    rows = _pending(db)
    assert [r.payload["plugins"] for r in rows] == [["other"]]


def test_a_withheld_plugin_is_not_taken_for_uninstalled(db, catalog, monkeypatch):
    """plugin-host also drops a stale or unloadable plugin from the catalog;
    /ready 503 says so, and the alarm must stay (second audit, 1)."""
    _drift_hooks_then(db, catalog, monkeypatch, [], ready=False)

    _signal_consent_drift(db, ["other"], "x.y")

    assert [r.payload["plugins"] for r in _pending(db)] == [["hooks"]]


def test_drift_rows_say_what_reconsent_would_approve(db, catalog):
    _consent(db, catalog, _entry("hooks", ["events", "schedules"],
                                 events=["a.b", "gone.x"], schedules=["poll"]))
    catalog[:] = [_entry("hooks", ["events", "schedules"],
                         events=["a.b", "c.d"], schedules=["poll", "sweep"])]
    (row,) = ap.plugin_consent_drift(None, OWNER, db)["drifted"]
    assert row["paused"] == ["events", "schedules"]
    assert row["events_added"] == ["c.d"]
    assert row["events_removed"] == ["gone.x"]
    assert row["schedules"] == ["poll", "sweep"]


def test_a_services_only_drift_lists_the_services(db, catalog):
    _consent(db, catalog, _entry("hooks", ["events"], events=["a.b"]))
    catalog[:] = [_entry("hooks", ["events"], events=["a.b"], services=["sidecar"])]
    (row,) = ap.plugin_consent_drift(None, OWNER, db)["drifted"]
    assert (row["events_added"], row["events_removed"], row["services"]) == ([], [], ["sidecar"])


def test_a_grant_pinned_to_a_shown_surface_is_refused_once_it_moves(db, catalog):
    """The dialog shows a fingerprint; if plugin-host moves on before the click,
    nothing is granted (third audit, 1)."""
    _consent(db, catalog, _entry("hooks", ["events"], events=["a.b"]))
    catalog[:] = [_entry("hooks", ["events"], events=["a.b", "c.d"])]
    shown = ap.plugin_consent_drift(None, OWNER, db)["drifted"][0]["fingerprint"]
    catalog[:] = [_entry("hooks", ["events"], events=["a.b", "c.d", "e.f"])]

    with pytest.raises(ap.HTTPException) as e:
        ap.consent_plugin("hooks", request=None, user=OWNER, db=db,
                          body=ap.ConsentGrant(fingerprint=shown))
    assert e.value.status_code == 409
    assert [d["key"] for d in ap.plugin_consent_drift(None, OWNER, db)["drifted"]] == ["hooks"]

    fresh = ap.plugin_consent_drift(None, OWNER, db)["drifted"][0]["fingerprint"]
    ap.consent_plugin("hooks", request=None, user=OWNER, db=db,
                      body=ap.ConsentGrant(fingerprint=fresh))
    assert ap.plugin_consent_drift(None, OWNER, db)["drifted"] == []


def test_the_pinned_grant_records_the_surface_it_checked(db, catalog, monkeypatch):
    """The check and the record must read the same catalog: a restart between
    two fetches must not pin a surface the owner never saw (fourth audit, 2)."""
    _consent(db, catalog, _entry("hooks", ["events"], events=["a.b"]))
    shown = _entry("hooks", ["events"], events=["a.b", "c.d"])
    catalog[:] = [shown]
    fp = ap.plugin_consent_drift(None, OWNER, db)["drifted"][0]["fingerprint"]
    moved = _entry("hooks", ["events"], events=["a.b", "c.d", "evil.e"])
    monkeypatch.setattr(pc, "fetch_catalog", lambda: [moved])  # record_consent's own fetch

    ap.consent_plugin("hooks", request=None, user=OWNER, db=db,
                      body=ap.ConsentGrant(fingerprint=fp))

    stored = db.execute(pc.text(
        "SELECT declared_events FROM plugin_consent WHERE plugin_key = 'hooks'")).scalar()
    assert pc.json.loads(stored) == ["a.b", "c.d"]


def test_an_unpinned_grant_cannot_approve_a_drifted_plugin(db, catalog):
    """Without a pin the grant would approve the new surface unseen and clear
    the drift: refused, nothing stored (seventh audit, 1)."""
    _consent(db, catalog, _entry("hooks", ["events"], events=["a.b"]))
    catalog[:] = [_entry("hooks", ["events"], events=["a.b", "evil.e"])]

    with pytest.raises(ap.HTTPException) as e:
        ap.consent_plugin("hooks", request=None, user=OWNER, db=db)
    assert e.value.status_code == 409
    assert [d["key"] for d in ap.plugin_consent_drift(None, OWNER, db)["drifted"]] == ["hooks"]
    stored = db.execute(pc.text(
        "SELECT declared_events FROM plugin_consent WHERE plugin_key = 'hooks'")).scalar()
    assert pc.json.loads(stored) == ["a.b"]


def test_a_grant_reads_one_catalog_snapshot(db, catalog, monkeypatch):
    """The drift check and the recorded surface come from one fetch: an
    unanswered fetch refuses the grant instead of skipping the check while a
    second fetch records the drifted surface (eighth audit, 1)."""
    _consent(db, catalog, _entry("hooks", ["events"], events=["a.b"]))
    catalog[:] = [_entry("hooks", ["events"], events=["a.b", "evil.e"])]
    monkeypatch.setattr(ap, "fetch_catalog", lambda: [])  # pc.fetch_catalog still answers

    with pytest.raises(ap.HTTPException) as e:
        ap.consent_plugin("hooks", request=None, user=OWNER, db=db)
    assert e.value.status_code == 503
    stored = db.execute(pc.text(
        "SELECT declared_events FROM plugin_consent WHERE plugin_key = 'hooks'")).scalar()
    assert pc.json.loads(stored) == ["a.b"]


def test_a_pinned_grant_is_refused_when_a_permission_was_added(db, catalog):
    """Permissions are part of the pin: an upgrade adding 'browser' after the
    dialog was shown must not ride the grant through (fifth audit, 1)."""
    _consent(db, catalog, _entry("hooks", ["events"], events=["a.b"]))
    catalog[:] = [_entry("hooks", ["events"], events=["a.b", "c.d"])]
    shown = ap.plugin_consent_drift(None, OWNER, db)["drifted"][0]["fingerprint"]
    catalog[:] = [_entry("hooks", ["events", "browser"], events=["a.b", "c.d"])]

    with pytest.raises(ap.HTTPException) as e:
        ap.consent_plugin("hooks", request=None, user=OWNER, db=db,
                          body=ap.ConsentGrant(fingerprint=shown))
    assert e.value.status_code == 409
    assert "browser" not in pc.consented_permissions(db, "hooks")


def test_the_rearm_check_is_throttled(db, catalog, monkeypatch):
    """A still-drifted alarm must not cost an HTTP catalog fetch on every
    event of another drifted plugin (third audit, 2)."""
    _consent(db, catalog, _entry("hooks", ["events"], events=["a.b"]))
    catalog[:] = [_entry("hooks", ["events"], events=["a.b", "c.d"])]
    _signal_consent_drift(db, ["hooks"], "a.b")
    calls = []
    monkeypatch.setattr(pc, "fetch_catalog", lambda: calls.append(1) or list(catalog))
    for _ in range(50):
        _signal_consent_drift(db, ["other"], "x.y")
    assert len(calls) == 1
    assert [r.payload["plugins"] for r in _pending(db)] == [["hooks"]]


def test_an_error_object_from_plugin_host_is_not_a_catalog(monkeypatch):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"detail": "nope"}

    monkeypatch.setattr(pc.httpx, "get", lambda *a, **k: _Resp())
    assert pc.fetch_catalog() == []


def test_drift_route_is_owner_only():
    with pytest.raises(ap.HTTPException) as e:
        ap._require_owner({"sub": "u", "role": "admin"})
    assert e.value.status_code == 403
    route = next(r for r in ap.router.routes if r.path.endswith("/consent-drift"))
    deps = {d.call for d in route.dependant.dependencies}
    assert ap._require_owner in deps
