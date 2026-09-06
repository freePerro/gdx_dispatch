# Design-doc archive

**Status: CURRENT** — the rule for this directory, written 2026-09-06 when the finished records moved here.

Every file here is a record of the **past**: a plan that shipped, an audit that
closed, or a description of something that was never built. Its status line
(line 3) says which — `MERGED #N`, `RELEASED vX.Y.Z`, `HISTORICAL`, `BUILT`.
The rule from `CLAUDE.md` § *The written record* applies unchanged:

- **Do not act on one of these.** Its "what already exists" table and its
  build steps describe the repo on the day it was written. Re-verify against
  code, a PR number or a release tag before doing anything it suggests.
- **Do not delete one.** Source files and immutable migrations cite these by
  filename; the reasoning and rejected alternatives they hold are the part the
  code cannot recover. The link scanner resolves citations by basename, so a
  citation written as `docs/design/<name>.md` still finds the file here.
- **Only the status line changes** — when a later PR ships or retires
  something a record still calls open.

Plans still in flight (`PLAN`, `PARTIALLY BUILT`, `NOT SCHEDULED`, debt
registers) stay one level up in `docs/design/`. When one finishes, its
last PR moves it here and stamps the status line in the same commit.
