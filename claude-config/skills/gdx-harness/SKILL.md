---
name: gdx-harness
description: How tests, the matrix, lint, CI and PR merging actually behave in gdx_dispatch. Load before running any test or the matrix, reading a CI result, arming a monitor or long-running command, regenerating a lockfile, re-stacking or merging a PR, using ast-grep, or running Docker Compose or reaching production over SSH.
---

# Commands and harness facts

Moved here from `CLAUDE.md` on 2026-10-11 so it loads when a session needs it, not in every session's preamble.

- ~15 known cross-file test flakes exist on main: they pass in isolation.
  Re-run in isolation before blaming your branch.
- Lint is a **ruff ratchet against a baseline**, not plain pass/fail — a
  branch can be "clean" and still over the ratchet.
- **CI (`ci.yml`) triggers on `pull_request` with base `main`, and on `push`
  to `main`.** It does *not* run on a PR whose base is another feature branch —
  which is why **mid-stack PRs still need the local matrix run and its results
  posted before merge**. Note also the `push` trigger carries a paths filter
  (`gdx_dispatch/**`, `gdx_dispatch/docker/**` — note the prefix: a *root*
  `docker/` change matches nothing — `.github/workflows/**`, excluding
  `**/*.md`)
  while the `pull_request` trigger has none, so a docs-only PR runs the full
  suite and a docs-only push to main does not. Verified against
  `.github/workflows/ci.yml` 2026-09-01.
- **The merge path is the ruleset plus auto-merge, not a hand merge.** Ruleset
  18005412 ("Branch Safety", active) requires 12 status checks on `main`, and
  `.github/workflows/auto-merge.yml` turns on squash auto-merge for every
  same-repo PR the moment it is opened or marked ready for review, so GitHub
  merges it once those checks are green. Marking a PR ready is the approval
  step. The ruleset does not require the branch to be up to date. `--admin` skips
  that gate (repository-role bypass is `always`), so it is kept only for when
  auto-merge cannot act: a fork PR, or a required check that is broken
  independently of the PR. Even then, use it only after enumerating every
  check's result by name. Ruleset and workflow read 2026-10-14.
- **Run the matrix with `gdx_dispatch/tools/run_tests_split.sh` (N=7), and
  never with `--network host`** — the host network breaks ~15 tests. The cost
  of leaving it off is that the Postgres arm goes **silently SKIPped** (102
  tests, measured 2026-09-14): a green local run has not exercised PG.
  The runner and `ci.yml` both pass `-ra` (GDXA-128), so every shard names
  its skips — read the categories, or the gap is invisible. On a bare `pytest`
  use `-ra` too and never plain `-rs`: `-r` REPLACES pytest's `fE` default, so
  `-rs` deletes the `FAILED`/`ERROR` summary lines.
  CI runs the `DATABASE_URL` and `GDX_TEST_PG_*` Postgres tests, and under `CI`
  an unreachable Postgres fails them rather than skipping (#440). The
  `TEST_DATABASE_URL` AI role tests skip in the shards and run in CI's own
  `ai-roles` job against `gdx_dispatch/tools/gdx_ai_roles.sql`, which fails on
  a skip (GDXA-1564; not a required check as of 2026-10-18); the local runner does not provision them, so they skip
  there. The `GDX_TEST_CONTROL_DB_URL` integration tests still skip green in CI.
- **After `git merge origin/main` and before the matrix, run
  `gdx_dispatch/tools/run_tests_split.sh --scans`** (~30 s, one docker
  process, gitdir mounted, `-ra`, non-zero on red, no wait for the matrix
  lock). It runs the tests that pin `.tenant_plane_redundant_filter_baseline`
  and `.duplicate_block_baseline` against the tree; those two line-keyed
  baselines were the matrix red on at least eight agent issues (GDXA-408).
  It does **not** cover the doc-link, PII, authz, route-shadow or OpenAPI
  baselines: green `--scans` is not "baselines clear", and the matrix still
  runs. The file list is derived by naming convention
  (`gdx_dispatch/tools/baseline_scan_tests.py`; `--scans --list` prints it):
  a test file named `*scan_refreeze*`, or one defining
  `test_the_committed_baseline_is_what_the_tree_scans`, joins without an edit
  here. On red,
  `run_tests_split.sh --refreeze-baselines` re-freezes both baselines: it
  admits shrinkage and line shifts and **refuses growth** (exit 2; the
  refusing scan's baseline is untouched, the other is still re-frozen; a
  scanner crash exits 1). It passes `--allow-new` only when you type it; do that only for a
  clone or filter you have read and mean to keep. A hand-rolled
  `docker run -v $PWD:/app` in a linked worktree cannot read the git index and
  fails these tests with `TrackedFilesUnavailable`, which looks like a stale
  baseline. Use the runner.
- **`run_tests_split.sh` takes a host-wide lock** (`/tmp/gdx_matrix.lock`): a
  second matrix waits and says so, because two at once are slower than two in
  a row (2026-09-24: four at once put the 20-core box at load 28 with 1.8 GB
  free). `MATRIX_LOCK=0` bypasses it. A docker `PYTEST` gets
  `--tmpfs /tmp` injected (`TMPFS_TMP=0` opts out): the tests' file-backed
  SQLite fsyncs on every commit, and with `/tmp` on the host disk shard 4 took
  ~14 min against ~1 min on tmpfs (2026-10-05); the full matrix is ~6 min.
  In a linked worktree the script also mounts the gitdir into a docker
  `PYTEST`, so the tracked-set guards pass there instead of failing 14 tests
  for want of the index.
- **A failed matrix sorts its reds against main** (`tools/matrix_vs_main.py`):
  NEW (probably yours), FLAKY ON MAIN, or ALREADY FAILING ON MAIN, from a baseline the
  maintainer's host records once per `origin/main` commit (a user timer outside
  this repo, ~/.cache/gdx_matrix_baseline). <!-- host paths, outside this repo; link-ok -->
  It is a report: the exit code is unchanged. With no baseline on the host, the
  reds say "unclassified", never "not yours"; against a baseline older than the
  merge-base they say "WAS RED ON MAIN … NOT proven", since main may have fixed
  the test since. A shard that dies without naming a test is never recorded as
  green. NEW is not proof: the baseline is a single run, so a flaky test can
  pass there and fail here. The script also prints the docker
  image's age and key library versions, and warns when the image predates the
  last `requirements.txt`/`requirements.lock`/Dockerfile change. CI and the
  images install `gdx_dispatch/requirements.lock` (GDXA-467), and the
  runner's dependency check fails an image that lacks any of its pins.
