"""GDXA-988: a rate-limit off switch must refuse to boot in a production-like env.

GDX_E2E_BYPASS=1 turns slowapi off and lets any request carrying the
client-supplied ``x-e2e-test: true`` header skip the Redis limiter, so the env
var is the whole protection. RATELIMIT_ENABLED=false is slowapi's own off
switch. Before this, nothing stopped either from reaching a production process.

The boot tests run a fresh interpreter importing ``gdx_dispatch.app`` — the
module-level ``app = create_app()`` uvicorn loads — with the pytest markers
stripped from its environment, so the gate's pytest exemption cannot hide a
missing refusal. The control arms boot the same way and must come up, which is
what proves the refusal arms measure the gate and not a broken import.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_STRIPPED = ("GDX_ENV", "GDX_E2E_BYPASS", "RATELIMIT_ENABLED", "PYTEST_VERSION", "PYTEST_CURRENT_TEST")


def _boot(env: dict[str, str]) -> subprocess.CompletedProcess:
    from cryptography.fernet import Fernet

    child_env = {k: v for k, v in os.environ.items() if k not in _STRIPPED}
    child_env.setdefault("JWT_SECRET", "test-jwt-secret-at-least-32-bytes-long-for-hs256-sha256-safety")
    # core/pii.py refuses at import in production without it — a different
    # refusal that would satisfy the returncode check for the wrong reason.
    child_env.setdefault("MASTER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    child_env.update(env)
    return subprocess.run(
        [sys.executable, "-c", "import gdx_dispatch.app; print('BOOTED')"],
        capture_output=True, text=True, timeout=180, cwd=str(REPO), env=child_env,
    )


@pytest.mark.parametrize(
    "env",
    [
        {"GDX_E2E_BYPASS": "1"},
        {"GDX_E2E_BYPASS": "1", "GDX_ENV": "production"},
        {"GDX_E2E_BYPASS": "1", "GDX_ENV": "staging"},
        {"RATELIMIT_ENABLED": "false", "GDX_ENV": "production"},
    ],
    ids=["bypass-env-unset", "bypass-production", "bypass-unknown-env", "slowapi-off-production"],
)
def test_a_rate_limit_off_switch_refuses_to_boot_in_production(env) -> None:
    out = _boot(env)
    assert out.returncode != 0, f"booted with {env}: {out.stdout[-500:]}"
    assert "REFUSING TO BOOT: rate limiting is switched off" in out.stderr, out.stderr[-2000:]
    assert "BOOTED" not in out.stdout


@pytest.mark.parametrize(
    "env",
    [
        {"GDX_E2E_BYPASS": "1", "GDX_ENV": "dev"},
        {"GDX_E2E_BYPASS": "0", "GDX_ENV": "production"},
        {"GDX_ENV": "production"},
    ],
    ids=["e2e-container-dev", "prod-value-0", "prod-bypass-unset"],
)
def test_the_same_boot_comes_up_when_it_should(env) -> None:
    out = _boot(env)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "BOOTED"
    assert "REFUSING TO BOOT" not in out.stderr


@pytest.mark.parametrize(
    ("env", "configured_enabled", "refused"),
    [
        ({"GDX_E2E_BYPASS": "1"}, True, True),
        ({"GDX_E2E_BYPASS": "1", "GDX_ENV": " Production "}, True, True),
        ({"GDX_E2E_BYPASS": "1", "GDX_ENV": "prod-eu"}, True, True),
        ({"GDX_ENV": "production"}, False, True),
        ({"GDX_E2E_BYPASS": "1", "GDX_ENV": "DEV"}, True, False),
        ({"GDX_E2E_BYPASS": "1", "GDX_ENV": "test"}, False, False),
        ({"GDX_E2E_BYPASS": "true", "GDX_ENV": "production"}, True, False),
        ({}, True, False),
    ],
)
def test_the_refusal_matrix(env, configured_enabled, refused) -> None:
    """The decision itself, every arm. ``GDX_E2E_BYPASS=true`` is not refused
    because nothing reads it as on: both readers compare against ``"1"``."""
    from gdx_dispatch.app import _security_bypass_refusal

    message = _security_bypass_refusal(env, configured_enabled)
    assert (message is not None) is refused, message
    if refused:
        assert message.startswith("REFUSING TO BOOT")
