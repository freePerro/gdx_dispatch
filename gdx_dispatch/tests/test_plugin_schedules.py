"""GDXA-439 — plugin schedules actually run.

A plugin could declare ``schedules=((name, cron, callable),)``, the owner
consented to "runs automatically on a fixed schedule", and nothing ever read the
cron or called the callable. These tests declare a schedule, consent to it, tick
the core driver and assert the callable RAN — through the real plugin-host app,
with only the network hop between the two replaced by an in-process client.

Last, a guard: every capability a manifest can declare, and every permission an
owner can consent to, names the code that acts on it. A new one without a
dispatcher fails here instead of shipping as another consented no-op.
"""
from __future__ import annotations

import dataclasses
import importlib
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import gdx_dispatch.core.plugin_consent as pc
import gdx_dispatch.core.plugin_schedules as ps
from gdx_dispatch.plugin_api.manifest import KNOWN_PERMISSIONS, PluginManifest
from gdx_dispatch.plugin_api.schedules import PluginScheduleRun, cron_matches, parse_cron
from gdx_dispatch.plugin_host.app import create_plugin_host

# Wednesday 2026-10-14 09:30 UTC.
WED_0930 = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)

# ---------------------------------------------------------------------------
# Cron dialect
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("expr", "when", "expected"), [
    ("* * * * *", WED_0930, True),
    ("*/5 * * * *", WED_0930, True),
    ("*/7 * * * *", WED_0930, False),
    ("30 9 * * *", WED_0930, True),
    ("30 9 * * mon-fri", WED_0930, True),
    ("30 9 * * sat,sun", WED_0930, False),
    ("30 9 * oct *", WED_0930, True),
    ("0-29 * * * *", WED_0930, False),
    ("10/20 * * * *", WED_0930, True),       # 10, 30, 50
    ("30 9 * * 0", datetime(2026, 10, 18, 9, 30, tzinfo=UTC), True),  # Sunday
    ("30 9 * * 7", datetime(2026, 10, 18, 9, 30, tzinfo=UTC), True),  # 7 = Sunday
    # Vixie either-day rule: both restricted → a day matches if EITHER does.
    ("30 9 1 * wed", WED_0930, True),
    ("30 9 1 * fri", WED_0930, False),
    # "*/2" counts as unrestricted, so day-of-week alone decides.
    ("30 9 */2 * fri", WED_0930, False),
])
def test_cron_matches(expr, when, expected):
    assert cron_matches(expr, when) is expected


@pytest.mark.parametrize("bad", [
    "", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "* * 0 * *",
    "5-1 * * * *", "*/0 * * * *", "@hourly", "L * * * *", "1,,2 * * * *",
])
def test_parse_cron_refuses_outside_the_dialect(bad):
    with pytest.raises(ValueError):
        parse_cron(bad)
    assert cron_matches(bad, WED_0930) is False


def test_manifest_strips_an_unrunnable_cron_with_a_warning_never_raises(caplog):
    with caplog.at_level("WARNING"):
        m = PluginManifest(
            key="x", name="X", permissions=("schedules",),
            schedules=(("good", "*/5 * * * *", lambda: None),
                       ("bad", "every five minutes", lambda: None)),
        )
    assert [s[0] for s in m.schedules] == ["good"]
    assert "'bad'" in caplog.text and "cannot run" in caplog.text


def test_manifest_keeps_the_first_of_two_schedules_with_one_name(caplog):
    first, second = (lambda: None), (lambda: None)
    with caplog.at_level("WARNING"):
        m = PluginManifest(key="x", name="X", permissions=("schedules",),
                           schedules=(("nightly", "0 3 * * *", first),
                                      ("nightly", "0 4 * * *", second)))
    assert m.schedules == (("nightly", "0 3 * * *", first),)
    assert "declared twice" in caplog.text


