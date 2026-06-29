"""Tests for the plugin_api foundation (ADR-013 step 1): manifest validation,
the version compat gate, and discovery's skip-don't-crash behavior.

Stdlib-only by design — runs on the host without the docker image. Also runnable
directly: `python3 gdx_dispatch/tests/test_plugin_discovery.py`.
"""
from __future__ import annotations

import pytest

from gdx_dispatch.plugin_api import (
    PluginManifest,
    is_compatible,
    load_manifests,
)


class _FakeEP:
    """Stand-in for an importlib.metadata EntryPoint: has .name and .load()."""

    def __init__(self, name, value=None, raises=None):
        self.name = name
        self._value = value
        self._raises = raises

    def load(self):
        if self._raises is not None:
            raise self._raises
        return self._value


def _manifest(key="foo", name="Foo", tier="professional", requires=""):
    return PluginManifest(key=key, name=name, tier=tier, requires=requires)


# --- PluginManifest validation -------------------------------------------------

def test_manifest_valid():
    m = _manifest()
    assert m.key == "foo" and m.name == "Foo" and m.tier == "professional"


def test_manifest_rejects_bad_key():
    for bad in ("", "  ", "Foo", "foo bar ".rstrip() + " "):
        with pytest.raises(ValueError):
            _manifest(key=bad)


def test_manifest_rejects_empty_name():
    with pytest.raises(ValueError):
        _manifest(name="")


def test_manifest_rejects_bad_tier():
    with pytest.raises(ValueError):
        _manifest(tier="enterprise")


# --- is_compatible -------------------------------------------------------------

def test_compat_empty_requires_any_version():
    assert is_compatible("", "1.2.0") is True
    assert is_compatible("   ", "0") is True


def test_compat_satisfied():
    assert is_compatible("gdx>=1.2", "1.2.0") is True
    assert is_compatible("gdx>=1.2.0", "1.3.0") is True
    assert is_compatible("gdx >= 1.0", "1.0.0") is True


def test_compat_not_satisfied():
    assert is_compatible("gdx>=2.0", "1.9.9") is False
    assert is_compatible("gdx>=1.10", "1.9.0") is False  # numeric, not string compare


def test_compat_unrecognized_fails_closed():
    # An unparseable constraint must NOT silently load.
    for bad in ("gdx==1.0", "gdx<2.0", ">=1.0", "django>=4", "garbage"):
        assert is_compatible(bad, "9.9.9") is False


# --- load_manifests ------------------------------------------------------------

def test_load_includes_compatible():
    eps = [_FakeEP("foo", _manifest(key="foo", requires="gdx>=1.0"))]
    out = load_manifests(eps, "1.5.0")
    assert [m.key for m in out] == ["foo"]


def test_load_skips_incompatible():
    eps = [
        _FakeEP("foo", _manifest(key="foo", requires="gdx>=99")),
        _FakeEP("bar", _manifest(key="bar", requires="")),
    ]
    out = load_manifests(eps, "1.0.0")
    assert [m.key for m in out] == ["bar"]  # foo gated out, bar survives


def test_load_skips_non_manifest():
    eps = [_FakeEP("weird", value={"not": "a manifest"})]
    assert load_manifests(eps, "1.0.0") == []


def test_load_skips_one_that_raises_without_killing_others():
    eps = [
        _FakeEP("boom", raises=RuntimeError("import blew up")),
        _FakeEP("ok", _manifest(key="ok")),
    ]
    out = load_manifests(eps, "1.0.0")
    assert [m.key for m in out] == ["ok"]  # boom skipped, ok still loads


# --- discover_with_dists (pairs each manifest with its distribution version) ---

class _FakeDist:
    def __init__(self, name, version):
        self.name = name
        self.version = version


def test_discover_with_dists_pairs_manifest_to_installed_version(monkeypatch):
    import importlib.metadata as md

    from gdx_dispatch.plugin_api import discovery

    ep_ok = _FakeEP("chi", _manifest(key="chipricing", requires=""))
    ep_ok.dist = _FakeDist("gdx-plugin-chi-pricing", "0.1.2")
    ep_gated = _FakeEP("old", _manifest(key="old", requires="gdx>=99"))
    ep_gated.dist = _FakeDist("old", "1.0")

    monkeypatch.setattr(md, "entry_points", lambda group: [ep_ok, ep_gated])
    out = discovery.discover_with_dists(current_version="1.5.0")
    # gated plugin dropped (compat); survivor carries its installed dist version
    assert [(m.key, n, v) for m, n, v in out] == [
        ("chipricing", "gdx-plugin-chi-pricing", "0.1.2")]


if __name__ == "__main__":
    import sys

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 — self-check harness
                failures += 1
                print(f"FAIL {name}: {exc}")
    print("ok" if not failures else f"{failures} FAILED")
    sys.exit(1 if failures else 0)
