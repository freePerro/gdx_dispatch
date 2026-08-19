"""Env knobs the code reads must actually reach the container.

`docker-compose.yml` enumerates `environment:` explicitly rather than passing
the whole `.env` through, so a setting can be documented, implemented, and
completely unreachable: the operator sets it, nothing happens, and there is no
error to explain why. That happened to `GDX_PLUGIN_CATALOG_URL` — the plugin
storefront's catalog override — which was written as env-configurable and then
silently ignored on a real deploy.

This is a text check on the compose files, deliberately: the failure is a
missing line in YAML, and no amount of Python testing sees it.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

DOCKER = pathlib.Path(__file__).resolve().parents[1] / "docker"

#: Settings the app reads from the environment that an operator is expected to
#: set. Add to this list when you add an operator-facing env knob.
OPERATOR_ENV = [
    ("GDX_PLUGIN_CATALOG_URL", "the plugin storefront's catalog URL"),
]

#: Compose files that stand up the app for a real deployment.
DEPLOY_COMPOSE = ["docker-compose.yml", "docker-compose.customer.yml"]


def _app_environment(filename: str) -> dict:
    data = yaml.safe_load((DOCKER / filename).read_text())
    app = (data.get("services") or {}).get("app")
    assert app, f"{filename} has no app service"
    env = app.get("environment") or {}
    if isinstance(env, list):  # `- KEY=value` form
        env = dict(item.split("=", 1) for item in env if "=" in item)
    return env


@pytest.mark.parametrize("filename", DEPLOY_COMPOSE)
@pytest.mark.parametrize(("var", "what"), OPERATOR_ENV)
def test_the_app_container_receives_operator_env(filename, var, what):
    env = _app_environment(filename)
    assert var in env, (
        f"{filename} never passes {var} to the app container, so setting {what} "
        "in .env does nothing and the operator gets no error saying why"
    )


@pytest.mark.parametrize("filename", DEPLOY_COMPOSE)
@pytest.mark.parametrize(("var", "what"), OPERATOR_ENV)
def test_an_unset_knob_falls_back_rather_than_blanking(filename, var, what):
    """`${VAR:-}` yields an empty string, so the code's default must survive it.

    Asserting the compose default is empty pairs with `catalog_url()` treating
    empty as "unset" — together they mean an operator who never sets the
    variable gets the built-in default rather than a blank URL.
    """
    value = str(_app_environment(filename)[var])
    assert value.startswith("${") and ":-" in value, (
        f"{filename}: {var} should be `${{{var}:-...}}` so an unset value is handled"
    )


def test_the_catalog_url_default_survives_an_empty_env():
    """The other half: empty must mean "use the default", not "use nothing"."""
    import os

    from gdx_dispatch.core import plugin_storefront as store

    old = os.environ.get("GDX_PLUGIN_CATALOG_URL")
    try:
        os.environ["GDX_PLUGIN_CATALOG_URL"] = ""
        assert store.catalog_url() == store.DEFAULT_CATALOG_URL
        os.environ["GDX_PLUGIN_CATALOG_URL"] = "   "
        assert store.catalog_url() == store.DEFAULT_CATALOG_URL
        os.environ["GDX_PLUGIN_CATALOG_URL"] = "https://example.invalid/c.json"
        assert store.catalog_url() == "https://example.invalid/c.json"
    finally:
        if old is None:
            os.environ.pop("GDX_PLUGIN_CATALOG_URL", None)
        else:
            os.environ["GDX_PLUGIN_CATALOG_URL"] = old