def test_a_parameter_with_a_default_is_not_handed_the_run():
    got: list = []

    def poll(limit=50):
        got.append(limit)

    def tick(run=None):
        got.append(run)

    client = _host(("poll", "* * * * *", poll), ("tick", "* * * * *", tick))
    assert client.post("/internal/schedules", json=_body()).json()["status"] == "ok"
    assert got == [50]
    # ...but an optional parameter named ``run`` is the slot for it.
    client.post("/internal/schedules", json=_body(name="tick", run_id="poller:tick:x"))
    assert isinstance(got[1], PluginScheduleRun) and got[1].run_id == "poller:tick:x"


# ---------------------------------------------------------------------------
# plugin-host /internal/schedules
# ---------------------------------------------------------------------------


def _host(*schedules, key="poller"):
    m = PluginManifest(key=key, name=key, permissions=("schedules",), schedules=schedules)
    return TestClient(create_plugin_host(plugins=[m], dists={}))


def _body(name="poll", run_id="poller:poll:2026-10-14T09:30Z", key="poller"):
    return {"key": key, "name": name, "scheduled_for": "2026-10-14T09:30Z", "run_id": run_id}


def test_catalog_publishes_the_cron_beside_the_names_only_preimage():
    client = _host(("poll", "*/5 * * * *", lambda: None))
    entry = client.get("/api/plugins").json()[0]
    assert entry["schedules"] == ["poll"]  # unchanged: the consent fingerprint preimage
    assert entry["schedule_specs"] == [{"name": "poll", "cron": "*/5 * * * *"}]


def test_host_runs_the_callable_with_or_without_the_run_argument():
    got: list = []
    calls: list = []
    client = _host(("poll", "* * * * *", lambda run: got.append(run)),
                   ("ping", "* * * * *", lambda: calls.append(1)))
    r = client.post("/internal/schedules", json=_body())
    assert r.json() == {"status": "ok", "run_id": "poller:poll:2026-10-14T09:30Z"}
    assert isinstance(got[0], PluginScheduleRun) and got[0].name == "poll"
    r = client.post("/internal/schedules", json=_body(name="ping", run_id="poller:ping:x"))
    assert r.json()["status"] == "ok" and calls == [1]


def test_host_drops_a_repeated_run_id_and_reports_unknown_and_failed():
    got: list = []

    def boom():
        raise RuntimeError("upstream down")

    client = _host(("poll", "* * * * *", lambda: got.append(1)),
                   ("boom", "* * * * *", boom))
    assert client.post("/internal/schedules", json=_body()).json()["status"] == "ok"
    assert client.post("/internal/schedules", json=_body()).json()["status"] == "duplicate"
    assert got == [1]  # ran once
    assert client.post("/internal/schedules",
                       json=_body(name="nope", run_id="r2")).json()["status"] == "unknown"
    r = client.post("/internal/schedules", json=_body(name="boom", run_id="r3")).json()
    assert r["status"] == "failed" and "upstream down" in r["error"]
    assert client.post("/internal/schedules", json={"key": "poller"}).status_code == 422


def test_schedule_route_is_behind_the_internal_token_gate(monkeypatch):
    monkeypatch.delenv("GDX_INTERNAL_TOKEN", raising=False)
    monkeypatch.setenv("GDX_ENV", "production")
    client = _host(("poll", "* * * * *", lambda: None))
    assert client.post("/internal/schedules", json=_body()).status_code == 401


# ---------------------------------------------------------------------------
# End to end: core driver → plugin-host → callable → audit row
# ---------------------------------------------------------------------------


def _db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)()


class _Wire:
    """Stands in for the httpx module: sends core's calls to the host app."""

    def __init__(self, client: TestClient):
        self.client = client
        self.posts: list[dict] = []

    def get(self, url, timeout=None):
        return self.client.get("/" + url.split("/", 3)[3])

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(json)
        return self.client.post("/" + url.split("/", 3)[3], json=json, headers=headers)


def _consent(db, client, key="poller", perms=("schedules",)):
    catalog = client.get("/api/plugins").json()
    with patch.object(pc, "fetch_catalog", return_value=catalog):
        pc.record_consent(db, key, list(perms), by="owner-1")


