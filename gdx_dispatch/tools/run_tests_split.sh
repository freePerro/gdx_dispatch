#!/usr/bin/env bash
# Run the pytest suite in N parallel pytest-split shards.
# Each shard is a fully independent pytest invocation — no xdist worker
# protocol, no shared fixtures across shards. Replaces `pytest -n N`.
#
# Usage:
#   gdx_dispatch/tools/run_tests_split.sh [pytest args]
#   N=4 gdx_dispatch/tools/run_tests_split.sh gdx_dispatch/tests/test_auth_*.py
#   PYTEST="docker run --rm --entrypoint python -e JWT_SECRET=<32+ bytes> \
#     -v $PWD:/app -w /app docker-app -m pytest" gdx_dispatch/tools/run_tests_split.sh
#
# Sweet spot from 2026-04-24 benchmark: N=7 on this laptop (14 cores).
# Beyond ~7 the per-process startup tax outpaces the parallelism gain.
#
# 2026-08-04 rewrite (assessment §7 item 3): the old script wiped the entire
# inherited addopts line (`-o addopts=`) to strip a long-gone `-n`/`--dist`.
# That also dropped the `-m "not e2e and not load and not health"` marker
# filter AND `-p no:schemathesis_xdist` — so a bare run collected tests/e2e/,
# whose test_schemathesis.py makes a NETWORK CALL at import time. addopts is
# now inherited from pytest.ini untouched; the explicit --ignore below is
# load-bearing (the marker filter alone runs after collection/import).

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

# ── committed-baseline scans: the fast pre-check (GDXA-408/410) ─────────────
#   run_tests_split.sh --scans [pytest args]     run only the baseline scan tests
#   run_tests_split.sh --scans --list            print which files those are
#   run_tests_split.sh --refreeze-baselines [--allow-new]
# Run both after `git merge origin/main` and BEFORE the matrix. The line-keyed
# `.tenant_plane_redundant_filter_baseline` and `.duplicate_block_baseline`
# were the matrix red on at least eight agent issues (GDXA-291 … 380), each a
# ~7 min round trip for what --scans says in ~25 s. The file list comes from
# tools/baseline_scan_tests.py, which derives it from the test files, so a new
# baseline scan joins without an edit here.
# --refreeze-baselines re-freezes both baselines in their `--baseline` mode,
# which admits only shrinkage and line shifts and REFUSES growth (exit 2). It
# never passes --allow-new on its own: that flag blesses new clones / filters,
# so it is forwarded only when the caller types it.
MODE=matrix
case "${1:-}" in
  --scans) MODE=scans; shift ;;
  --refreeze-baselines) MODE=refreeze; shift ;;
esac

if [ "$MODE" = "refreeze" ]; then
  allow=()
  for arg in "$@"; do
    case "$arg" in
      --allow-new) allow=(--allow-new) ;;
      *) echo "✗ --refreeze-baselines takes only --allow-new (got '$arg')"; exit 2 ;;
    esac
  done
  # Host python on purpose: both scanners are stdlib-only, read the git index
  # natively (no gitdir mount to get wrong), and write files the caller owns.
  # REFREEZE_PYTHON exists so tests/test_baseline_scan_tests.py can prove the
  # argv without rewriting the real baselines.
  py="${REFREEZE_PYTHON:-python3}"
  # Each scanner runs whatever the other did, so one refusal leaves the other
  # baseline re-frozen. Exit 2 from a scanner is its growth refusal; anything
  # else is a crash, and --allow-new would crash the same way.
  refused=0
  crashed=0
  for scanner in duplicate_block_scan tenant_plane_redundant_filter_scan; do
    echo "── $scanner --baseline ${allow[*]}"
    $py -m "gdx_dispatch.tools.$scanner" --baseline "${allow[@]}"
    rc=$?
    if [ "$rc" -eq 2 ]; then
      echo "✗ $scanner refused growth (exit 2) — its baseline is unchanged"
      refused=1
    elif [ "$rc" -ne 0 ]; then
      echo "✗ $scanner crashed (exit $rc) — its baseline is unchanged"
      crashed=1
    fi
  done
  if [ "$crashed" -ne 0 ]; then
    echo
    echo "CRASHED — read the traceback above; --allow-new will not get past it."
    exit 1
  fi
  if [ "$refused" -ne 0 ]; then
    echo
    echo "REFUSED — fix what grew, or re-run with --allow-new only for what you have read and mean to keep."
    exit 2
  fi
  echo
  echo "Re-frozen. Review \`git diff -- .duplicate_block_baseline .tenant_plane_redundant_filter_baseline\`, then $0 --scans"
  exit 0
