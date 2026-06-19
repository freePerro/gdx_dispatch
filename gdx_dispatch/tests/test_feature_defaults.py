"""Catalog + validator unit tests for gdx_dispatch/core/feature_defaults.py.

The catalog is the source of truth for tech-mobile setting defaults +
bounds. Validation runs on every admin write — these tests pin the
contract so a typo'd catalog entry doesn't slip past code review.
"""
from __future__ import annotations

import pytest

from gdx_dispatch.core.feature_defaults import (
    TECH_MOBILE_SETTINGS,
    list_tech_mobile_settings,
    tech_mobile_default,
    validate_tech_mobile_value,
)


class TestCatalogShape:
    def test_every_entry_has_required_keys(self) -> None:
        required = {"type", "default", "phase", "label"}
        for key, meta in TECH_MOBILE_SETTINGS.items():
            missing = required - meta.keys()
            assert not missing, f"{key} missing keys: {missing}"

    def test_every_entry_namespaces_under_tech_mobile(self) -> None:
        for key in TECH_MOBILE_SETTINGS:
            assert key.startswith("tech_mobile."), f"{key!r} not namespaced"

    def test_default_round_trips_through_validator(self) -> None:
        # If a tenant resets a setting, the catalog default must itself be
        # a value the validator accepts. Catches bound/default mismatches.
        for key, meta in TECH_MOBILE_SETTINGS.items():
            assert validate_tech_mobile_value(key, meta["default"]) == meta["default"]

    def test_enum_bounds_are_tuples(self) -> None:
        for key, meta in TECH_MOBILE_SETTINGS.items():
            if meta["type"] == "enum":
                assert isinstance(meta["bounds"], tuple), f"{key} enum bounds must be tuple"
                assert len(meta["bounds"]) >= 2, f"{key} enum needs ≥2 options"

    def test_int_bounds_are_ordered_pairs(self) -> None:
        for key, meta in TECH_MOBILE_SETTINGS.items():
            if meta["type"] == "int" and meta.get("bounds") is not None:
                lo, hi = meta["bounds"]
                assert lo <= hi, f"{key} bounds out of order: {meta['bounds']}"
                assert lo <= meta["default"] <= hi, (
                    f"{key} default {meta['default']} outside bounds {meta['bounds']}"
                )


class TestListing:
    def test_listing_orders_by_phase(self) -> None:
        rows = list_tech_mobile_settings()
        phases = [r["phase"] for r in rows]
        assert phases == sorted(phases)

    def test_listing_includes_key_and_metadata(self) -> None:
        rows = list_tech_mobile_settings()
        assert all("key" in r and "type" in r and "default" in r for r in rows)


class TestDefaults:
    def test_default_returns_catalog_value(self) -> None:
        assert tech_mobile_default("tech_mobile.gps_retention_days") == 45
        assert tech_mobile_default("tech_mobile.drive_time_provider") == "google"

    def test_default_unknown_key_raises(self) -> None:
        with pytest.raises(KeyError):
            tech_mobile_default("tech_mobile.does_not_exist")


class TestValidator:
    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown setting"):
            validate_tech_mobile_value("tech_mobile.bogus", True)

    def test_bool_accepts_true_false_only(self) -> None:
        assert validate_tech_mobile_value("tech_mobile.gps_tracking_enabled", True) is True
        assert validate_tech_mobile_value("tech_mobile.gps_tracking_enabled", False) is False
        with pytest.raises(ValueError, match="expected bool"):
            validate_tech_mobile_value("tech_mobile.gps_tracking_enabled", 1)
        with pytest.raises(ValueError, match="expected bool"):
            validate_tech_mobile_value("tech_mobile.gps_tracking_enabled", "yes")

    def test_int_accepts_values_in_bounds(self) -> None:
        assert validate_tech_mobile_value("tech_mobile.gps_retention_days", 7) == 7
        assert validate_tech_mobile_value("tech_mobile.gps_retention_days", 365) == 365

    def test_int_rejects_out_of_bounds(self) -> None:
        with pytest.raises(ValueError, match="out of bounds"):
            validate_tech_mobile_value("tech_mobile.gps_retention_days", 6)
        with pytest.raises(ValueError, match="out of bounds"):
            validate_tech_mobile_value("tech_mobile.gps_retention_days", 366)

    def test_int_rejects_bool_disguised_as_int(self) -> None:
        # Python bool is an int subclass — guard against True silently
        # passing as 1 for a numeric setting.
        with pytest.raises(ValueError, match="expected int"):
            validate_tech_mobile_value("tech_mobile.gps_retention_days", True)

    def test_enum_accepts_listed_values(self) -> None:
        assert (
            validate_tech_mobile_value("tech_mobile.drive_time_provider", "mapbox")
            == "mapbox"
        )

    def test_enum_rejects_unlisted_value(self) -> None:
        with pytest.raises(ValueError, match="not in"):
            validate_tech_mobile_value("tech_mobile.drive_time_provider", "waze")