- `pytest.ini` already carries `-q`; adding another makes output useless. To
  read a CI failure use the jobs API, not `gh run view --log`:
  `gh api --allow-escape-sequences repos/freePerro/gdx_dispatch/actions/jobs/<jobId>/logs`.
  The flag is required: CI logs carry terminal escapes, and without it gh
  writes nothing to stdout (refusal on stderr, exit 1), so a piped `grep -c`
  prints 0 for a command that never ran (GDXA-1416, gh 2.102.0). A zero from
  grep counts only after `| wc -c` shows the log is non-empty.
  pytest's own stdout is not in that job log, but the Postgres service log
  (server version, test-issued SQL `ERROR:` lines) and the `--durations=15`
  block are, so together they prove a PG test executed. Counts and skips are
  not recoverable from it (GDXA-1423, run 36348298309, 2026-10-18). That
  service log also showed CI's `image: postgres:16` floats: it served 16.15,
  while prod was reported on 16.13; pinning is the maintainer's call.
- **A foreground `sleep` is blocked and a background Bash dies at 600s.** Long
  runs go through `nohup`, watched by ONE `Monitor` or a single until-loop —
  never a poll loop.
- **A monitor's poll command is proven once in the foreground before it is
  armed, and it never swallows its own failure** — emit the error as an event
  and exit, so silence can only mean "still waiting". On 2026-09-23 a
  `gh pr checks --json` loop written with `|| continue` sat silent for its
  whole 30 minutes: the distro's gh 2.46.0 had no `--json` on `pr checks`
  (added in gh 2.50.0). gh has come from GitHub's own apt repository
  (`/etc/apt/sources.list.d/github-cli.list`) since 2026-09-23; plain
  `gh pr checks` output carries the verdict in column 2 regardless.
- Compose `--env-file`: an empty `JWT_SECRET` crash-loops the container. The
  `verifyplaywright` path passes `--env-file` rather than cloning the
  environment, deliberately — cloning trips the credential guard.
- `ssh gdx-vps` reaches production over Tailscale. Real mobile verification
  runs on the local Pixel 8 AVD, where `10.0.2.2` is the host.
- **ast-grep is `~/.local/bin/ast-grep`** (pip `ast-grep-cli` 0.45.3, user <!-- user-site binary, outside this repo; link-ok -->
  site). Invoke it as `ast-grep`, never `sg`: `/usr/bin/sg` is the Linux
  group-switch command (a symlink to `newgrp`). The pip `sg` launcher was
  deleted on purpose and a pip upgrade recreates it — delete it again if it
  reappears; nothing checks for it. ast-grep has no `vue` language: `.vue`
  files are reached only through the root `sgconfig.yml` mapping them to
  `html`, after which `-l js` patterns match inside `<script>` blocks. Without
  that file every `.vue` file is skipped silently (`--inspect summary` should
  say `isProject=true`). The same file makes the repo an ast-grep project
  with no rule directories, so a bare `ast-grep scan` at the root exits 0
  having checked nothing — pass rules explicitly. A `severity: info` rule prints as `note[…]`, not
  `info[…]`, so grepping for `info[` finds nothing — count findings with
  `--json`.

## PR and merge sharp edges

- **A CONFLICTING PR runs no `pull_request` CI at all — silently.** GitHub
  cannot build the merge ref, so no run is created: not queued, not failed,
  nothing. A PR showing zero checks is DIRTY, not pending (cost a full CI
  cycle on #757/#758, 2026-09-20). Check `gh pr view N --json mergeable`
  before reading absent checks as anything.
- **With squash-merges, a stacked PR always goes CONFLICTING the moment its
  parent merges** if they touch the same files (the baselines guarantee they
  do). Not a merge mistake — the child still carries the parent's pre-squash
  commits. The fix is always the same two minutes: `git checkout -B <branch>
  origin/main && git cherry-pick <child's own commit>` and force-push; do
  NOT `git rebase origin/main`, which replays the whole pre-squash stack
  into conflicts (2026-09-20, three rounds of #753–#758).
