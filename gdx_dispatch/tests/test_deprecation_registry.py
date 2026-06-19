"""SS-25 Slice C — tests for ``gdx_dispatch.core.deprecation_registry``."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from gdx_dispatch.core.deprecation_registry import (
    DeprecationEntry,
    DeprecationRegistry,
    get_registry,
    reset_registry_for_tests,
)


def _mk_entry(path: str = "/api/v1/customers") -> DeprecationEntry:
    return DeprecationEntry(
        endpoint=path,
        deprecated_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
        sunset_at=datetime(2028, 4, 1, tzinfo=timezone.utc),
        replacement_endpoint="/api/v2/customers",
    )


class TestDeprecationEntry:
    def test_valid(self):
        e = _mk_entry()
        assert e.endpoint == "/api/v1/customers"
        assert e.replacement_endpoint == "/api/v2/customers"

    def test_naive_timestamps_rejected(self):
        with pytest.raises(ValueError):
            DeprecationEntry(
                endpoint="/x",
                deprecated_at=datetime(2026, 1, 1),  # noqa: DTZ001
                sunset_at=datetime(2028, 1, 1, tzinfo=timezone.utc),
            )

    def test_deprecated_after_sunset_rejected(self):
        with pytest.raises(ValueError):
            DeprecationEntry(
                endpoint="/x",
                deprecated_at=datetime(2029, 1, 1, tzinfo=timezone.utc),
                sunset_at=datetime(2028, 1, 1, tzinfo=timezone.utc),
            )

    def test_to_public_dict(self):
        d = _mk_entry().to_public_dict()
        assert d["endpoint"] == "/api/v1/customers"
        assert d["replacement_endpoint"] == "/api/v2/customers"
        assert "deprecated_at" in d and "sunset_at" in d


class TestRegistry:
    def test_empty(self):
        reg = DeprecationRegistry()
        assert len(reg) == 0
        assert reg.lookup("/anything") is None
        assert "/anything" not in reg

    def test_lookup_and_contains(self):
        reg = DeprecationRegistry([_mk_entry()])
        assert "/api/v1/customers" in reg
        assert reg.lookup("/api/v1/customers").replacement_endpoint == "/api/v2/customers"
        assert reg.lookup("/api/v1/other") is None

    def test_all_entries_snapshot(self):
        reg = DeprecationRegistry([_mk_entry("/a"), _mk_entry("/b")])
        paths = {e.endpoint for e in reg.all_entries()}
        assert paths == {"/a", "/b"}


class TestFromEntries:
    def test_parses_iso_with_z(self):
        reg = DeprecationRegistry.from_entries([
            {
                "endpoint": "/api/v1/jobs",
                "deprecated_at": "2026-04-01T00:00:00Z",
                "sunset_at": "2028-04-01T00:00:00Z",
                "replacement_endpoint": "/api/v2/jobs",
            }
        ])
        entry = reg.lookup("/api/v1/jobs")
        assert entry is not None
        assert entry.deprecated_at.tzinfo is not None

    def test_missing_field_raises(self):
        with pytest.raises(ValueError):
            DeprecationRegistry.from_entries([{"endpoint": "/x"}])


class TestJsonFile:
    def test_missing_file_returns_empty(self, tmp_path: Path):
        reg = DeprecationRegistry.from_json_file(tmp_path / "nope.json")
        assert len(reg) == 0

    def test_reads_file(self, tmp_path: Path):
        p = tmp_path / "dep.json"
        p.write_text(json.dumps({
            "deprecations": [
                {
                    "endpoint": "/api/v1/x",
                    "deprecated_at": "2026-04-01T00:00:00Z",
                    "sunset_at": "2028-04-01T00:00:00Z",
                }
            ]
        }))
        reg = DeprecationRegistry.from_json_file(p)
        assert "/api/v1/x" in reg

    def test_malformed_file_raises(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("this is not json")
        with pytest.raises(json.JSONDecodeError):
            DeprecationRegistry.from_json_file(p)


class TestSingleton:
    def test_singleton_roundtrip(self):
        reset_registry_for_tests()
        reg1 = get_registry()
        reg2 = get_registry()
        assert reg1 is reg2
        reset_registry_for_tests()
