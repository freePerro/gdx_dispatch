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
    ("GDX_INTERNAL_TOKEN", "the plugin-host /internal/* shared secret"),
    ("PLUGIN_BROWSER_ALLOWED_HOSTS", "the browser-stream host allowlist"),
]

#: Compose files that stand up the app for a real deployment.
DEPLOY_COMPOSE = ["docker-compose.yml", "docker-compose.customer.yml"]

#: Vars that more than one container must agree on, and every service that needs
#: the SAME value. Checking only `app` is not enough: a shared secret delivered
#: to app alone fails closed everywhere else, silently.
#:
#: GDX_INTERNAL_TOKEN's list includes celery-high on purpose — it runs
#: `deliver_plugin_event_task`, which POSTs plugin-host /internal/events itself
#: (core/plugin_events.py). The issue that asked for this fix (#596) named only
#: "app and plugin-host"; that would have 401'd every plugin event delivery.
_PLUGIN_HOST_CALLERS = ["app", "plugin-host", "celery-high", "celery-low", "celery-beat"]

SHARED_ENV = [
    ("GDX_INTERNAL_TOKEN", _PLUGIN_HOST_CALLERS),
    # SECRET_KEY is the LOAD-BEARING one, not GDX_INTERNAL_TOKEN. The token is
    # normally empty and derived from SECRET_KEY (core/internal_auth.py), so a
    # deploy where SECRET_KEY stopped reaching plugin-host or celery would fail
    # the internal gate closed while every GDX_INTERNAL_TOKEN assertion here
    # stayed green. Pinning only the token tested the variable nobody sets.
    ("SECRET_KEY", _PLUGIN_HOST_CALLERS),
]

#: Containers that must NOT be able to compute the internal token. n8n runs
#: untrusted workflow code on the same compose network; keeping it out of the
#: GDX secret set is the whole reason the token exists.
#:
#: Both names matter, and SECRET_KEY is the one that actually matters: the token
#: is derived from it, so handing n8n SECRET_KEY hands it the token even though
#: GDX_INTERNAL_TOKEN never appears in its environment.
TOKEN_MUST_NOT_REACH = ["n8n"]
MUST_NOT_REACH_VARS = ["GDX_INTERNAL_TOKEN", "SECRET_KEY"]


def _service_environment(filename: str, service: str) -> dict:
    data = yaml.safe_load((DOCKER / filename).read_text())
    svc = (data.get("services") or {}).get(service)
    assert svc, f"{filename} has no {service} service"
    env = svc.get("environment") or {}
    if isinstance(env, list):  # `- KEY=value` form
        env = dict(item.split("=", 1) for item in env if "=" in item)
    return env


def _app_environment(filename: str) -> dict:
    return _service_environment(filename, "app")


def _services(filename: str) -> dict:
    return yaml.safe_load((DOCKER / filename).read_text()).get("services") or {}


@pytest.mark.parametrize("filename", DEPLOY_COMPOSE)
@pytest.mark.parametrize(("var", "what"), OPERATOR_ENV)
def test_the_app_container_receives_operator_env(filename, var, what):
    env = _app_environment(filename)
    assert var in env, (
        f"{filename} never passes {var} to the app container, so setting {what} "
        "in .env does nothing and the operator gets no error saying why"
    )


#: The subset of OPERATOR_ENV where an EMPTY value is a valid, benign state
#: meaning "use the built-in default". Deliberately not all of OPERATOR_ENV:
#: GDX_INTERNAL_TOKEN is excluded because empty there means *fail closed*, not
#: "fall back", and a test named "falls back rather than blanking" asserting it
#: would document the opposite of the actual contract.
FALLS_BACK_WHEN_EMPTY = [
    ("GDX_PLUGIN_CATALOG_URL", "the plugin storefront's catalog URL"),
    ("PLUGIN_BROWSER_ALLOWED_HOSTS", "the browser-stream host allowlist"),
]


@pytest.mark.parametrize("filename", DEPLOY_COMPOSE)
@pytest.mark.parametrize(("var", "what"), FALLS_BACK_WHEN_EMPTY)
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


