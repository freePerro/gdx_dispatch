#!/usr/bin/env bash
# Ruff ratchet — fail when the violation count rises above .ruff_baseline,
# and lower the baseline when it falls (see 'the ratchet half' below).
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
TARGET_DEFAULT="gdx_dispatch/"
TARGET="${RUFF_TARGET:-$TARGET_DEFAULT}"

# Whether the COUNT describes the whole tree, which is the only thing that
# decides if it may be written back as a baseline. A narrowed RUFF_TARGET makes
# it a partial measurement; a redirected RUFF_BASELINE_FILE does not — that
# just says where to keep the number, and is how the gate's own tests exercise
# this without touching the repo's real baseline.
NARROWED=0
[ "$TARGET" != "$TARGET_DEFAULT" ] && NARROWED=1

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

# ── the ratchet half (Doug, 2026-09-12) ───────────────────────────────────
#
# Until now this was a one-way CEILING: it failed on an increase and did
# nothing on a decrease, and no code path anywhere wrote .ruff_baseline. Every
# cleanup banked headroom instead of locking it in — measured on merged main
# 2026-09-12, baseline 980 against a real count of 974, six violations of free
# slack a future regression could spend while the gate stayed green. That is
# the "a green gate proves nothing unless it can fail for your defect" class
# this repo keeps finding (#454, #679, #716): the gate was named for a
# behaviour it did not have.
#
# This runs LAST, after the F821/F823 hard gate. An earlier draft wrote the
# baseline before it, so a run that ultimately FAILED still banked a lower
# number — a failing gate must never move the bar.
#
# Lowering can only make the gate stricter, so the write is safe in kind. The
# danger is writing a number that does not describe the committed tree, which
# would red CI for everyone. Hence:
#
#   * the ruff that measured it matches CI's pin. An OLDER ruff knows fewer
#     rules and reports FEWER violations, so its count would set a baseline
#     CI can never meet.
#   * the working tree is CLEAN. This is the guard the first draft missed and
#     the one most likely to fire in real use: a lint gate is normally run
#     mid-edit, and an uncommitted `# ruff: noqa` was measured taking the
#     baseline from 980 to 937. The count has to describe the tree that is
#     actually committed, not the one on disk at the moment.
#   * RUFF_TARGET is not narrowed, so the count covers the whole tree.
#     (RUFF_BASELINE_FILE may be redirected — that only moves where the number
#     is kept, which is how this behaviour is tested.)
#   * RUFF_RATCHET_NO_WRITE is unset. ANY non-empty value suppresses; the
#     first draft compared against the literal "1", so `=true` still wrote.
#
# Where this actually fires: a developer run with the pinned ruff on a clean
# tree, which leaves .ruff_baseline modified for them to commit. On a CI runner
# the write is real but the file is discarded at the end of the job, so CI
# cannot lock a gain in by itself — it reports the slack instead, and someone
# has to run this locally and commit the number. Making CI *fail* on slack
# would close that loop; that is a policy call, not a mechanical one.
if [ "$CURRENT" -lt "$BASELINE" ]; then
    DIRTY=""
    if command -v git >/dev/null 2>&1 && git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
        DIRTY=$(git -C "$REPO_ROOT" status --porcelain -- . ':!.ruff_baseline' 2>/dev/null || true)
    fi

    if [ -n "${RUFF_RATCHET_NO_WRITE:-}" ]; then
        echo "   (would lower to $CURRENT — suppressed by RUFF_RATCHET_NO_WRITE)"
    elif [ "$NARROWED" = "1" ]; then
        echo "   (would lower to $CURRENT — not writing: RUFF_TARGET is '$TARGET', so this"
        echo "    count does not describe the whole tree)"
    elif [ -z "${LOCAL:-}" ] || [ -z "${PINNED:-}" ] || [ "${LOCAL:-}" != "${PINNED:-}" ]; then
        echo "   (would lower to $CURRENT — not writing: measured with ruff ${LOCAL:-unknown},"
        echo "    baseline is calibrated for ${PINNED:-unknown}. A lower count from an older ruff"
        echo "    would set a baseline CI cannot meet. Run:  pip install ruff==${PINNED:-<ci pin>})"
    elif [ -n "$DIRTY" ]; then
        echo "   (would lower to $CURRENT — not writing: working tree is dirty, so this count"
        echo "    describes uncommitted edits rather than the committed tree. Commit, then re-run.)"
    else
        printf '%s\n' "$CURRENT" > "$BASELINE_FILE"
        echo "🔒 ratchet: baseline lowered $BASELINE -> $CURRENT"
        echo "   Commit .ruff_baseline with this change, or the gain is not kept."
    fi
fi