def _audit_rows(db):
    from gdx_dispatch.core.audit import ensure_audit_table

    ensure_audit_table(db)  # "no rows" must not read as "no table"
    return db.execute(text(
        "SELECT action, entity_id, details FROM audit_logs "
        "WHERE action = 'plugin.schedule_run'"
    )).all()


def test_a_declared_and_consented_schedule_actually_runs_and_is_audited():
    ran: list[PluginScheduleRun] = []
    client = _host(("poll", "*/5 * * * *", lambda run: ran.append(run)),
                   ("nightly", "0 3 * * *", lambda run: ran.append(run)))
    db = _db()
    _consent(db, client)
    wire = _Wire(client)
    with patch.object(ps, "httpx", wire):
        out = ps.dispatch_due_schedules(db, now=WED_0930.replace(second=41))

    # The callable ran — once, for the due schedule only, at the tick's minute.
    assert [r.name for r in ran] == ["poll"]
    assert ran[0].run_id == "poller:poll:2026-10-14T09:30Z"
    assert out["results"] == {"poller:poll:2026-10-14T09:30Z": "ok"}
    rows = _audit_rows(db)
    assert len(rows) == 1 and rows[0][1] == "poller"
    details = rows[0][2]
    assert "owner-1" in str(details) and "'ok'" in str(details).replace('"', "'")

    # A retried tick for the same minute is not a second run.
    with patch.object(ps, "httpx", wire):
        again = ps.dispatch_due_schedules(db, now=WED_0930)
    assert len(ran) == 1
    assert again["results"] == {"poller:poll:2026-10-14T09:30Z": "duplicate"}
    assert len(_audit_rows(db)) == 1  # already audited → not audited twice


def test_no_consent_no_run():
    ran: list = []
    client = _host(("poll", "* * * * *", lambda: ran.append(1)))
    db = _db()
    pc.ensure_consent_table(db)
    wire = _Wire(client)
    with patch.object(ps, "httpx", wire):
        out = ps.dispatch_due_schedules(db, now=WED_0930)
    assert ran == [] and wire.posts == [] and out["due"] == 0


def test_consent_without_the_schedules_permission_does_not_run():
    ran: list = []
    m = PluginManifest(key="poller", name="poller", permissions=("schedules", "events"),
                       events=("job.*",), event_handler=lambda e: None,
                       schedules=(("poll", "* * * * *", lambda: ran.append(1)),))
    client = TestClient(create_plugin_host(plugins=[m], dists={}))
    db = _db()
    _consent(db, client, perms=("events",))
    with patch.object(ps, "httpx", _Wire(client)):
        ps.dispatch_due_schedules(db, now=WED_0930)
    assert ran == []


def test_a_drifted_plugin_fails_closed_and_signals(caplog):
    ran: list = []
    db = _db()
    _consent(db, _host(("poll", "* * * * *", lambda: None)))
    # Upgrade adds a schedule the owner never saw → fingerprint drift.
    upgraded = _host(("poll", "* * * * *", lambda: ran.append(1)),
                     ("exfil", "* * * * *", lambda: ran.append(2)))
    with (patch.object(ps, "httpx", _Wire(upgraded)),
          patch("gdx_dispatch.core.plugin_events._signal_consent_drift") as sig):
        out = ps.dispatch_due_schedules(db, now=WED_0930)
    assert ran == [] and out["drifted"] == ["poller"]
    sig.assert_called_once()
    # Each blocked due run is logged, outside the (here mocked) throttled signal.
    assert "plugin_schedule_blocked_by_consent_drift" in caplog.text
    assert "poller:poll:2026-10-14T09:30Z" in caplog.text

    # Nothing due this minute → nothing blocked → no line.
    caplog.clear()
    with (patch.object(ps, "httpx", _Wire(_host(("poll", "0 3 * * *", lambda: None),
                                                 ("exfil", "0 3 * * *", lambda: None)))),
          patch("gdx_dispatch.core.plugin_events._signal_consent_drift")):
        ps.dispatch_due_schedules(db, now=WED_0930)
    assert "plugin_schedule_blocked_by_consent_drift" not in caplog.text


