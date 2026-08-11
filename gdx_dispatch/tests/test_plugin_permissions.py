"""Per-plugin authorization rules (ADR-013, 2026-08-11).

Two layers, resolved with OR: the blanket `plugins.read`/`plugins.write` (static,
so the builtin admin contract picks them up) and the per-plugin
`plugin.<key>.read`/`.write` (generated from the installed catalog).
"""
from __future__ import annotations

import pytest

from gdx_dispatch.core import plugin_permissions
from gdx_dispatch.core.permissions import AVAILABLE_PERMISSIONS, BUILTIN_ROLES, WILDCARD
from gdx_dispatch.core.plugin_permissions import (
    action_for_method,
    catalog_entries,
    installed_plugin_keys,
    is_plugin_permission,
    may_grant_plugin_permission,
    may_use_plugin,
    reset_catalog_cache,
)


class TestKeyShape:
    @pytest.mark.parametrize("key", [
        "plugin.chipricing.read",
        "plugin.midland.write",
        "plugin.a.read",
        "plugin.some-plugin_2.write",
    ])
    def test_accepts_well_formed_keys(self, key):
        assert is_plugin_permission(key)

    @pytest.mark.parametrize("key", [
        "plugins.read",          # the blanket key is static, not per-plugin
        "plugin..read",          # empty plugin key
        "plugin.chipricing.",    # no action
        "plugin.chipricing.delete",  # only read/write exist
        "plugin.CHIPRICING.read",    # plugin keys are lowercase
        "plugin.chipricing.read.extra",
        "plugin.a/b.read",
        "*",
        "",
        None,
    ])
    def test_rejects_malformed_keys(self, key):
        assert not is_plugin_permission(key)

    def test_shape_not_membership(self):
        # Deliberate: validation must not depend on the plugin-host being
        # reachable, or a role save during an outage would silently strip every
        # per-plugin grant a tenant had.
        assert is_plugin_permission("plugin.notinstalledanywhere.read")


class TestBlanketKeysAreStatic:
    def test_blanket_keys_are_in_the_catalog(self):
        assert "plugins.read" in AVAILABLE_PERMISSIONS
        assert "plugins.write" in AVAILABLE_PERMISSIONS

    def test_admin_holds_the_blanket_grant(self):
        # The whole reason the blanket layer exists: BUILTIN_ROLES["admin"] is
        # computed from the STATIC catalog, so a purely dynamic scheme would
        # leave admins with no plugin key at all.
        assert "plugins.read" in BUILTIN_ROLES["admin"]
        assert "plugins.write" in BUILTIN_ROLES["admin"]

    def test_technician_holds_neither(self):
        assert "plugins.read" not in BUILTIN_ROLES["technician"]
        assert "plugins.write" not in BUILTIN_ROLES["technician"]

    def test_viewer_gets_no_plugin_write(self):
        assert "plugins.write" not in BUILTIN_ROLES["viewer"]


class TestMayUsePlugin:
    def test_wildcard_passes(self):
        assert may_use_plugin({WILDCARD}, "chipricing", "write")

    def test_blanket_covers_any_plugin(self):
        assert may_use_plugin({"plugins.read"}, "chipricing", "read")
        assert may_use_plugin({"plugins.read"}, "midland", "read")

    def test_blanket_read_does_not_confer_write(self):
        assert not may_use_plugin({"plugins.read"}, "chipricing", "write")

    def test_per_plugin_grant_is_scoped_to_that_plugin(self):
        perms = {"plugin.chipricing.read"}
        assert may_use_plugin(perms, "chipricing", "read")
        assert not may_use_plugin(perms, "midland", "read")

    def test_per_plugin_read_does_not_confer_write(self):
        assert not may_use_plugin({"plugin.chipricing.read"}, "chipricing", "write")

    def test_unrelated_permissions_do_not_pass(self):
        assert not may_use_plugin({"jobs.read_own", "mobile.use"}, "chipricing", "read")

    def test_empty_key_is_denied_even_for_the_wildcard(self):
        # An unidentifiable target is a denied one — never guess which plugin a
        # request was for.
        assert not may_use_plugin({WILDCARD}, "", "read")


class TestMethodMapping:
    @pytest.mark.parametrize("method", ["GET", "get", "HEAD", "OPTIONS"])
    def test_reads(self, method):
        assert action_for_method(method) == "read"

    @pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "post", ""])
    def test_everything_else_is_a_write(self, method):
        # Strict on purpose: a POST used as a search costs someone a 403 they
        # report; a mutating method graded as a read is a silent write.
        assert action_for_method(method) == "write"


