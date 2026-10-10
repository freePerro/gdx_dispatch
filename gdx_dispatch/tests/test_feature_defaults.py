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
        assert validate_tech_mobile_value("tech_mobile.gps_breadcrumb_enabled", True) is True
        assert validate_tech_mobile_value("tech_mobile.gps_breadcrumb_enabled", False) is False
        with pytest.raises(ValueError, match="expected bool"):
            validate_tech_mobile_value("tech_mobile.gps_breadcrumb_enabled", 1)
        with pytest.raises(ValueError, match="expected bool"):
            validate_tech_mobile_value("tech_mobile.gps_breadcrumb_enabled", "yes")

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


class TestNoLeadTechCompletionToggle:
    """#644 — both lead-tech-completion toggles are gone, and stay gone.

    Two keys spelled the same idea in opposite orders. Only one was ever
    read, and only by a gate on the deprecated
    ``POST /api/mobile/jobs/{job_id}/complete``; the other was never read
    by anything, so the admin form offered a switch that could not do
    what its label promised. Both were removed rather than repaired —
    lead-tech-only completion is deferred, not shipped.

    An absence assertion is the honest guard here: re-adding a catalog
    entry is what would make the UI lie again, and that is exactly what
    this catches. Whoever builds the feature for real deletes this class
    in the same PR that adds a reader.
    """

    def test_neither_spelling_is_in_the_catalog(self) -> None:
        assert "tech_mobile.completion_lead_tech_only" not in TECH_MOBILE_SETTINGS
        assert "tech_mobile.lead_tech_only_completion" not in TECH_MOBILE_SETTINGS


class TestEveryKeyHasAReader:
    """GDXA-493 — every catalog key is read by shipped code, or is allowlisted.

    The admin form renders every catalog entry as a live switch. A key that
    nothing reads lets the office flip a setting that changes nothing — #644
    deleted two such keys, GDXA-493 sixteen more (among them three spellings
    of one idea twice over: ``gps_breadcrumb_interval_sec``/``_seconds`` and
    ``push_fallback_policy`` beside the live ``push_fallback_mode``).

    A *reader* is a string literal equal to the full key in a tracked
    ``.py``/``.vue``/``.js``/``.ts`` file under ``gdx_dispatch/`` that is not a
    test, an e2e spec, a doc or this catalog. Python is parsed, so a docstring
    or comment naming a key is not a reader; in JS/Vue/TS the key must sit in
    quotes or backticks, which a ``//`` prose comment usually does not. What
    this cannot see: a key assembled at runtime (``"tech_mobile." + name``) —
    none exists today — and a reader that fetches the setting and then
    ignores it. The second is the #644 shape that only a reviewer catches.
    """

    _SUFFIXES = (".py", ".vue", ".js", ".ts")
    _CATALOG = "gdx_dispatch/core/feature_defaults.py"

    # key -> reason it may stay in the catalog with no reader. Empty on
    # purpose; an entry here needs a reason a reviewer would accept.
    ALLOWLIST: dict[str, str] = {}

    @classmethod
    def _is_reader_source(cls, path: str) -> bool:
        if not path.startswith("gdx_dispatch/") or not path.endswith(cls._SUFFIXES):
            return False
        if path == cls._CATALOG:
            return False
        parts = path.split("/")
        if {"tests", "e2e", "__tests__", "docs", "node_modules"} & set(parts):
            return False
        name = parts[-1]
        return ".spec." not in name and ".test." not in name

    @staticmethod
    def _python_literals(source: str) -> set[str]:
        import ast

        tree = ast.parse(source)
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(
                node,
                (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                body = node.body
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                ):
                    docstrings.add(id(body[0].value))
        return {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        }

    @classmethod
    def unread_keys(cls, keys, sources: dict[str, str]) -> list[str]:
        """Keys with no reader among ``sources`` (repo path -> text)."""
        import re

        py_literals: set[str] = set()
        other_text: list[str] = []
        for path, text in sources.items():
            if path.endswith(".py"):
                py_literals |= cls._python_literals(text)
            else:
                other_text.append(text)
        blob = "\n".join(other_text)
        unread = []
        for key in keys:
            if key in py_literals:
                continue
            if re.search(r"""['"`]""" + re.escape(key) + r"""['"`]""", blob):
                continue
            unread.append(key)
        return sorted(unread)

    @classmethod
    def _repo_sources(cls) -> dict[str, str]:
        from gdx_dispatch.tools.tracked_files import REPO_ROOT, read_tracked_files

        return {
            path: (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
            for path in sorted(read_tracked_files())
            if cls._is_reader_source(path) and (REPO_ROOT / path).is_file()
        }

    def test_every_catalog_key_has_a_reader_or_a_reason(self) -> None:
        unread = self.unread_keys(TECH_MOBILE_SETTINGS, self._repo_sources())
        unexplained = [k for k in unread if k not in self.ALLOWLIST]
        assert not unexplained, (
            "tech-mobile catalog keys the admin form offers that nothing reads: "
            f"{unexplained}. Wire a reader, delete the entry, or allowlist it "
            "with a reason in TestEveryKeyHasAReader.ALLOWLIST."
        )

    def test_allowlist_does_not_rot(self) -> None:
        # An allowlisted key that is gone, or has since gained a reader, is
        # a stale exemption: remove it so the next unread key cannot hide
        # behind it.
        stale = [k for k in self.ALLOWLIST if k not in TECH_MOBILE_SETTINGS]
        assert not stale, f"allowlisted keys not in the catalog: {stale}"
        unread = set(self.unread_keys(self.ALLOWLIST, self._repo_sources()))
        read_now = [k for k in self.ALLOWLIST if k not in unread]
        assert not read_now, f"allowlisted keys that now have a reader: {read_now}"

    def test_the_guard_can_fail(self) -> None:
        # The ratchet is only worth citing if it reddens for the defect.
        sources = {
            "gdx_dispatch/routers/x.py": (
                '"""Docstring naming "tech_mobile.in_docstring" is not a read."""\n'
                "# tech_mobile.in_comment is not a read either\n"
                'VALUE = get_setting(db, "tech_mobile.read_py")\n'
            ),
            "gdx_dispatch/frontend/src/views/X.vue": (
                "// tech_mobile.in_js_comment\n"
                "const v = settings['tech_mobile.read_js']\n"
            ),
        }
        keys = [
            "tech_mobile.read_py",
            "tech_mobile.read_js",
            "tech_mobile.in_docstring",
            "tech_mobile.in_comment",
            "tech_mobile.in_js_comment",
            "tech_mobile.nowhere",
        ]
        assert self.unread_keys(keys, sources) == [
            "tech_mobile.in_comment",
            "tech_mobile.in_docstring",
            "tech_mobile.in_js_comment",
            "tech_mobile.nowhere",
        ]
