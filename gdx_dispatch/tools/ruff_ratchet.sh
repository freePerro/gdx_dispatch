#!/usr/bin/env bash
# Ruff ratchet — fail when the violation count rises above .ruff_baseline.
#
# This lives in a script rather than inline in ci.yml so the gate's OWN
# failure modes are reachable from a test
# (gdx_dispatch/tests/test_ruff_ratchet_gate.py). Logic embedded in a
# workflow `run:` block cannot be tested by anything, which is how the same
# class of parsing bug shipped twice.
#
# THE SAFETY NET IS THE AFFIRMATIVE CHECK, NOT THE LIST BELOW. A count is
# trusted only when ruff actually reported one, or affirmatively reported a
# clean tree. The named branches exist to produce a useful message; they are
# not the thing keeping the gate honest. (An earlier draft of this script
# enumerated three known-bad modes and treated that as exhaustive — an audit
# then found a fourth, which is exactly the failure that framing invites.)
#
# Modes in which this step used to report success without measuring anything:
#
#   1. UNANCHORED COUNT (latent on the current rule selection, not observed
#      firing). ruff prints matching SOURCE LINES in its diagnostic snippets,
#      with two lines of context either side. A test file contains the literal
#      text "Found 2 customers." (test_ai_ask_loop_single_tool.py:98 and :116).
#      `grep -oP 'Found \K\d+'` matches such a line as readily as the summary,
#      the shell then sees two values, `[` errors with "integer expected", and
#      `if` reads that error as false. Today ruff emits no diagnostic within
#      two lines of either occurrence, so the old expression returns a single
#      value — the bug is armed, not firing. Anchoring to column 0 disarms it:
#      snippet lines carry a `116 | ` gutter, the summary starts at column 0.
#   2. RUFF ITSELF FAILING. Exit >= 2 (unwritable cache, bad config) prints no
#      summary line, and the old `|| echo 0` turned that into CURRENT=0.
#      Observed live on a developer machine 2026-09-09: a root-owned
#      .ruff_cache made ruff exit 2, and the old gate would have passed.
#   3. UNREADABLE TARGET. ruff reports E902 as an ordinary diagnostic and exits
#      1, printing "Found 1 error." — a count that sails under any baseline.
#   4. NOTHING TO CHECK. Given a path holding no Python files, ruff prints
#      "warning: No Python files found under the given path(s)" followed by
#      "All checks passed!" and exits 0. An exclude rule, a layout move or a
#      wrong target silently empties the gate.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BASELINE_FILE="${RUFF_BASELINE_FILE:-$REPO_ROOT/.ruff_baseline}"
TARGET="${RUFF_TARGET:-gdx_dispatch/}"

_fail() {
    echo "❌ $1"
    printf '%s\n' "${OUT:-}" | tail -20
    exit 1
}

BASELINE=$(cat "$BASELINE_FILE" 2>/dev/null || echo "")
case "$BASELINE" in
    '' | *[!0-9]*)
        echo "❌ ruff baseline at $BASELINE_FILE is missing or not a number"
        exit 1
        ;;
esac

# ── ruff version drift (#679 sibling sweep, 2026-09-12) ───────────────────
# The baseline is VERSION-SPECIFIC — ci.yml says so where it pins the install:
# "a ruff bump that adds/changes rules would shift the count and false-fail".
# But this script invokes a bare `ruff`, so a local run measures with whatever
# is on PATH against a baseline calibrated for CI's pin. Measured 2026-09-12:
# CI pins 0.15.18, this machine had 0.15.8.
#
# The risk is asymmetric and that is why this warns rather than refuses: an
# OLDER ruff knows FEWER rules, so it reports FEWER violations, so a local run
# can read "under baseline" on a tree CI will fail. A false green, which is the
# one outcome a ratchet must never produce silently.
#
# Not a hard failure: a version mismatch makes the number untrustworthy, not
# wrong, and refusing would block a legitimate local run for a cosmetic reason.
# The pin is read FROM ci.yml so there is no second copy to drift.
CI_WORKFLOW="$REPO_ROOT/.github/workflows/ci.yml"
if [ -f "$CI_WORKFLOW" ]; then
    # Every part of this probe is non-fatal by construction. `set -euo pipefail`
    # is in force, so a bare command substitution that exits non-zero would kill
    # the script — and `ruff` here may be a STUB: test_ruff_ratchet_gate.py
    # replays a scenario for any argv it does not recognise, so `ruff --version`
    # can return "Found 3 errors." with rc 1. Hence `|| true` on both pipelines
    # and a strict version-shape test before comparing: anything that is not
    # X.Y.Z is treated as "cannot tell", not as drift.
    PINNED=$(grep -oE 'pip install ruff==[0-9]+\.[0-9]+\.[0-9]+' "$CI_WORKFLOW" 2>/dev/null | head -1 | sed 's/.*ruff==//' || true)
    LOCAL=$(ruff --version 2>/dev/null | awk '{print $2}' || true)
    if printf '%s' "$LOCAL" | grep -qvE '^[0-9]+\.[0-9]+\.[0-9]+$'; then
        LOCAL=""
    fi
    if [ -n "$PINNED" ] && [ -n "$LOCAL" ] && [ "$PINNED" != "$LOCAL" ]; then
        echo "⚠ ruff version drift: measuring with $LOCAL, baseline calibrated for $PINNED (ci.yml)."
        echo "  An older ruff knows fewer rules — this count can read GREEN on a tree CI fails."
        echo "  Match CI before trusting a pass:  pip install ruff==$PINNED"
    fi
fi

RC=0
OUT=$(ruff check "$TARGET" 2>&1) || RC=$?

if [ "$RC" -ge 2 ]; then
    _fail "ruff itself failed (exit $RC) — the ratchet measured nothing:"
fi
if printf '%s\n' "$OUT" | grep -qE '^E902'; then
    _fail "ruff could not read its target (E902) — the ratchet measured nothing:"
fi
if printf '%s\n' "$OUT" | grep -qE '^warning: No Python files found'; then
    _fail "ruff found no Python files under '$TARGET' — the ratchet measured nothing:"
fi

CURRENT=$(printf '%s\n' "$OUT" | grep -oP '^Found \K\d+' || true)

if [ -z "$CURRENT" ]; then
    # No summary line. The ONLY acceptable reason is ruff affirmatively
    # reporting a clean tree; every other silence is an unmeasured run.
    if printf '%s\n' "$OUT" | grep -qxF 'All checks passed!'; then
        CURRENT=0
    else
        _fail "ruff reported no violation count and no clean tree — refusing to pass:"
    fi
fi

case "$CURRENT" in
    '' | *[!0-9]*)
        _fail "violation count from ruff is not a number ('$CURRENT') — refusing to pass:"
        ;;
esac

echo "ruff: baseline=$BASELINE current=$CURRENT"
if [ "$CURRENT" -gt "$BASELINE" ]; then
    echo "❌ Ruff violations increased ($CURRENT > $BASELINE)"
    ruff check "$TARGET" --statistics | tail -20
    exit 1
fi

# Hard-fail on syntax / undefined-name errors regardless of the baseline.
ruff check "$TARGET" --select F821,F823 --quiet
