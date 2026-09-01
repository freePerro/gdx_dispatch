"""Guard: docs must not grow new references to files that do not exist.

Companion to ``gdx_dispatch/tools/doc_link_scan.py``.

Three things are asserted, and the second is the one a sibling scanner got
wrong. On 2026-09-01 an adversarial audit of a PII scanner in this repo found
its "ratchet" had **nothing enforcing direction** — the auditor added a
violation, re-froze the baseline, and CI went green. Calling a baseline a
ratchet does not make it one. So:

1. No dead reference outside the baseline.
2. **The baseline may only shrink.** Enforced here, and independently by
   ``--write`` refusing to write a larger file.
3. The scanner can actually detect a dead reference — a check that cannot go
   red is decoration.
"""
from __future__ import annotations

from gdx_dispatch.tools import doc_link_scan as scan


def test_no_new_dead_doc_references():
    """A doc names a file that does not resolve, and it is not baselined.

    Fix the path, delete the reference, or — if it is a placeholder or lives
    outside this repo — put ``link-ok`` on the line. Re-freezing the baseline
    is the wrong answer and ``--write`` will refuse it.
    """
    baseline = scan.load_baseline()
    new = [h for h in scan.scan() if h not in baseline]
    assert not new, (
        f"{len(new)} dead doc reference(s) not in the baseline:\n  "
        + "\n  ".join(h.replace("|", ": ") for h in new[:20])
    )


def test_baseline_only_shrinks():
    """The ratchet, enforced rather than asserted in prose.

    Without this, `--write` is a one-command escape from every finding above.
    """
    live = len(scan.scan())
    frozen = len(scan.load_baseline())
    assert live <= frozen, (
        f"{live} dead references against a baseline of {frozen}. The baseline is a "
        "ratchet: it may only fall. Fix the reference rather than re-freezing."
    )


# --------------------------------------------------------------------------
# Counterfactual — prove the detector works
# --------------------------------------------------------------------------
def test_detector_flags_a_path_that_does_not_resolve():
    assert not scan.resolves("gdx_dispatch/tools/totally_invented_tool.py")


def test_detector_accepts_a_path_that_does():
    assert scan.resolves("gdx_dispatch/tools/doc_link_scan.py")
    assert scan.resolves("CLAUDE.md")


def test_detector_resolves_by_basename_anywhere():
    """A doc citing a bare filename should not be called dead just because it
    omitted the directory."""
    assert scan.resolves("doc_link_scan.py")


def test_foreign_and_placeholder_paths_are_not_counted():
    """`ai-queue/` and friends live outside this repo by design; placeholders
    are never meant to resolve. Counting them would bury the real findings."""
    for ref in ("ai-queue/rd/latest_report.md", "plans/whatever.md",
                "~/Desktop/thing.md", "your_module.py", "path/to/file.py"):
        assert not scan.interesting(ref), ref


def test_real_looking_paths_are_counted():
    for ref in ("gdx_dispatch/tools/pollution_check.py", "ARCHITECTURAL_STATE.md"):
        assert scan.interesting(ref), ref


def test_link_ok_escape_hatch_is_honoured():
    assert scan.SKIP_LINE.search("see `gdx_dispatch/tools/gone.py`  <!-- link-ok -->")
    assert not scan.SKIP_LINE.search("see `gdx_dispatch/tools/gone.py`")
