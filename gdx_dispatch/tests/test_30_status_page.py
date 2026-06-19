"""gdx_dispatch/tests/test_30_status_page.py — Unit tests for the public status page
and incident management feature.

All Redis calls are patched out so these tests run without a live Redis
instance.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from gdx_dispatch.core.status_page import (
    SERVICE_COMPONENTS,
    StatusLevel,
    create_incident,
    get_incidents,
    get_service_status,
    get_uptime_stats,
    resolve_incident,
    update_incident,
    update_service_status,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_redis(store: dict | None = None) -> MagicMock:
    """Return a MagicMock that behaves like a tiny in-memory Redis client."""
    if store is None:
        store = {}
    r = MagicMock()
    r.get.side_effect = lambda key: store.get(key)
    r.set.side_effect = lambda key, val: store.update({key: val})
    r.lrange.return_value = []
    return r


# ---------------------------------------------------------------------------
# 1. SERVICE_COMPONENTS contains all required services
# ---------------------------------------------------------------------------

class TestServiceComponents:
    def test_all_required_services_present(self):
        required = {"API", "Database", "Email", "SMS", "QuickBooks Sync", "Payments", "GPS Tracking"}
        assert required.issubset(set(SERVICE_COMPONENTS)), (
            f"Missing from SERVICE_COMPONENTS: {required - set(SERVICE_COMPONENTS)}"
        )

    def test_status_level_values(self):
        assert StatusLevel.OPERATIONAL.value == "operational"
        assert StatusLevel.DEGRADED.value == "degraded"
        assert StatusLevel.OUTAGE.value == "outage"


# ---------------------------------------------------------------------------
# 2. get_service_status — returns all services defaulting to operational
# ---------------------------------------------------------------------------

class TestGetServiceStatus:
    def test_all_services_returned(self):
        store: dict = {}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            result = get_service_status()
        assert len(result) == len(SERVICE_COMPONENTS)
        names = {s["name"] for s in result}
        assert names == set(SERVICE_COMPONENTS)

    def test_defaults_to_operational(self):
        store: dict = {}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            result = get_service_status()
        for svc in result:
            assert svc["status"] == StatusLevel.OPERATIONAL.value

    def test_reflects_stored_status(self):
        store = {"status:services": json.dumps({"API": "degraded"})}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            result = get_service_status()
        api_entry = next(s for s in result if s["name"] == "API")
        assert api_entry["status"] == "degraded"


# ---------------------------------------------------------------------------
# 3. create_incident — validates inputs, persists, returns id
# ---------------------------------------------------------------------------

class TestCreateIncident:
    def test_create_returns_id(self):
        store: dict = {}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            inc_id = create_incident(
                title="API Slowdown",
                severity="major",
                affected_services=["API"],
            )
        assert isinstance(inc_id, str) and len(inc_id) > 0

    def test_invalid_severity_raises(self):
        with pytest.raises(ValueError, match="Invalid severity"):
            create_incident(title="X", severity="catastrophic", affected_services=[])

    def test_unknown_service_raises(self):
        with pytest.raises(ValueError, match="Unknown service"):
            create_incident(
                title="X",
                severity="minor",
                affected_services=["NonExistentService"],
            )

    def test_affected_services_stored(self):
        store: dict = {}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            inc_id = create_incident(
                title="DB Outage",
                severity="critical",
                affected_services=["Database", "API"],
            )
            raw = store.get("status:incidents")
        assert raw is not None
        incidents = json.loads(raw)
        match = next((i for i in incidents if i["id"] == inc_id), None)
        assert match is not None
        assert set(match["affected_services"]) == {"Database", "API"}

    def test_updates_list_initialised(self):
        store: dict = {}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            inc_id = create_incident(
                title="SMS Delay",
                severity="minor",
                affected_services=["SMS"],
            )
            raw = store.get("status:incidents")
        incidents = json.loads(raw)
        match = next(i for i in incidents if i["id"] == inc_id)
        assert match["updates"] == []


# ---------------------------------------------------------------------------
# 4. update_incident — appends timeline entry
# ---------------------------------------------------------------------------

class TestUpdateIncident:
    def _seed_incident(self, store: dict) -> str:
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            return create_incident("Test", "minor", ["SMS"])

    def test_update_appends_entry(self):
        store: dict = {}
        inc_id = self._seed_incident(store)
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            ok = update_incident(inc_id, status="identified", message="Root cause found.")
        assert ok is True
        incidents = json.loads(store["status:incidents"])
        match = next(i for i in incidents if i["id"] == inc_id)
        assert len(match["updates"]) == 1
        entry = match["updates"][0]
        assert entry["status"] == "identified"
        assert entry["message"] == "Root cause found."
        assert "timestamp" in entry

    def test_update_nonexistent_returns_false(self):
        store: dict = {"status:incidents": json.dumps([])}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            ok = update_incident("deadbeef", status="investigating", message="Checking.")
        assert ok is False

    def test_multiple_updates_accumulate(self):
        store: dict = {}
        inc_id = self._seed_incident(store)
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            update_incident(inc_id, "investigating", "Looking into it.")
            update_incident(inc_id, "identified", "Found the issue.")
        incidents = json.loads(store["status:incidents"])
        match = next(i for i in incidents if i["id"] == inc_id)
        assert len(match["updates"]) == 2


# ---------------------------------------------------------------------------
# 5. resolve_incident — marks resolved with timestamp
# ---------------------------------------------------------------------------

class TestResolveIncident:
    def test_resolve_sets_status(self):
        store: dict = {}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            inc_id = create_incident("API Down", "critical", ["API"])
            ok = resolve_incident(inc_id, resolution="Rolled back bad deploy.")
        assert ok is True
        incidents = json.loads(store["status:incidents"])
        match = next(i for i in incidents if i["id"] == inc_id)
        assert match["status"] == "resolved"
        assert match["resolution"] == "Rolled back bad deploy."
        assert match["resolved_at"] is not None

    def test_resolve_nonexistent_returns_false(self):
        store: dict = {"status:incidents": json.dumps([])}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            ok = resolve_incident("nosuchid", "Fixed.")
        assert ok is False


# ---------------------------------------------------------------------------
# 6. get_incidents — capped list
# ---------------------------------------------------------------------------

class TestGetIncidents:
    def _seed_n(self, store: dict, n: int) -> list[str]:
        ids = []
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            for i in range(n):
                ids.append(create_incident(f"Incident {i}", "minor", ["Email"]))
        return ids

    def test_returns_up_to_limit(self):
        store: dict = {}
        self._seed_n(store, 10)
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            result = get_incidents(limit=5)
        assert len(result) <= 5

    def test_returns_all_when_fewer_than_limit(self):
        store: dict = {}
        self._seed_n(store, 3)
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            result = get_incidents(limit=20)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# 7. get_uptime_stats — per-service stats
# ---------------------------------------------------------------------------

class TestGetUptimeStats:
    def test_returns_correct_shape(self):
        store: dict = {}
        r = _make_redis(store)
        r.lrange.return_value = []
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            result = get_uptime_stats("API", days=30)
        assert result["service"] == "API"
        assert isinstance(result["uptime_pct"], float)
        assert result["days"] == 30

    def test_defaults_to_100_when_no_data(self):
        store: dict = {}
        r = _make_redis(store)
        r.lrange.return_value = []
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            result = get_uptime_stats("Payments", days=7)
        assert result["uptime_pct"] == 100.0

    def test_reads_dedicated_key_when_present(self):
        store = {
            "status:uptime:Database": json.dumps({"uptime_pct": 99.75}),
        }
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            result = get_uptime_stats("Database", days=90)
        assert result["uptime_pct"] == 99.75

    def test_unknown_service_raises(self):
        with pytest.raises(ValueError, match="Unknown service"):
            get_uptime_stats("FakeService", days=30)


# ---------------------------------------------------------------------------
# 8. update_service_status — validation and persistence
# ---------------------------------------------------------------------------

class TestUpdateServiceStatus:
    def test_valid_update_persists(self):
        store: dict = {}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            update_service_status("API", "degraded")
        stored = json.loads(store["status:services"])
        assert stored["API"] == "degraded"

    def test_invalid_service_raises(self):
        with pytest.raises(ValueError, match="Unknown service"):
            update_service_status("NonExistent", "operational")

    def test_invalid_status_raises(self):
        with pytest.raises(ValueError, match="Invalid status"):
            update_service_status("API", "fire")


# ---------------------------------------------------------------------------
# 9. Admin router endpoints — fast FastAPI TestClient smoke tests
# ---------------------------------------------------------------------------

class TestAdminRouterEndpoints:
    """Smoke-test the admin router using FastAPI's TestClient with Redis mocked."""

    @pytest.fixture()
    def client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from gdx_dispatch.core.status_page import admin_router
        app = FastAPI()
        app.include_router(admin_router)
        return TestClient(app, raise_server_exceptions=False)

    def test_create_incident_endpoint(self, client):
        store: dict = {}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            resp = client.post(
                "/api/admin/incidents",
                json={"title": "Payments Down", "severity": "critical", "affected_services": ["Payments"]},
            )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert data["created"] is True

    def test_create_incident_invalid_severity(self, client):
        store: dict = {}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            resp = client.post(
                "/api/admin/incidents",
                json={"title": "X", "severity": "catastrophic", "affected_services": []},
            )
        # Pydantic rejects the pattern mismatch with 422
        assert resp.status_code == 422

    def test_update_incident_endpoint(self, client):
        store: dict = {}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            create_resp = client.post(
                "/api/admin/incidents",
                json={"title": "GPS Lag", "severity": "minor", "affected_services": ["GPS Tracking"]},
            )
            inc_id = create_resp.json()["id"]
            upd_resp = client.patch(
                f"/api/admin/incidents/{inc_id}",
                json={"status": "investigating", "message": "Team is looking into it."},
            )
        assert upd_resp.status_code == 200
        assert upd_resp.json()["updated"] is True

    def test_update_nonexistent_incident_returns_404(self, client):
        store: dict = {"status:incidents": json.dumps([])}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            resp = client.patch(
                "/api/admin/incidents/doesnotexist",
                json={"status": "investigating", "message": "Checking."},
            )
        assert resp.status_code == 404

    def test_resolve_incident_endpoint(self, client):
        store: dict = {}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            create_resp = client.post(
                "/api/admin/incidents",
                json={"title": "Email Outage", "severity": "major", "affected_services": ["Email"]},
            )
            inc_id = create_resp.json()["id"]
            res_resp = client.post(
                f"/api/admin/incidents/{inc_id}/resolve",
                json={"resolution": "Fixed upstream relay."},
            )
        assert res_resp.status_code == 200
        assert res_resp.json()["resolved"] is True

    def test_resolve_nonexistent_returns_404(self, client):
        store: dict = {"status:incidents": json.dumps([])}
        r = _make_redis(store)
        with patch("gdx_dispatch.core.status_page._get_redis", return_value=r):
            resp = client.post(
                "/api/admin/incidents/nosuchid/resolve",
                json={"resolution": "N/A"},
            )
        assert resp.status_code == 404
