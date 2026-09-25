"""Guard: every source file has an owning subagent, and every owner is real.

Companion to ``gdx_dispatch/tools/agent_ownership_scan.py`` and the map it
reads, ``gdx_dispatch/tools/agent_ownership.txt``. Decided 2026-09-24
(``domain-agents-plan`` under ``docs/design/``).

Four assertions plus one that proves the scanner can go red at all — the
2026-09-01 audit of a sibling scanner found a "ratchet" that could not fail,
and this file is not going to be the next one.
"""
from __future__ import annotations

import pytest

from gdx_dispatch.tools import agent_ownership_scan as scan


@pytest.fixture(scope="module")
def report():
    return scan.scan()


def test_every_source_file_has_an_owner(report):
    """A file under a covered root matches no rule.

    Add a line to ``agent_ownership.txt`` naming the agent that owns it. Do
    not add a catch-all: the point of this test is to make you decide.
    """
    assert not report.unowned, (
        f"{len(report.unowned)} file(s) with no owning agent:\n  " + "\n  ".join(report.unowned[:30])
    )


def test_every_rule_matches_a_tracked_file(report):
    """A rule names a file that was renamed or deleted. Fix or delete the rule."""
    assert not report.dead_rules, (
        f"{len(report.dead_rules)} rule(s) match nothing:\n  " + "\n  ".join(report.dead_rules)
    )


def test_every_owner_has_an_agent_file(report):
    """The map names an owner with no ``.claude/agents/<owner>.md`` checked in."""
    assert not report.owners_without_agent, (
        "owner(s) named in the map with no agent file:\n  " + "\n  ".join(report.owners_without_agent)
    )


def test_every_agent_file_owns_something(report):
    """An agent definition exists that the map never names — a definition nobody maintains."""
    assert not report.agents_without_territory, (
        "agent file(s) with no territory in the map:\n  " + "\n  ".join(report.agents_without_territory)
    )


def test_scanner_reports_an_unowned_file():
    """The check can actually fail: an unclaimed router under a covered root is reported."""
    fake = "gdx_dispatch/routers/zz_unclaimed_router.py"
    r = scan.scan(files={fake}, rules=scan.load_rules())
    assert r.unowned == [fake]


def test_last_match_wins():
    rules = [
        ("gdx_dispatch/frontend/src/views/Mobile*.vue", "mobile-tech"),
        ("gdx_dispatch/frontend/src/views/MobileInboxView.vue", "comms-email-phone"),
    ]
    assert scan.owner_of("gdx_dispatch/frontend/src/views/MobileInboxView.vue", rules) == "comms-email-phone"
    assert scan.owner_of("gdx_dispatch/frontend/src/views/MobileJobsView.vue", rules) == "mobile-tech"
    assert scan.owner_of("gdx_dispatch/frontend/src/views/JobsView.vue", rules) is None


def test_tests_and_package_markers_are_not_covered():
    assert not scan.is_covered("gdx_dispatch/routers/__init__.py")
    assert not scan.is_covered("gdx_dispatch/frontend/src/views/__tests__/JobsView.spec.js")
    assert not scan.is_covered("gdx_dispatch/frontend/src/utils/dates.spec.js")
    assert not scan.is_covered("gdx_dispatch/tests/test_jobs.py")
    assert scan.is_covered("gdx_dispatch/routers/jobs.py")
    assert scan.is_covered("gdx_dispatch/frontend/src/views/JobsView.vue")