@pytest.mark.parametrize("filename", DEPLOY_COMPOSE)
@pytest.mark.parametrize(("var", "services"), SHARED_ENV)
def test_every_container_that_needs_a_shared_secret_receives_it(filename, var, services):
    """A shared secret delivered to some containers and not others fails closed
    on the ones that missed it — with no error naming the missing variable.

    This is the guard that can fail for #596/#598: drop GDX_INTERNAL_TOKEN from
    the x-app-env anchor, or move it into an app-only block, and this goes red.
    """
    present = {s for s in services if var in _service_environment(filename, s)}
    missing = sorted(set(services) - present)
    assert not missing, (
        f"{filename}: {var} never reaches {missing}. Every one of {services} "
        f"talks to plugin-host's /internal/* API, which fails closed without it."
    )


@pytest.mark.parametrize("filename", DEPLOY_COMPOSE)
def test_the_internal_token_never_reaches_the_untrusted_workflow_container(filename):
    """n8n runs untrusted workflow code on the compose network. The token exists
    to keep it out of /internal/*, so handing it the token defeats the point.

    Checks BOTH delivery routes, because the two stacks use different ones: the
    `environment:` block, and the secrets VOLUME that the customer stack's
    secrets-init writes runtime.env onto. An env-only assertion passes happily
    while a `gdx_secrets:/secrets:ro` mount hands n8n the whole secret set —
    which is the customer stack's actual mechanism.
    """
    services = _services(filename)
    present = [s for s in TOKEN_MUST_NOT_REACH if s in services]
    if not present:
        # SKIP, not a silent pass. docker-compose.yml runs no n8n, so this
        # parametrization checked nothing while reporting green — half an
        # advertised guard that is really decoration. Saying "skipped" keeps the
        # coverage claim honest, and turns into a real check the day that stack
        # gains one.
        pytest.skip(f"{filename} runs none of {TOKEN_MUST_NOT_REACH}")
    for service in present:
        env = _service_environment(filename, service)
        for var in MUST_NOT_REACH_VARS:
            assert var not in env, (
                f"{filename}: {service} receives {var}. That container is the "
                "threat model — and because the internal token is DERIVED from "
                "SECRET_KEY, handing it either one lets it reach /internal/*."
            )
        mounts = [str(v).split(":")[0] for v in (services[service].get("volumes") or [])]
        assert "gdx_secrets" not in mounts, (
            f"{filename}: {service} mounts gdx_secrets. entrypoint.sh sources "
            "runtime.env from there, so that volume delivers SECRET_KEY (and "
            "any explicit token) just as surely as an environment: entry. n8n "
            "gets its own gdx_n8n_secrets volume and nothing else."
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


def test_the_browser_allowlist_default_survives_an_empty_env():
    """The runtime half, and the one that matters most.

    Compose passes `${PLUGIN_BROWSER_ALLOWED_HOSTS:-}`, so the variable is
    PRESENT AND EMPTY on every deploy that does not set it. `os.environ.get(k,
    default)` only defaults when the key is ABSENT — so adding the passthrough
    while reading it that way collapses the allowlist to the empty set and
    refuses every URL, killing the browser stream on a stack that changed
    nothing. Empty must mean "use the built-in list".
    """
    import os

    from gdx_dispatch.plugin_host import browser_stream as bs

    old = os.environ.get("PLUGIN_BROWSER_ALLOWED_HOSTS")
    try:
        for blank in ("", "   ", ",", " , "):
            os.environ["PLUGIN_BROWSER_ALLOWED_HOSTS"] = blank
            assert bs.allowed_hosts(), f"blank value {blank!r} emptied the allowlist"
        os.environ["PLUGIN_BROWSER_ALLOWED_HOSTS"] = "example.com, Other.COM"
        assert bs.allowed_hosts() == {"example.com", "other.com"}
        assert bs.host_allowed("https://example.com/x")
        assert not bs.host_allowed("https://evil.invalid/x")
    finally:
        if old is None:
            os.environ.pop("PLUGIN_BROWSER_ALLOWED_HOSTS", None)
        else:
            os.environ["PLUGIN_BROWSER_ALLOWED_HOSTS"] = old