def test_an_unreachable_host_is_retried_with_the_original_minute():
    class Down:
        def get(self, *a, **k):
            raise ConnectionError("refused")

        post = get

    db = _db()
    pc.ensure_consent_table(db)
    with patch.object(ps, "httpx", Down()):
        out = ps.dispatch_due_schedules(db, now=WED_0930)
    assert out["undelivered"] is None  # catalog unreadable → whole tick retries

    task = ps.dispatch_plugin_schedules_task
    with (patch.object(ps, "SessionLocal", lambda: db),
          patch.object(ps, "dispatch_due_schedules", return_value=out),
          patch.object(task, "retry", side_effect=RuntimeError("retry")) as retry,
          pytest.raises(RuntimeError, match="retry")):
        task.run()
    assert retry.call_args.kwargs["args"] == [out["minute"], None]


def test_a_run_that_times_out_but_executed_is_audited_on_the_retry():
    """The callable runs inline, so a slow one outlives core's POST timeout:
    core sees a timeout, but the run happened. The retry's ``duplicate`` must
    still produce the audit row."""
    import httpx as real_httpx

    ran: list = []
    client = _host(("poll", "* * * * *", lambda: ran.append(1)))
    db = _db()
    _consent(db, client)

    class SlowWire(_Wire):
        def post(self, url, json=None, headers=None, timeout=None):
            super().post(url, json=json, headers=headers, timeout=timeout)  # it runs...
            raise real_httpx.ReadTimeout("read timed out")  # ...core never hears

    with patch.object(ps, "httpx", SlowWire(client)):
        first = ps.dispatch_due_schedules(db, now=WED_0930)
    assert ran == [1] and first["undelivered"] == ["poller:poll:2026-10-14T09:30Z"]

    # The whole-tick retry (only=None, as after an unreadable catalog) is a
    # retry path too: what decides the audit is the trail, not the path.
    with patch.object(ps, "httpx", _Wire(client)):
        retry = ps.dispatch_due_schedules(db, now=WED_0930, only=None)
    assert ran == [1]  # not run twice
    assert retry["results"] == {"poller:poll:2026-10-14T09:30Z": "duplicate"}
    rows = _audit_rows(db)
    details = rows[0][2] if isinstance(rows[0][2], dict) else __import__("json").loads(rows[0][2])
    assert len(rows) == 1 and details["outcome"] == "ok"
    assert details["error"] is None and details["confirmed_via"] == "a later send"


def test_a_run_still_going_is_checked_back_on_for_its_final_outcome():
    """A callable that outlives the timeout AND the retry delay is still
    ``running`` when the retry arrives. That is not its outcome: the run stays
    owed, and the next send audits how it really ended."""
    import threading

    import httpx as real_httpx

    release = threading.Event()

    def slow():
        release.wait(5)
        raise RuntimeError("upstream down")

    client = _host(("poll", "* * * * *", slow))
    db = _db()
    _consent(db, client)
    first_send: list[threading.Thread] = []

    class SlowWire(_Wire):
        def post(self, url, json=None, headers=None, timeout=None):
            t = threading.Thread(target=super().post, args=(url,),
                                 kwargs={"json": json, "headers": headers})
            t.start()
            first_send.append(t)
            t.join(0.3)
            raise real_httpx.ReadTimeout("read timed out")

    run_id = "poller:poll:2026-10-14T09:30Z"
    with patch.object(ps, "httpx", SlowWire(client)):
        ps.dispatch_due_schedules(db, now=WED_0930)
    with patch.object(ps, "httpx", _Wire(client)):
        mid = ps.dispatch_due_schedules(db, now=WED_0930, only=[run_id], last_attempt=False)
    assert mid["undelivered"] == [run_id] and _audit_rows(db) == []

    release.set()
    first_send[0].join(5)
    with patch.object(ps, "httpx", _Wire(client)):
        ps.dispatch_due_schedules(db, now=WED_0930, only=[run_id])
    rows = _audit_rows(db)
    assert len(rows) == 1
    details = str(rows[0][2]).replace('"', "'")
    assert "'failed'" in details and "upstream down" in details


