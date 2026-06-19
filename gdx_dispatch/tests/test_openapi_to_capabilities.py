"""Tests for gdx_dispatch.core.openapi_to_capabilities (SS-14 slice E)."""
from __future__ import annotations

from gdx_dispatch.core.openapi_to_capabilities import (
    _extract_resource,
    derive_capability_options,
)


def test_empty_spec_yields_empty_list():
    assert derive_capability_options({}) == []
    assert derive_capability_options({"paths": {}}) == []


def test_malformed_spec_degrades_gracefully():
    assert derive_capability_options({"paths": "not-a-dict"}) == []


def test_single_get_endpoint_produces_read_option():
    spec = {"paths": {"/api/jobs": {"get": {}}}}
    result = derive_capability_options(spec)
    assert result == [
        {
            "action": "read",
            "resource_type": "jobs",
            "label": "Read jobs",
            "paths": ["/api/jobs"],
        }
    ]


def test_http_method_mapping():
    spec = {
        "paths": {
            "/api/widgets": {
                "get": {},
                "post": {},
                "put": {},
                "patch": {},
                "delete": {},
                "head": {},
                "options": {},
            }
        }
    }
    result = derive_capability_options(spec)
    actions = {o["action"] for o in result}
    # head/options/get all collapse to "read"
    assert actions == {"read", "create", "update", "delete"}


def test_dedup_across_list_and_detail_paths():
    spec = {
        "paths": {
            "/api/jobs": {"get": {}},
            "/api/jobs/{id}": {"get": {}},
        }
    }
    result = derive_capability_options(spec)
    assert len(result) == 1
    assert result[0]["action"] == "read"
    assert result[0]["resource_type"] == "jobs"
    assert "/api/jobs" in result[0]["paths"]
    assert "/api/jobs/{id}" in result[0]["paths"]


def test_resource_prefixes_are_stripped():
    assert _extract_resource("/api/jobs") == "jobs"
    assert _extract_resource("/v1/customers") == "customers"
    assert _extract_resource("/public/webhooks") == "webhooks"
    assert _extract_resource("/api/v1/jobs") == "jobs"
    # All segments stripped → None
    assert _extract_resource("/api/v1/{id}") is None
    # Parameter-only first segment skipped
    assert _extract_resource("/api/{id}") is None
    # Root path has no resource
    assert _extract_resource("/") is None
    assert _extract_resource("") is None


def test_non_resource_paths_are_ignored():
    spec = {
        "paths": {
            "/": {"get": {}},
            "/api/{id}": {"get": {}},
            "/api/jobs": {"get": {}},
        }
    }
    result = derive_capability_options(spec)
    # Only the real resource survives.
    assert [o["resource_type"] for o in result] == ["jobs"]


def test_unknown_http_methods_ignored():
    spec = {
        "paths": {
            "/api/jobs": {
                "get": {},
                "trace": {},  # unknown: not mapped
                "parameters": [],  # OpenAPI shared params: not mapped
            }
        }
    }
    result = derive_capability_options(spec)
    assert len(result) == 1
    assert result[0]["action"] == "read"


def test_output_sorted_by_resource_then_action():
    spec = {
        "paths": {
            "/api/jobs": {"post": {}, "get": {}},
            "/api/customers": {"get": {}},
        }
    }
    result = derive_capability_options(spec)
    labels = [(o["resource_type"], o["action"]) for o in result]
    assert labels == [
        ("customers", "read"),
        ("jobs", "create"),
        ("jobs", "read"),
    ]


def test_paths_list_sorted_and_deduped():
    spec = {
        "paths": {
            "/api/jobs/{id}": {"get": {}},
            "/api/jobs": {"get": {}},
        }
    }
    result = derive_capability_options(spec)
    assert result[0]["paths"] == ["/api/jobs", "/api/jobs/{id}"]


def test_fastapi_live_spec_integration():
    """Smoke-check against a real FastAPI app.openapi() dict."""
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/api/jobs")
    def list_jobs():
        return []

    @app.post("/api/jobs")
    def create_job():
        return {}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return {}

    result = derive_capability_options(app.openapi())
    pairs = {(o["action"], o["resource_type"]) for o in result}
    assert ("read", "jobs") in pairs
    assert ("create", "jobs") in pairs
