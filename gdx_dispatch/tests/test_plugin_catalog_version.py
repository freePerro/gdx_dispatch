"""The plugin catalog publishes which version is actually running.

`/api/plugins` used to return key/name/tier/ui/permissions and nothing about
the distribution behind them. Two things were therefore impossible outside the
plugin-host process:

* saying WHICH version of a plugin is running — the pairing exists inside
  `discover_with_dists()` and was simply dropped on the floor; and
* joining a plugin key (manifest-defined, e.g. "n8n") to the distribution the
  install tables key on (e.g. "gdx-plugin-n8n").

The tempting substitute — reading a version off `plugin_artifact`'s filename —
reports the version that was *meant* to be installed, which is exactly the
stale-code lie: metadata can say 2.0.0 while the loaded code is 1.0.0. Only
the running process can answer this honestly.

Needs FastAPI → runs in the docker image.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from gdx_dispatch.plugin_api.manifest import PluginManifest
from gdx_dispatch.plugin_host.app import create_plugin_host


def _manifest(key: str) -> PluginManifest:
    return PluginManifest(key=key, name=f"{key.title()} Plugin", tier="starter")


def _catalog(client) -> dict:
    r = client.get("/api/plugins")
    assert r.status_code == 200
    return {p["key"]: p for p in r.json()}


def test_catalog_reports_the_running_version_and_distribution():
    app = create_plugin_host(
        plugins=[_manifest("n8n")],
        dists={"n8n": ("gdx-plugin-n8n", "0.4.0")},
    )
    entry = _catalog(TestClient(app))["n8n"]

    assert entry["version"] == "0.4.0"
    assert entry["distribution"] == "gdx-plugin-n8n"
    # The pre-existing contract must not shift underneath core.
    assert entry["key"] == "n8n"
    assert entry["name"] == "N8N Plugin"
    assert entry["tier"] == "starter"
    assert "permissions" in entry and "ui" in entry


def test_distribution_differs_from_key_so_the_join_is_not_guesswork():
    """A plugin key is not its package name — that is the whole reason this
    field has to be published rather than inferred."""
    app = create_plugin_host(
        plugins=[_manifest("chipricing")],
        dists={"chipricing": ("gdx-plugin-chi-pricing", "0.3.1")},
    )
    entry = _catalog(TestClient(app))["chipricing"]

    assert entry["key"] != entry["distribution"]
    assert entry["distribution"] == "gdx-plugin-chi-pricing"


def test_unknown_distribution_reports_null_not_a_guess():
    """No dist info (an injected/test plugin) must read as unknown, never as a
    fabricated version — a wrong version here would be worse than none."""
    app = create_plugin_host(plugins=[_manifest("example")])
    entry = _catalog(TestClient(app))["example"]

    assert entry["version"] is None
    assert entry["distribution"] is None


def test_a_stale_plugin_is_still_absent_from_the_catalog():
    """Fail-closed behaviour is unchanged: a plugin loaded at the wrong version
    is withheld entirely, so it cannot report a version at all."""
    app = create_plugin_host(
        plugins=[_manifest("n8n"), _manifest("example")],
        dists={"n8n": ("gdx-plugin-n8n", "0.4.0")},
        stale={"n8n": {"installed": "0.4.0", "desired": "0.5.0"}},
    )
    catalog = _catalog(TestClient(app))

    assert "n8n" not in catalog
    assert "example" in catalog