@pytest.mark.xfail(strict=True, reason=(
    "GDXA-439: the beat entry is platform-core's follow-up (core/scheduler.py). "
    "When it lands this XPASSes and fails: delete this marker in that change."))
def test_the_driver_is_on_the_beat_schedule():
    """GDXA-438's shape is a dispatcher nothing calls. This guard fails when the
    driver exists but beat never ticks it."""
    from gdx_dispatch.core.scheduler import build_beat_schedule

    tasks = {e.get("task") for e in build_beat_schedule().values()}
    assert "gdx_dispatch.core.plugin_events.dispatch_plugin_schedules" in tasks


def test_the_task_is_registered_and_routed_beside_event_dispatch():
    from gdx_dispatch.core.celery_app import celery_app

    name = "gdx_dispatch.core.plugin_events.dispatch_plugin_schedules"
    assert name in celery_app.tasks
    assert celery_app.amqp.router.route({}, name)["queue"].name == "priority:high"


# ---------------------------------------------------------------------------
# Guard: no consented capability without a dispatcher
# ---------------------------------------------------------------------------

#: Manifest fields that only DESCRIBE the plugin — read at load or render time.
DESCRIPTIVE_FIELDS = {
    "key", "name", "tier", "requires", "router", "migrations_path", "ui",
    "permissions", "catalog_types", "pricing_strategies",
    # Reserved by ADR-015 Slice 3 and documented as such; no consent, no claim.
    "importers",
}

#: Manifest fields that make code run on its own, and what runs them.
CAPABILITY_FIELDS = {
    "events": "gdx_dispatch.core.plugin_events:deliver_plugin_event_task",
    "event_handler": "gdx_dispatch.core.plugin_events:deliver_plugin_event_task",
    "schedules": "gdx_dispatch.core.plugin_schedules:dispatch_plugin_schedules_task",
}

#: Every consentable permission, and the code that re-checks that consent
#: before acting on it.
PERMISSION_CONSUMERS = {
    "browser": "gdx_dispatch.routers.browser_proxy:_gate_browser",
    "events": "gdx_dispatch.core.plugin_consent:event_recipients",
    "schedules": "gdx_dispatch.core.plugin_consent:schedule_runners",
    "email": "gdx_dispatch.tasks.plugin_email_outbox:_consented",
}

#: Consentable, consumed by nothing — allowed ONLY with consent text that says so.
RESERVED_PERMISSIONS = {"services"}


def _resolve(ref: str):
    mod, _, attr = ref.partition(":")
    return getattr(importlib.import_module(mod), attr)


def test_every_manifest_field_is_descriptive_or_has_a_dispatcher():
    fields = {f.name for f in dataclasses.fields(PluginManifest)}
    unclassified = fields - DESCRIPTIVE_FIELDS - set(CAPABILITY_FIELDS)
    assert not unclassified, (
        f"new PluginManifest field(s) {sorted(unclassified)}: if the plugin can "
        "make code run with it, name its dispatcher in CAPABILITY_FIELDS"
    )
    for field, ref in CAPABILITY_FIELDS.items():
        assert callable(_resolve(ref)), f"{field} has no dispatcher at {ref}"


def test_every_permission_has_a_consumer_or_says_it_is_reserved():
    from gdx_dispatch.plugin_api.manifest import PERMISSION_RISKS

    assert set(KNOWN_PERMISSIONS) == set(PERMISSION_CONSUMERS) | RESERVED_PERMISSIONS
    for perm, ref in PERMISSION_CONSUMERS.items():
        assert callable(_resolve(ref)), f"{perm} has no consumer at {ref}"
    for perm in RESERVED_PERMISSIONS:
        risk = PERMISSION_RISKS[perm].lower()
        assert "enables nothing" in risk, (
            f"{perm} runs nothing, so its consent text must not claim it does"
        )