class TestDelegation:
    def test_owner_may_grant_anything(self):
        assert may_grant_plugin_permission({WILDCARD}, "plugin.chipricing.write")

    def test_blanket_holder_may_delegate_that_action(self):
        # Without this, an admin (whose contract is `plugins.write`, never
        # `plugin.chipricing.write`) would see every per-plugin checkbox and be
        # able to tick none — leaving the owner as the only one who could ever
        # grant a plugin.
        assert may_grant_plugin_permission({"plugins.write"}, "plugin.chipricing.write")
        assert may_grant_plugin_permission({"plugins.read"}, "plugin.chipricing.read")

    def test_blanket_read_does_not_delegate_write(self):
        assert not may_grant_plugin_permission({"plugins.read"}, "plugin.chipricing.write")

    def test_holding_one_plugin_does_not_delegate_it(self):
        # Still a cap: a per-plugin grantee is not a delegator.
        assert not may_grant_plugin_permission(
            {"plugin.chipricing.write"}, "plugin.chipricing.write"
        )

    def test_unrelated_grantor_delegates_nothing(self):
        assert not may_grant_plugin_permission({"jobs.write"}, "plugin.chipricing.read")

    def test_non_plugin_keys_are_not_handled_here(self):
        # The caller's exact-match cap still governs everything else.
        assert not may_grant_plugin_permission({"plugins.write"}, "billing.write")


class TestCatalogEntries:
    def test_emits_a_read_write_pair_per_plugin(self):
        rows = catalog_entries({"chipricing": "CHI Pricing", "midland": "Midland"})
        keys = [r["key"] for r in rows]
        assert keys == [
            "plugin.chipricing.read", "plugin.chipricing.write",
            "plugin.midland.read", "plugin.midland.write",
        ]
        assert {r["category"] for r in rows} == {"plugins"}

    def test_uses_the_human_label(self):
        rows = catalog_entries({"chipricing": "CHI Pricing"})
        assert rows[0]["label"] == "Use CHI Pricing"
        assert rows[1]["label"] == "Change data in CHI Pricing"

    def test_falls_back_to_the_key_when_unnamed(self):
        assert catalog_entries({"midland": ""})[0]["label"] == "Use midland"

    def test_drops_a_malformed_plugin_key(self):
        # A hostile or malformed key must never mint a permission that can't be
        # matched by is_plugin_permission — that would be an ungrantable row.
        assert catalog_entries({"Bad Key!": "x", "ok": "OK"}) == catalog_entries({"ok": "OK"})

    def test_empty_catalog_is_empty(self):
        assert catalog_entries({}) == []


class TestCatalogFetchDegrades:
    def test_unreachable_plugin_host_yields_no_rows(self, monkeypatch):
        # The Roles screen must still render (with the blanket keys) when
        # plugin-host is down — never 500.
        monkeypatch.setenv("PLUGIN_HOST_URL", "http://127.0.0.1:9")  # closed port
        assert installed_plugin_keys(timeout=0.25) == {}


class TestCatalogCaching:
    """The Roles screen calls installed_plugin_keys on a get_current_user-only
    endpoint. Uncached, that let ANY authenticated user make the app issue a
    blocking upstream request per page load — against plugin-host, which
    pip-installs on boot and has been observed hanging. Sync endpoint bodies run
    in a bounded threadpool, so enough concurrent calls would tie up every
    worker. (2026-08-11 audit.)
    """

    def setup_method(self):
        reset_catalog_cache()

    def teardown_method(self):
        reset_catalog_cache()

    def _counting_fetch(self, monkeypatch, value):
        calls = {"n": 0}

        def fake(_timeout):
            calls["n"] += 1
            return dict(value)

        monkeypatch.setattr(plugin_permissions, "_fetch_plugin_keys", fake)
        return calls

    def test_repeat_calls_hit_the_cache(self, monkeypatch):
        calls = self._counting_fetch(monkeypatch, {"chipricing": "CHI"})
        for _ in range(25):
            assert installed_plugin_keys() == {"chipricing": "CHI"}
        assert calls["n"] == 1

    def test_a_failed_fetch_is_cached_too(self, monkeypatch):
        # An outage must cost one slow call per TTL, not one per request —
        # otherwise the cache does nothing in exactly the case it exists for.
        calls = self._counting_fetch(monkeypatch, {})
        for _ in range(25):
            assert installed_plugin_keys() == {}
        assert calls["n"] == 1

    def test_refresh_forces_a_fetch(self, monkeypatch):
        calls = self._counting_fetch(monkeypatch, {"chipricing": "CHI"})
        installed_plugin_keys()
        installed_plugin_keys(refresh=True)
        assert calls["n"] == 2

    def test_reset_drops_the_cache(self, monkeypatch):
        # Called when plugin-host restarts, so a newly installed plugin's
        # checkboxes appear without waiting out the TTL.
        calls = self._counting_fetch(monkeypatch, {"chipricing": "CHI"})
        installed_plugin_keys()
        reset_catalog_cache()
        installed_plugin_keys()
        assert calls["n"] == 2

    def test_caller_cannot_mutate_the_cached_dict(self, monkeypatch):
        self._counting_fetch(monkeypatch, {"chipricing": "CHI"})
        first = installed_plugin_keys()
        first["injected"] = "nope"
        assert "injected" not in installed_plugin_keys()

    def test_an_ungrantable_key_is_dropped_not_silently_kept(self, monkeypatch):
        # A non-lowercase manifest key can never match is_plugin_permission, so
        # a per-plugin row for it would be an ungrantable checkbox.
        import httpx

        class _Resp:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return [{"key": "GoodBad", "name": "x"}, {"key": "fine", "name": "Fine"}]

        monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
        assert installed_plugin_keys(refresh=True) == {"fine": "Fine"}
