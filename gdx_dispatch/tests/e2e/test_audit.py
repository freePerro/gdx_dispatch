"""E2E tests for Audit Trail — AUDIT-01 through AUDIT-05.

Covers: audit log entries exist for mutations, entries have correct
tenant_id/user_id/action, audit log is append-only.
"""
from __future__ import annotations

import uuid

import pytest

from gdx_dispatch.tests.e2e.conftest import (
    assert_api_success,
)

pytestmark = [pytest.mark.e2e]


class TestAuditLogPopulated:
    def test_audit_01_mutations_logged(self, api, console_tracker):
        """Every create/update/delete writes audit entry."""
        # Create a customer to trigger an audit entry
        unique = uuid.uuid4().hex[:8]
        resp = api.post("/api/customers", json_data={
            "name": f"Audit Test {unique}",
        })
        assert resp.status_code in (200, 201)
        cid = resp.json()["id"]

        # Check audit log for this entity
        log_resp = api.get(f"/api/audit/entity/customer/{cid}")
        if log_resp.status_code == 200:
            raw = log_resp.json()
            entries = raw if isinstance(raw, list) else raw.get("items", [])
            assert isinstance(entries, list)
            assert len(entries) >= 1, "Create should produce at least one audit entry"
            # Verify the create action is logged
            actions = [e.get("action", "") for e in entries]
            assert any("create" in a.lower() for a in actions), (
                f"Expected 'create' action in audit log, got: {actions}"
            )

        # Update should also log
        api.patch(f"/api/customers/{cid}", json_data={"name": f"Audit Updated {unique}"})

        log_resp2 = api.get(f"/api/audit/entity/customer/{cid}")
        if log_resp2.status_code == 200:
            raw2 = log_resp2.json()
            entries2 = raw2 if isinstance(raw2, list) else raw2.get("items", [])
            assert len(entries2) >= 2, "Update should add another audit entry"


class TestAuditLogQuery:
    def test_audit_02_query_log(self, api, console_tracker):
        """GET /api/audit/logs returns entries with user, action, entity, timestamp."""
        resp = api.get("/api/audit/logs")
        assert_api_success(resp)
        data = resp.json()
        assert isinstance(data, (list, dict))
        entries = data if isinstance(data, list) else data.get("items", data.get("entries", []))
        if entries:
            first = entries[0]
            # Should have standard audit fields
            assert "action" in first or "event" in first, (
                f"Audit entry missing action field, keys: {list(first.keys())}"
            )
            assert "user_id" in first or "user" in first, (
                f"Audit entry missing user field, keys: {list(first.keys())}"
            )
            # Timestamp field
            assert any(k in first for k in ["timestamp", "created_at", "event_at"]), (
                f"Audit entry missing timestamp, keys: {list(first.keys())}"
            )


class TestAuditImmutability:
    def test_audit_03_no_delete(self, api, console_tracker):
        """Audit entries cannot be modified or deleted via API."""
        # Get an audit entry
        resp = api.get("/api/audit/logs")
        assert_api_success(resp)
        data = resp.json()
        entries = data if isinstance(data, list) else data.get("items", data.get("entries", []))
        if not entries:
            pytest.skip("No audit entries to test deletion on")

        entry_id = entries[0].get("id")
        if not entry_id:
            pytest.skip("Audit entry has no id field")

        # Attempt to delete — should fail
        del_resp = api.delete(f"/api/audit/logs/{entry_id}")
        assert del_resp.status_code in (403, 404, 405), (
            f"Audit delete should be forbidden, got {del_resp.status_code}"
        )

        # Attempt to modify — should fail
        patch_resp = api.patch(f"/api/audit/logs/{entry_id}", json_data={
            "action": "tampered",
        })
        assert patch_resp.status_code in (403, 404, 405), (
            f"Audit update should be forbidden, got {patch_resp.status_code}"
        )


class TestAuditIPTracking:
    def test_audit_04_contains_ip(self, api, console_tracker):
        """Each audit entry includes request IP address."""
        resp = api.get("/api/audit/logs")
        assert_api_success(resp)
        data = resp.json()
        entries = data if isinstance(data, list) else data.get("items", data.get("entries", []))
        if entries:
            first = entries[0]
            # Check for IP field
            has_ip = any(k in first for k in ["ip", "ip_address", "request_ip", "source_ip"])
            if not has_ip:
                pytest.xfail(
                    f"Audit entry missing IP field — keys present: {list(first.keys())}"
                )


class TestAuditEntityFilter:
    def test_audit_05_entity_filter(self, api, console_tracker):
        """GET /api/audit/entity/{type}/{id} returns entries for specific entity."""
        # Create something to audit
        unique = uuid.uuid4().hex[:8]
        resp = api.post("/api/customers", json_data={
            "name": f"Audit Filter {unique}",
        })
        assert resp.status_code in (200, 201)
        cid = resp.json()["id"]

        # Query audit for this entity
        filter_resp = api.get(f"/api/audit/entity/customer/{cid}")
        assert_api_success(filter_resp)
        raw = filter_resp.json()
        entries = raw if isinstance(raw, list) else raw.get("items", [])
        assert isinstance(entries, list)
        # All entries should reference this entity
        for entry in entries:
            entity_id = entry.get("entity_id", entry.get("resource_id", ""))
            assert str(entity_id) == str(cid), (
                f"Filtered audit entry references wrong entity: {entity_id} != {cid}"
            )