fi

if [ "$MODE" = "scans" ]; then
  scan_root=()
  if [ -n "${SCANS_TESTS_ROOT:-}" ]; then
    scan_root=(--root "$SCANS_TESTS_ROOT")
  fi
  if ! scan_files="$(python3 -I "$REPO_ROOT/gdx_dispatch/tools/baseline_scan_tests.py" "${scan_root[@]}")"; then
    echo "✗ could not derive the baseline scan test list"
    exit 2
  fi
  if [ "${1:-}" = "--list" ]; then
    echo "$scan_files"
    exit 0
  fi
  mapfile -t SCAN_FILES <<< "$scan_files"
fi

N="${N:-7}"
LOG_DIR="${LOG_DIR:-/tmp/gdx_split}"
mkdir -p "$LOG_DIR"

# Resolve a Python that can actually run the suite. There is usually NO host
# venv for this repo — deps live in the docker-app image (see the PYTEST
# docker example above and docs). Order: explicit $PYTEST > .venv > python3.
if [ -z "${PYTEST:-}" ]; then
  if [ "$MODE" = "scans" ] && [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
    # A pre-check nobody runs because it needs a 150-character env var is no
    # pre-check: --scans defaults to the docker-app image the matrix uses.
    PYTEST="docker run --rm --entrypoint python -e PYTHONPATH=/app -e JWT_SECRET=test-secret-key-at-least-32-bytes-long-x -v $REPO_ROOT:/app -w /app docker-app -m pytest"
  elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTEST="$REPO_ROOT/.venv/bin/python -m pytest"
  else
    PYTEST="python3 -m pytest"
  fi
fi
# ── linked worktree: let a docker PYTEST read the git index ──────────────
# In a `git worktree add` checkout `.git` is a FILE pointing at
# <main>/.git/worktrees/<name>, which sits outside the `-v $PWD:/app` mount.
# tools/tracked_files.py follows that pointer, so inside the container the
# tracked-set guards (tracked_files, doc_link_scan, saas_surfaces_retired,
# tenant_plane_redundant_filter, agent_ownership) raise TrackedFilesUnavailable:
# 14 red tests on every Paperclip worktree (2026-09-24), which cost each run a
# second matrix on a copied tree just to prove them environmental. Mounting the
# gitdir read-only at its own host path is enough — verified 2026-09-24,
# test_tracked_files.py went 4 failed → 8 passed on a Paperclip worktree.
if [ -f "$REPO_ROOT/.git" ] && [[ "$PYTEST" == *"docker run"* ]]; then
  GITDIR="$(sed -n 's/^gitdir: *//p' "$REPO_ROOT/.git")"
  case "$GITDIR" in /*) ;; *) GITDIR="$(cd "$REPO_ROOT" && realpath "$GITDIR")" ;; esac
  if [ -d "$GITDIR" ] && [[ "$PYTEST" != *"$GITDIR"* ]]; then
    PYTEST="${PYTEST/docker run/docker run -v $GITDIR:$GITDIR:ro}"
    echo "linked worktree: mounted $GITDIR read-only so the tracked-set guards can read the index"
  fi
fi
# ── docker PYTEST: keep the container's /tmp in RAM ─────────────────────────
# Tests build file-backed SQLite DBs under tmp_path (/tmp/pytest-of-appuser/…).
# Without this, the container's /tmp is overlayfs on the host disk, and every
# SQLite commit waits on an fsync — so a shard's speed depends on how fast the
# host drive syncs, not on the tests. Measured 2026-10-05:
#   test_mobile_job_clock.py (13 tests) .... 138 s on disk, 3.5 s on tmpfs
#   shard 4 of 7 ............................ ~14 min on disk, ~1 min on tmpfs
# CI is unaffected: it runs pytest directly, not this script or docker.
# TMPFS_TMP=0 opts out.
# size is a ceiling, not a reservation: pages are used only as files are
# written. It must clear session_recorder's 20 GB free-space refusal, or 11
# test_session_recorder.py tests degrade (4g did exactly that, 2026-10-05).
if [ "${TMPFS_TMP:-1}" = "1" ] && [[ "$PYTEST" == *"docker run"* ]] && [[ "$PYTEST" != *"--tmpfs"* ]]; then
  PYTEST="${PYTEST/docker run/docker run --tmpfs /tmp:rw,exec,size=32g}"
fi

# --version alone isn't enough — a host pytest without the app's deps fails
# every shard with usage errors. Probe the actual imports the suite needs.
# Only possible when $PYTEST is the "<python> -m pytest" form (stripping the
# suffix yields a python we can hand `-c`); for a bare pytest binary or other
# shapes, fall back to --version so we don't misparse (`pytest -c` reads a
# CONFIG FILE, not code — probing that way would fail with a misleading error).
if [[ "$PYTEST" == *" -m pytest" ]]; then
  PYBIN="${PYTEST% -m pytest}"
  if ! $PYBIN -c "import pytest, fastapi, pytest_split" >/dev/null 2>&1; then
    echo "✗ '$PYTEST' lacks the suite's dependencies (pytest/fastapi/pytest-split)."
    echo "  This repo usually has no host venv — run inside the docker-app image:"
    echo "  PYTEST=\"docker run --rm --entrypoint python -e JWT_SECRET=<32+ bytes> -v \$PWD:/app -w /app docker-app -m pytest\" $0"
    exit 2
  fi
elif ! $PYTEST --version >/dev/null 2>&1; then
  echo "✗ '$PYTEST' cannot run pytest at all. See the docker-app example in the header."
  exit 2
fi

# ── dependency drift gate (#679) ───────────────────────────────────────────
# The image bakes gdx_dispatch/requirements.txt at BUILD time; the working tree
# is bind-mounted over /app at RUN time. So source is always current and deps
# never are: add a dependency, and it is absent from the harness until someone
# rebuilds, with nothing to tell you. The failure mode is a COLLECTION error
# ("ModuleNotFoundError: No module named 'freezegun'"), which takes a whole file
# out of the run and reads like noise next to a wall of passes. CI is immune —
# it pip-installs on a fresh runner — which is exactly why this is easy to miss
# on mid-stack PRs, where the local matrix is the only gate (ci.yml does not
# trigger on a PR whose base is another feature branch).
#
# `pip install --dry-run --no-index` is the whole check: --no-index keeps it
# offline (~2s, no network, no resolver round-trips), exit 0 when every
# requirement is satisfied by what is installed, exit 1 otherwise.
#
# Measured 2026-09-12 — this gate CAN fail, which is the point:
#   clean tree ............................................. exit 0
#   version drift (freezegun>=99.0 vs installed 1.5.5) ...... exit 1
#   missing package (the #679 shape) ........................ exit 1
# `pip check` was rejected as the instrument: it verifies that INSTALLED
# packages agree with each other and never reads requirements.txt, so it
# returns "No broken requirements found" / exit 0 with the defect present.
#
# SKIP_DEP_CHECK=1 bypasses it. Deliberately not silent when you do.
REQ_FILE="gdx_dispatch/requirements.txt"
if [ "${SKIP_DEP_CHECK:-0}" = "1" ]; then
  echo "⚠ dependency drift check SKIPPED (SKIP_DEP_CHECK=1)"
elif [ -n "${PYBIN:-}" ] && [ -f "$REQ_FILE" ]; then
  dep_log="$LOG_DIR/dep_drift.log"
  if $PYBIN -m pip install --dry-run --no-index -r "$REQ_FILE" > "$dep_log" 2>&1; then
    :
  else
    echo "✗ dependency drift: the test environment does not satisfy $REQ_FILE"
    echo
    grep -E "^ERROR:|No matching distribution|ResolutionImpossible" "$dep_log" | head -5 | sed 's/^/    /'
    echo
    echo "  The image bakes requirements at build time. Rebuild it:"
    echo "    docker compose -f gdx_dispatch/docker/docker-compose.yml build app"
    echo "  Full log: $dep_log   Bypass (not advised): SKIP_DEP_CHECK=1 $0"
    exit 3
  fi
else
  # Say so rather than skip in silence. `PYBIN` is only set for the
  # "<python> -m pytest" shape of $PYTEST; a bare `pytest` binary gives us no
  # interpreter to ask about its own site-packages, so the check cannot run.
  # An unrunnable gate that prints nothing is indistinguishable from a gate
  # that passed — the exact shape this script now exists to prevent.
  if [ -z "${PYBIN:-}" ]; then
    echo "⚠ dependency drift NOT CHECKED: \$PYTEST is not the '<python> -m pytest' form,"
    echo "  so there is no interpreter to query. A stale dependency set would be invisible."
  elif [ ! -f "$REQ_FILE" ]; then
    echo "⚠ dependency drift NOT CHECKED: $REQ_FILE not found from $REPO_ROOT."
  fi
fi

# ── docker image age ─────────────────────────────────────────────────────────
# The drift gate above proves the image SATISFIES requirements.txt; it cannot
# prove the image matches what CI tests with. requirements.txt pins ranges
# (fastapi>=…,<1.0), CI resolves them fresh on every run, and the local image
# keeps whatever it resolved the day it was built — so an old image can be red
# where CI is green, or green where CI is red, with no file having changed.
# Report-only: say how old the image is and what it carries, loudly when it
# predates the last requirements/Dockerfile change on this branch.
# IMAGE_AGE_CHECK=0 opts out.
IMAGE=""
if [[ "${PYBIN:-}" == *"docker run"* ]]; then
  IMAGE="${PYBIN##* }"
fi
IMAGE_ID=""
if [ "${IMAGE_AGE_CHECK:-1}" = "1" ] && [ -n "$IMAGE" ]; then
  IMAGE_ID="$(docker image inspect -f '{{.Id}}' "$IMAGE" 2>/dev/null || true)"
  created="$(docker image inspect -f '{{.Created}}' "$IMAGE" 2>/dev/null || true)"
  if [ -n "$created" ]; then
    img_ts="$(date -d "$created" +%s 2>/dev/null || echo 0)"
    age_d=$(( ( $(date +%s) - img_ts ) / 86400 ))
    vers="$(docker run --rm --entrypoint python "$IMAGE" -c \
      "import importlib.metadata as m; print(' '.join(f'{p} {m.version(p)}' for p in ('fastapi','pydantic','sqlalchemy','freezegun')))" 2>/dev/null || true)"
    echo "image $IMAGE built $(date -d "$created" '+%Y-%m-%d %H:%M') (${age_d}d old): ${vers:-versions unreadable}"
    req_ts="$(git -C "$REPO_ROOT" log -1 --format=%ct -- gdx_dispatch/requirements.txt gdx_dispatch/docker/Dockerfile 2>/dev/null || true)"
    if [ -n "$req_ts" ] && [ "$img_ts" -lt "$req_ts" ]; then
      echo "⚠ image is OLDER than the last requirements.txt/Dockerfile change on this branch"
      echo "  ($(date -d "@$req_ts" '+%Y-%m-%d %H:%M')). Failures may be the image, not your code. Rebuild:"
      echo "    docker compose -f gdx_dispatch/docker/docker-compose.yml build app"
    elif [ "$age_d" -ge "${IMAGE_MAX_AGE_DAYS:-7}" ]; then
      echo "⚠ image is ${age_d} days old; CI resolves requirements.txt's ranges fresh on every"
      echo "  run, so this image may carry older library versions than CI tests with."
    fi
  fi
fi

# addopts comes from pytest.ini (marker filter + -q + -p no:schemathesis_xdist).
# --ignore is REQUIRED on top of it: e2e/test_schemathesis.py performs a
# network call at import time, and marker filtering happens after import.
# FORKED=1 re-enables per-test subprocess isolation. Default matches ci.yml
# (unforked since 2026-08-04, #20 re-test) — keep the two in lockstep.
#
# `-ra` makes every shard NAME what it did not pass. Without it the matrix says
# "102 skipped" and names not one of them, so a Postgres arm that never ran
# looks exactly like a green one — the gap CLAUDE.md calls invisible. Measured
# 2026-09-27 on test_pg_fixture_smoke.py + test_gdx_ai_readonly_role.py:
# 9 skipped / 0 reason lines before, 9 skipped / 7 reason lines summing to 9
# after, same runtime.
#
# `-ra` and NOT `-rs`. pytest's `-r` is a STORE option whose default is "fE"
# (_pytest.terminal._REPORTCHARS_DEFAULT) and getreportopt() iterates only the
# chars you pass, so a bare `-rs` REPLACES that default and deletes every
# `FAILED <test>` and `ERROR <file>.py` short-summary line. The collection-error
# report at the bottom of this script greps for exactly `^ERROR [^ ]+\.py`, so
# `-rs` would silently switch off the #679 guard. Verified 2026-09-27 against a
# planted unimportable module: `-rs` printed a bare "1 error in 0.34s" and no
# COLLECTION ERRORS block at all.
#
# `a` is getreportopt()'s own alias for "sxXEf" — skipped, xfailed, XPASSed,
# errored, failed — so it is a one-char superset of `-rs`/`-rfEs`, and the only
# form that surfaces an XPASS. That last part earns its keep on the NON-strict
# xfails: test_schema_fixture_drift.py:97 is `strict=False` and its reason says
# to flip strict=True once SS-4d lands, so it starting to pass is exactly the
# signal we want and is otherwise silent. (test_settings_row.py:179 is
# `strict=True`, where an XPASS already fails the shard on its own — `X` is what
# covers the ones that do not.) test_run_tests_split_report_opts.py pins the
# behaviour rather than the letters, so a better flag stays allowed.
COMMON_OPTS=(--ignore=gdx_dispatch/tests/e2e --tb=short -ra)
if [ "${FORKED:-0}" = "1" ]; then
  COMMON_OPTS+=(--forked)
fi

# --scans: one pytest process over the derived files, no shards. Everything
# above still applied — the gitdir mount (without it every tracked-set guard
# raises TrackedFilesUnavailable, which reads like a stale baseline), tmpfs,
# the dependency gate and the image-age report. The matrix lock is NOT taken:
# it rations N parallel shards, and one ~25 s process waiting ~7 min behind
# another agent's matrix would defeat the point of a pre-check.
if [ "$MODE" = "scans" ]; then
  echo "baseline scans (${#SCAN_FILES[@]} files):"
  printf '    %s\n' "${SCAN_FILES[@]}"
  set +e
  $PYTEST "${COMMON_OPTS[@]}" "${SCAN_FILES[@]}" "$@"
  rc=$?
  # 5 = nothing collected, which here means the list or a -k filter is wrong,
  # not that there was nothing to check — and not a stale baseline.
  if [ "$rc" -eq 5 ]; then
    echo
    echo "FAIL — no baseline scan test was collected (pytest exit 5). Check the -k/args you passed"
    echo "  and $0 --scans --list. This is not a stale baseline: do not re-freeze for it."
    exit 1
  elif [ "$rc" -ne 0 ]; then
    echo
    echo "FAIL — baseline scans red (pytest exit $rc). A stale baseline is re-frozen with"
    echo "  $0 --refreeze-baselines"
    echo "which refuses anything that GREW. Then re-run $0 --scans."
    exit 1
  fi
  echo
  echo "PASS — baseline scans green."
  exit 0
fi

# ── host-wide matrix lock ────────────────────────────────────────────────
# Two matrices at once are slower than two in a row: on 2026-09-24 four
# Paperclip agents ran this concurrently — 28 shards on 20 cores, load 28,
# 1.8 GB free. One matrix at a time on this host. A waiter says so and names the holder, so a
# polled log explains the silence. MATRIX_LOCK=0 bypasses; MATRIX_LOCK_FILE
# relocates. The lock lives on fd 9 and drops when this script exits.
LOCK_FILE="${MATRIX_LOCK_FILE:-/tmp/gdx_matrix.lock}"
if [ "${MATRIX_LOCK:-1}" = "1" ]; then
  if command -v flock >/dev/null 2>&1; then
    exec 9>>"$LOCK_FILE"
    if ! flock -n 9; then
      echo "⏳ matrix lock $LOCK_FILE is held ($(tail -1 "$LOCK_FILE" 2>/dev/null || echo 'holder unknown')) — waiting for it (MATRIX_LOCK=0 to bypass)"
      wait_start=$(date +%s)
      flock 9
      echo "🔓 matrix lock acquired after $(( $(date +%s) - wait_start ))s"
    fi
    printf 'pid %s since %s in %s\n' "$$" "$(date -Is)" "$REPO_ROOT" > "$LOCK_FILE"
  else
    echo "⚠ flock not found — matrix lock NOT taken; two matrices may overlap"
  fi
fi

# Clear the last run's shard logs: an N=4 run after an N=7 one would otherwise
# leave groups 5-7 behind, and the error scans and the vs-main report below read
# group_*.log, so stale reds would be reported as this run's.
rm -f "$LOG_DIR"/group_*.log

pids=()
for g in $(seq 1 "$N"); do
  $PYTEST "${COMMON_OPTS[@]}" --splits "$N" --group "$g" "$@" \
      > "$LOG_DIR/group_${g}.log" 2>&1 &
  pids+=("$!")
done

fail=0
i=0
for pid in "${pids[@]}"; do
  i=$((i + 1))
  set +e
  wait "$pid"
  rc=$?
  set -e
  # pytest exit 5 = "no tests collected" — expected when N > test count.
  if [ "$rc" -ne 0 ] && [ "$rc" -ne 5 ]; then
    fail=1
    echo "✗ group $i failed (exit $rc) — see $LOG_DIR/group_${i}.log"
  fi
done

echo
echo "=== per-shard summary ==="
for g in $(seq 1 "$N"); do
  printf "group %s: %s\n" "$g" "$(tail -1 "$LOG_DIR/group_${g}.log")"
done

# Collection errors are the loudest-consequence, quietest-looking failure here
# (#679): the file never ran at all, and the tail line says "1 error" next to a
# wall of passes. Name the files instead of leaving them in the scroll. This is
# a REPORT, not the gate — pytest exits 2 on a collection error, so `fail` is
# already set above; surfacing it separately means it cannot be skimmed past.
# Anchor on pytest's "short test summary info" line (`ERROR <path>`), NOT on
# the banner `____ ERROR collecting <path> ____` — the banner is padded with
# underscores, so `^ERROR collecting` matches nothing. Verified 2026-09-12 by
# planting a module that fails to import and reading the log.
#
# `|| true` is load-bearing: `set -e` is in force here (re-enabled inside the
# wait loop above), and grep exits 1 when it finds nothing — which is the
# HAPPY path. Without it a clean run dies silently right before "PASS".
collect_errors="$(grep -hE "^ERROR [^ ]+\.py" "$LOG_DIR"/group_*.log 2>/dev/null | sort -u || true)"
missing_mods="$(grep -hoE "ModuleNotFoundError: No module named '[^']+'" "$LOG_DIR"/group_*.log 2>/dev/null | sort -u || true)"
if [ -n "$collect_errors" ]; then
  echo
  echo "✗ COLLECTION ERRORS — these files did NOT run:"
  echo "$collect_errors" | sed 's/^/    /'
  if [ -n "$missing_mods" ]; then
    echo
    echo "  Missing imports:"
    echo "$missing_mods" | sed 's/^/    /'
  fi
  echo "  A collection error removes the whole file from the run. If this is an"
  echo "  ImportError for a package in requirements.txt, rebuild the image."
  fail=1
fi

# A caller's own report flag lands AFTER COMMON_OPTS on the shard command and
# pytest keeps the last one, so `-rs`, `-vrs`, `-r=s` or `--report-chars=s`
# passed to this script silently undo the `-ra` and drop pytest's "fE" — the
# shard then says "3 failed" and names none of them, and the collection-error
# report above finds nothing to grep. Rather than parse every spelling of the
# flag, check the output: a shard whose tail reports failures or errors must
# carry at least one FAILED/ERROR summary line. Anchored on the summary shape
# (`FAILED <file>::`, `ERROR <file>.py`), because a failing test's captured log
# prints `ERROR    logger:...` and its stdout can print anything. Report-only —
# such a shard exited non-zero, so `fail` is already set.
for g in $(seq 1 "$N"); do
  log="$LOG_DIR/group_${g}.log"
  if tail -1 "$log" | grep -qE "[0-9]+ (failed|errors?)\b" \
     && ! grep -qE "^(FAILED [^ ]+::|ERROR [^ ]+\.py)" "$log"; then
    echo
    echo "✗ group $g reports failures but names none — a -r/--report-chars flag (or --no-summary)"
    echo "  passed to this script replaced the runner's -ra and dropped 'f'/'E'. Re-run without it."
  fi
done

# ── failures vs main ────────────────────────────────────────────────────────
# Sort every named failure into NEW (yours) and ALREADY FAILING ON MAIN, from a
# baseline the host records once per origin/main commit (matrix_vs_main.py has
# the why). Report-only: it never changes this script's exit code — a red that
# is already red on main is still red, it is just not this branch's to chase.
# MATRIX_VS_MAIN=0 opts out.
if [ "$fail" -ne 0 ] && [ "${MATRIX_VS_MAIN:-1}" = "1" ]; then
  python3 -I "$REPO_ROOT/gdx_dispatch/tools/matrix_vs_main.py" compare \
    --logs "$LOG_DIR" --repo "$REPO_ROOT" --image "$IMAGE_ID" || true
fi

if [ "$fail" -ne 0 ]; then
  echo
  echo "FAIL — at least one shard reported errors. Logs in $LOG_DIR/"
  exit 1
fi
echo
echo "PASS — all $N shards green. Logs in $LOG_DIR/"
