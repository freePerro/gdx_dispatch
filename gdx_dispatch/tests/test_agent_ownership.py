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


def test_scanner_reports_an_unowned_root_baseline():
    """A new repo-root ratchet baseline with no rule is reported (GDXA-142).

    Until 2026-09-27 coverage was ``COVERED_ROOTS`` alone — 11 prefixes, all
    under ``gdx_dispatch/`` — so a root path never reached the ownership
    assertion and six tracked ratchet baselines sat UNOWNED while this gate
    stayed green. This is the input that turns it red.
    """
    fake = ".zz_unclaimed_baseline"
    r = scan.scan(files={fake}, rules=scan.load_rules())
    assert r.unowned == [fake]


def test_root_coverage_reaches_baselines_and_stops_there():
    """Root coverage is glob-limited: ratchets in, the maintainer's files out."""
    assert scan.is_covered(".ruff_baseline")
    assert scan.is_covered(".tenant_plane_redundant_filter_baseline")
    assert scan.is_covered(".semgrepignore")
    # The root's project files belong to the maintainer, not to any one agent,
    # so an empty COVERED_ROOTS prefix was the wrong shape.
    assert not scan.is_covered("README.md")
    assert not scan.is_covered("LICENSE")
    # `*` crosses `/` in fnmatch, so the globs must be anchored to the root.
    assert not scan.is_covered("docs/design/some_baseline")


def test_every_root_rule_is_gated_or_recorded_as_ungated(report):
    """A root rule landed in the ungated half without anyone saying so.

    Either add a glob to ``COVERED_ROOT_GLOBS`` so the rule is enforced, or add
    the path to ``DOCUMENTED_ONLY_ROOT_RULES`` to say out loud that it is not.
    Silently ungated is how six ratchet baselines stayed unclaimed (GDXA-142).
    """
    assert not report.ungated_root_rules, (
        f"{len(report.ungated_root_rules)} repo-root rule(s) neither gated nor recorded:\n  "
        + "\n  ".join(report.ungated_root_rules)
    )


def test_an_accidentally_ungated_root_rule_is_reported():
    """The check above can fail: a root rule matching no covered glob is caught."""
    rules = [*scan.load_rules(), (".dockerignore", "platform-core")]
    r = scan.scan(files={".dockerignore"}, rules=rules)
    assert r.ungated_root_rules == [".dockerignore"]
    # ...and a rule that *is* gated, or is on the documented-only list, is not.
    assert ".ruff_baseline" not in r.ungated_root_rules
    assert "conftest.py" not in r.ungated_root_rules


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
