"""#579: GDX_E2E_BYPASS=1 must actually take the slowapi layer out of e2e runs.

The old bypass lived in a `default_limits` callable. slowapi never hands such a
callable the request, so its `x-e2e-test` check could not fire: with the env var
set and the header sent, request 121 got a 429. An e2e suite that grew past
~120 requests a minute from one IP would have started flaking with 429s that
look like application errors, while the bypass read as if it worked.

create_app() now sets `limiter.enabled` from the env var. These run the real
app, the probe from the maintainer's 2026-09-13 ruling: 130 requests, past the
120/minute default, with and without the bypass.

`limiter` and its in-memory counters are module-global, so the fixture resets
the counters before each build and restores both afterwards. Otherwise a later
test in this shard would inherit a spent budget or a disabled limiter.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("JWT_SECRET", "test-jwt-secret-at-least-32-bytes-long-for-hs256-sha256-safety")

PROBES = 130  # past DEFAULT_RATE_LIMIT's 120/minute
PATH = "/health"  # a real route slowapi limits; the control arm proves it still does


@pytest.fixture
def build(monkeypatch):
    import gdx_dispatch.app as appmod

    enabled_before = appmod.limiter.enabled

    def _build(bypass: str | None) -> TestClient:
        if bypass is None:
            monkeypatch.delenv("GDX_E2E_BYPASS", raising=False)
        else:
            monkeypatch.setenv("GDX_E2E_BYPASS", bypass)
        app = appmod.create_app()
        appmod.limiter.reset()
        return TestClient(app)

    yield _build

    appmod.limiter.enabled = enabled_before
    appmod.limiter.reset()


def _first_429(client: TestClient, headers: dict[str, str]) -> int | None:
    for i in range(1, PROBES + 1):
        if client.get(PATH, headers=headers).status_code == 429:
            return i
    return None


@pytest.mark.parametrize("bypass", [None, "0"], ids=["unset", "prod-value-0"])
def test_without_the_bypass_the_default_limit_still_bites(build, bypass) -> None:
    """Control. If this stops returning 429 at 121, the probe no longer measures
    slowapi and the bypass test below proves nothing."""
    client = build(bypass)
    assert _first_429(client, {"x-e2e-test": "true"}) == 121, (
        "slowapi's 120/minute default no longer applies to the probe path"
    )


def test_the_bypass_lets_e2e_traffic_past_the_default_limit(build) -> None:
    client = build("1")
    assert _first_429(client, {"x-e2e-test": "true"}) is None, (
        "GDX_E2E_BYPASS=1 with x-e2e-test still hit slowapi's 120/minute cap (#579)"
    )


def test_the_bypass_does_not_need_the_header(build) -> None:
    """The ruled trade-off, pinned so it stays a decision: the env var alone
    turns slowapi off. Only throwaway e2e containers and the lab set it."""
    client = build("1")
    assert _first_429(client, {}) is None


def test_a_later_build_without_the_bypass_turns_the_limiter_back_on(build) -> None:
    """`limiter` is process-global. An implementation that only ever sets it
    False would leave rate limiting off for every app built afterwards."""
    build("1")
    client = build(None)
    assert _first_429(client, {"x-e2e-test": "true"}) == 121


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, True),
        ({"GDX_E2E_BYPASS": "1"}, False),
        ({"RATELIMIT_ENABLED": "false"}, False),
    ],
    ids=["default", "e2e-bypass", "slowapi-own-off-switch"],
)
def test_the_real_module_level_app_honours_both_switches(env, expected) -> None:
    """A fresh interpreter importing the app, the way uvicorn builds it.

    RATELIMIT_ENABLED=false is slowapi's own off switch, read when the Limiter
    is built. The bypass has to combine with it, not overwrite it: an
    unconditional `enabled = bypass != "1"` silently turned it back on.
    """
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    child_env = {k: v for k, v in os.environ.items() if k not in ("GDX_E2E_BYPASS", "RATELIMIT_ENABLED")}
    child_env.update(env)
    out = subprocess.run(
        [sys.executable, "-c", "import gdx_dispatch.app as a; print(a.limiter.enabled)"],
        capture_output=True, text=True, timeout=120, cwd=str(repo), env=child_env,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == str(expected), (env, out.stdout[-500:])
