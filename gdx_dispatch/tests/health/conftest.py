"""Health-suite conftest — routes drift-detector failures to the alert bus.

Every `@pytest.mark.health` test failure (in any phase — setup/call/teardown)
gets appended to the alert bus via `gdx_dispatch.tools.orchestrator.alert_bus`.

Why the shared bus module instead of inlining the write logic:
  - conftest AND the health_daemon both need the same dedup + rotation +
    loud-failure semantics. Inlining duplicates the silent-failure surface
    we're trying to eliminate.
  - A single import path means one place to audit and fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Make `gdx` importable even when pytest invokes us from an unexpected cwd.
# This is belt-and-suspenders on top of pytest.ini's `pythonpath = .`.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

# 2026-04-20: orchestrator package extracted to ../gdx-orchestrator.
# alert_bus was tied to the legacy autonomous-loop's health-alert
# routing; without the loop running there's no consumer for these
# alerts. Stubbed so the suite still loads — failures no longer route
# anywhere. If health-alert routing is needed again, repoint at the
# new alert sink.
def append_alert(*_args, **_kwargs):
    """No-op stub — orchestrator alert_bus removed 2026-04-20."""
    return False

MARKER = "health"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Route health-test failures to the alert bus.

    Covers ALL phases (setup, call, teardown) so fixture-level failures
    (e.g. DB connection dies during setup) are detected — not just
    call-phase assertion failures.

    `report.outcome` is pytest's narrow set: passed/failed/skipped. A
    failure in setup is reported as outcome="failed" with when="setup";
    pytest's summary view renders that as ERROR, but the outcome string
    is still "failed". We route on outcome=="failed" regardless of phase.

    Perf note: this write is synchronous and happens per-failure. For a
    10-detector suite where at most a few fail, that's < 5 ms overhead on
    a healthy disk — tolerated as the cost of the bus. The daemon runs
    the suite headless so the latency is fully amortized there anyway.
    """
    outcome = yield
    report = outcome.get_result()

    if report.outcome != "failed":
        return  # passed or skipped — not an alert
    if MARKER not in item.keywords:
        return  # not a health test

    # Build the body so the alert is actionable even when longrepr is None
    # (which can happen for certain internal pytest errors).
    body_parts = [f"phase={report.when}", f"nodeid={item.nodeid}"]
    if report.longrepr:
        body_parts.append(str(report.longrepr)[-400:])
    else:
        body_parts.append("(no longrepr — likely an internal pytest error)")
    body = " | ".join(body_parts)

    # append_alert returns False on unrecoverable OSError and logs to
    # stderr. We deliberately do NOT raise here — pytest's own failure
    # output is still the primary signal for the developer-facing surface;
    # the bus write is the secondary amplification.
    append_alert(
        source="health_detector",
        severity="CRITICAL",
        body=body,
        detector=item.name,
        extra={"nodeid": item.nodeid, "phase": report.when},
    )


def pytest_configure(config):
    """Fail loud if the `health` marker isn't registered in pytest.ini.

    Catches the class of silent failure where someone renames the marker
    (from `health` to something else) but forgets to update this conftest
    — today that would silently stop routing ALL tests without any error.
    """
    ini_markers = config.getini("markers")
    declared = {line.split(":")[0].strip() for line in ini_markers if line.strip()}
    if MARKER not in declared:
        print(
            f"[health_conftest] WARNING: marker {MARKER!r} not registered "
            f"in pytest.ini. Detector routing may no-op. "
            f"Registered markers: {sorted(declared)}",
            file=sys.stderr, flush=True,
        )
