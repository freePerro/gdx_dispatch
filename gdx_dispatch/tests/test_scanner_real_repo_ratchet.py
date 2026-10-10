"""Each report-only scanner runs on the REAL tree, against a frozen baseline.

GDXA-404: ``drift_scanner``, ``silent_failure_scanner``, ``comment_drift_scan``
and ``frontend_contract_scan`` were tested only on ``tmp_path`` fixtures. No
default-suite test and no CI step ran any of them on the repo, so all four
exited 1 on main (326 / 94 / 76 / 5 findings) with every gate green.

These tests fail when the tree and the baseline differ in either direction: a
new finding, or a baseline entry the tree no longer produces. Re-freezing over
a NEW finding is the one wrong answer: fix it, or mark it in source where the
scanner supports a marker. Re-freezing after a FIX is required, in the same
change, and lowers the pinned count below.
"""
from __future__ import annotations

from collections import Counter

import pytest

from gdx_dispatch.tools import scanner_baseline as sb
from gdx_dispatch.tools import silent_failure_scanner

# Entries in each baseline when frozen on main (GDXA-404, 2026-10-09), pinned
# EXACTLY, not as a ceiling (a ceiling rots: test_authz_route_sweep.py). Lower
# a count in the same change that fixes a finding; a raised count means
# someone re-froze over a new finding, and review sees it here.
BASELINE_CEILING = {
    "drift_scanner": 326,
    "silent_failure_scanner": 94,
    "comment_drift_scan": 76,
    "frontend_contract_scan": 4,
}


def test_every_scanner_has_a_ceiling():
    assert set(BASELINE_CEILING) == set(sb.SCANNERS)


@pytest.mark.parametrize("name", sorted(sb.SCANNERS))
def test_baseline_count_is_pinned(name):
    n = sum(sb.load_baseline(sb.SCANNERS[name].baseline).values())
    assert n == BASELINE_CEILING[name], (
        f"{sb.SCANNERS[name].baseline.name} has {n} entries, pinned at "
        f"{BASELINE_CEILING[name]}: lower the pin if findings were fixed; a "
        f"raise means the baseline was re-frozen over new findings"
    )


@pytest.mark.parametrize("name", sorted(sb.SCANNERS))
def test_no_new_findings_on_the_real_tree(name):
    hits = sb.run(name)
    # A scan that silently finds nothing would pass the ratchet vacuously.
    # Every one of these had findings on main when frozen; the day one reaches
    # zero, lower its ceiling to 0 and drop this guard for it.
    assert hits, f"{name} returned no findings at all: did it scan anything?"
    new = sb.new_hits(hits, sb.load_baseline(sb.SCANNERS[name].baseline))
    assert not new, (
        f"{name}: {len(new)} finding(s) not in {sb.SCANNERS[name].baseline.name}:\n  "
        + "\n  ".join(h.shown for h in new[:40])
        + "\nFix them. Re-freezing the baseline to make this pass is the wrong answer."
    )
    stale = sb.stale_entries(hits, sb.load_baseline(sb.SCANNERS[name].baseline))
    assert not stale, (
        f"{name}: {sum(stale.values())} baseline entr(ies) the tree no longer "
        f"produces — fixed (good: re-freeze with `python -m "
        f"gdx_dispatch.tools.scanner_baseline --write {name}` and lower "
        f"BASELINE_CEILING), or the scanner stopped scanning something:\n  "
        + "\n  ".join(sorted(stale)[:40])
    )


# ── the ratchet itself can go red ───────────────────────────────────────────

def _hit(file="gdx_dispatch/x.py", code="C", text="t"):
    return sb.Hit(file, code, text, f"{file} {code}")


def test_new_hits_counts_duplicates():
    """A third copy of a twice-baselined shape is new; moving lines is not."""
    a = _hit()
    baseline = Counter({a.signature: 2})
    assert sb.new_hits([a, a], baseline) == []
    assert sb.new_hits([a, a, a], baseline) == [a]


def test_a_fix_and_a_narrowed_scan_both_read_as_stale():
    """Audit, GDXA-404: with only a new-hits check, a fixed finding stayed as
    budget and a scanner that dropped most of its tree stayed green."""
    a, b = _hit(text="a"), _hit(text="b")
    baseline = Counter({a.signature: 2, b.signature: 1})
    assert sb.stale_entries([a, a, b], baseline) == Counter()
    assert sb.stale_entries([a, b], baseline) == Counter({a.signature: 1})
    assert sb.stale_entries([a], baseline) == Counter({a.signature: 1, b.signature: 1})


def test_signature_ignores_line_numbers_and_keys_on_content():
    assert _hit(text="x").signature == _hit(text="x").signature
    assert _hit(text="x").signature != _hit(text="y").signature
    assert "|" in _hit().signature and _hit().signature.startswith("gdx_dispatch/x.py|C|")


def test_untracked_files_do_not_count(tmp_path):
    """No .git: every hit is kept (tmp repos). With an index: only tracked
    files, and a file-less ``-`` hit always survives."""
    hits = [_hit(file="a.py"), _hit(file="-")]
    assert sb.tracked_only(hits, tmp_path) == hits
    real = sb.tracked_only([_hit(file="no/such/untracked_file.py"), _hit(file="-")])
    assert [h.file for h in real] == ["-"]


def test_drift_finding_on_the_real_tree_reads_as_new(tmp_path, monkeypatch):
    """End to end on drift_scanner: a real violating file under a scanned
    directory produces a hit the baseline does not hold."""
    from gdx_dispatch.tools import drift_scanner

    routers = tmp_path / "gdx_dispatch" / "routers"
    routers.mkdir(parents=True)
    (routers / "planted.py").write_text(
        "def f():\n    try:\n        g()\n    except Exception:\n        pass\n"
    )
    gdx = tmp_path / "gdx_dispatch"
    for attr, value in {
        "REPO_ROOT": tmp_path, "GDX_DIR": gdx, "ROUTERS_DIR": routers,
        "CORE_DIR": gdx / "core", "REQUIREMENTS": gdx / "requirements.txt",
        "APP_PY": gdx / "app.py",
    }.items():
        monkeypatch.setattr(drift_scanner, attr, value)
    monkeypatch.setattr(sb, "REPO_ROOT", tmp_path)
    hits = sb._drift_scanner()
    planted = [h for h in hits if h.file == "gdx_dispatch/routers/planted.py"]
    assert planted, hits
    assert sb.new_hits(planted, sb.load_baseline(sb.SCANNERS["drift_scanner"].baseline)) == planted


def test_silent_failure_report_is_written_outside_the_repo():
    """The default --json used to be ai-queue/rd/operations/ inside the tree,
    so every run left an untracked file in the checkout."""
    report = silent_failure_scanner.REPORT_PATH.resolve()
    assert not report.is_relative_to(sb.REPO_ROOT.resolve()), report


def test_silent_failure_repo_root_needs_no_ai_queue():
    assert silent_failure_scanner.REPO_ROOT == sb.REPO_ROOT
