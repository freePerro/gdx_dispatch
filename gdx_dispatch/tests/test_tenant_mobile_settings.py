"""Round-trip tests for gdx_dispatch/core/tenant_mobile_settings.py.

Sprint tech_mobile S1-Z3 — verifies the catalog-merged-with-overrides
read path and the per-request cache.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from gdx_dispatch.core.feature_defaults import (
    TECH_MOBILE_SETTINGS,
    tech_mobile_default,
)
from gdx_dispatch.core.tenant_mobile_settings import (
    get_tenant_mobile_setting,
    load_tenant_mobile_settings,
)
from gdx_dispatch.models.tenant_models import AppSettings


def _fresh_request() -> SimpleNamespace:
    """Stub request whose ``state`` is a plain SimpleNamespace.

    Mirrors how Starlette exposes request.state — a setattr-friendly
    object — without dragging the real Request class into a unit test.
    """
    return SimpleNamespace(state=SimpleNamespace())


class TestLoad:
    def test_no_app_settings_row_returns_pure_catalog_defaults(self, tenant_db) -> None:
        # Brand-new tenant DB with no AppSettings row — the helper must
        # still return every catalog default so callers can read settings
        # before the tenant hits Save.
        resolved = load_tenant_mobile_settings(tenant_db)
        assert resolved["tech_mobile.gps_retention_days"] == 45
        assert resolved["tech_mobile.drive_time_provider"] == "google"
        assert set(resolved.keys()) == set(TECH_MOBILE_SETTINGS.keys())

    def test_app_settings_row_with_empty_overrides(self, tenant_db) -> None:
        tenant_db.add(AppSettings(tenant_mobile_settings={}))
        tenant_db.commit()
        resolved = load_tenant_mobile_settings(tenant_db)
        assert resolved["tech_mobile.gps_retention_days"] == 45

    def test_overrides_merge_over_catalog_defaults(self, tenant_db) -> None:
        tenant_db.add(
            AppSettings(
                tenant_mobile_settings={
                    "tech_mobile.gps_retention_days": 90,
                    "tech_mobile.drive_time_provider": "off",
                }
            )
        )
        tenant_db.commit()
        resolved = load_tenant_mobile_settings(tenant_db)
        assert resolved["tech_mobile.gps_retention_days"] == 90
        assert resolved["tech_mobile.drive_time_provider"] == "off"
        # Untouched settings still come from the catalog.
        assert resolved["tech_mobile.gps_breadcrumb_interval_sec"] == tech_mobile_default(
            "tech_mobile.gps_breadcrumb_interval_sec"
        )

    def test_stale_override_keys_ignored(self, tenant_db) -> None:
        # If the catalog drops a key in a later release, an override
        # that's still in the column for that key must NOT show up in
        # the resolved dict.
        tenant_db.add(
            AppSettings(
                tenant_mobile_settings={
                    "tech_mobile.removed_in_v2": "stale",
                    "tech_mobile.gps_retention_days": 60,
                }
            )
        )
        tenant_db.commit()
        resolved = load_tenant_mobile_settings(tenant_db)
        assert "tech_mobile.removed_in_v2" not in resolved
        assert resolved["tech_mobile.gps_retention_days"] == 60

    def test_non_dict_storage_treated_as_no_overrides(self, tenant_db) -> None:
        # Defensive: if the column somehow ends up holding a non-dict
        # value (corruption / bad migration), reads must still succeed.
        row = AppSettings(tenant_mobile_settings={})
        tenant_db.add(row)
        tenant_db.commit()
        # Bypass the validator and force the bad shape directly.
        row.tenant_mobile_settings = ["not", "a", "dict"]  # type: ignore[assignment]
        tenant_db.commit()
        resolved = load_tenant_mobile_settings(tenant_db)
        assert resolved["tech_mobile.gps_retention_days"] == 45


class TestRequestCache:
    def test_first_load_populates_request_state(self, tenant_db) -> None:
        request = _fresh_request()
        assert getattr(request.state, "mobile_settings", None) is None
        load_tenant_mobile_settings(tenant_db, request=request)
        assert isinstance(request.state.mobile_settings, dict)

    def test_second_load_returns_cached_dict_object(self, tenant_db) -> None:
        request = _fresh_request()
        first = load_tenant_mobile_settings(tenant_db, request=request)
        second = load_tenant_mobile_settings(tenant_db, request=request)
        # Identity, not equality — proves the second call short-circuits
        # without re-reading the AppSettings row.
        assert first is second

    def test_cache_isolated_per_request(self, tenant_db) -> None:
        req_a = _fresh_request()
        req_b = _fresh_request()
        load_tenant_mobile_settings(tenant_db, request=req_a)
        load_tenant_mobile_settings(tenant_db, request=req_b)
        assert req_a.state.mobile_settings is not req_b.state.mobile_settings


class TestSingleKey:
    def test_returns_catalog_default_when_no_override(self, tenant_db) -> None:
        assert get_tenant_mobile_setting(tenant_db, "tech_mobile.gps_retention_days") == 45

    def test_returns_override_value_when_set(self, tenant_db) -> None:
        tenant_db.add(
            AppSettings(
                tenant_mobile_settings={"tech_mobile.gps_retention_days": 200}
            )
        )
        tenant_db.commit()
        assert get_tenant_mobile_setting(tenant_db, "tech_mobile.gps_retention_days") == 200

    def test_unknown_key_without_default_raises(self, tenant_db) -> None:
        with pytest.raises(KeyError):
            get_tenant_mobile_setting(tenant_db, "tech_mobile.does_not_exist")

    def test_unknown_key_with_default_returns_default(self, tenant_db) -> None:
        assert (
            get_tenant_mobile_setting(
                tenant_db, "tech_mobile.does_not_exist", default="fallback"
            )
            == "fallback"
        )

    def test_known_key_ignores_caller_default(self, tenant_db) -> None:
        # If the catalog has the key, the catalog default wins over the
        # caller-supplied default. Keeps the catalog authoritative.
        assert (
            get_tenant_mobile_setting(
                tenant_db,
                "tech_mobile.gps_retention_days",
                default=99999,
            )
            == 45
        )

    def test_request_cache_shared_with_bulk_loader(self, tenant_db) -> None:
        request = _fresh_request()
        load_tenant_mobile_settings(tenant_db, request=request)
        # After the bulk load, swapping the AppSettings storage should
        # NOT change a per-key read on the same request — proves the
        # cache is honored.
        tenant_db.add(
            AppSettings(
                tenant_mobile_settings={"tech_mobile.gps_retention_days": 200}
            )
        )
        tenant_db.commit()
        assert (
            get_tenant_mobile_setting(
                tenant_db, "tech_mobile.gps_retention_days", request=request
            )
            == 45
        )
