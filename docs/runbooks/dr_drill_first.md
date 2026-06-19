# Runbook: First DR Drill (SS-34)

> **Operational status:** REQUIRED pre-cutover gate for SS-30.
> This is the first end-to-end disaster-recovery drill. Green on this
> runbook is a hard dependency of the SS-30 cutover.

## RTO / RPO targets

| Metric | Target | Placeholder until measured |
|---|---|---|
| **RTO** (recovery time objective) | < 4 hours from "start" to "user can log in" | PLACEHOLDER — first drill will establish the baseline |
| **RPO** (recovery point objective) | <= 15 minutes of data loss acceptable | PLACEHOLDER — depends on backup cadence; see SS-1 |
| **Cadence** | Quarterly | Next drill: 90 days after the first green |

## Escalation contacts (placeholders)

| Role | Name | Channel |
|---|---|---|
| Incident commander | PLACEHOLDER | PLACEHOLDER |
| DB owner | PLACEHOLDER | PLACEHOLDER |
| Auth/Authentik owner | PLACEHOLDER | PLACEHOLDER |
| Secondary on-call | PLACEHOLDER | PLACEHOLDER |

Fill in before first real drill. Keep this section current; stale
contact info is worse than none during an incident.

## Pre-flight

Perform EVERY check. If any item fails, abort — do not proceed.

1. **Staging isolated from production.** `staging_db_url` MUST NOT
   equal the production `DATABASE_URL`. The orchestrator refuses if
   they match, but verify manually too.
2. **Staging has adequate disk.** Need `1.5 x size_bytes` free for
   the dump + the restored DB (compressed dump expands ~3x).
3. **`pg_dump` / `pg_restore` versions match the server major
   version.** Version skew between client and server is a common
   drill failure that wastes the RTO budget.
4. **Source DB is reachable from the drill host.** Run a quick
   `psql -c 'select 1'` against `--source-db` before kicking off.
5. **Backup artifact target path exists and is writable.**
6. **Audit hash-chain is intact on the source.** Run a pre-drill
   `verify_chain` sample on one tenant to confirm the baseline.
7. **No production cutover in progress.** The drill locks a CPU for
   `pg_dump`; overlapping with a real cutover is a bad day.

## Kick off

### Via CLI (systemd timer / manual)

```bash
python -m gdx_dispatch.tools.dr_drill_cron \
  --scope=full \
  --source-db=postgresql://USER:PASS@db.prod-replica.internal/gdx \
  --staging-db=postgresql://USER:PASS@db.staging.internal/gdx_drill \
  --snapshot-target=/var/backups/dr/drill-$(date -u +%Y%m%dT%H%M%SZ).pgc \
  --json
```

Exit code 0 = drill passed. See `gdx_dispatch/tools/dr_drill_cron.py` for the
full exit-code table.

### Via admin UI

Navigate to `/admin/dr-drills`. Click **Schedule Drill** and fill in
scope + staging URL + source URL + snapshot target. The UI surfaces
live pass/fail; full JSON report is available on the detail view.

### Via API (for automation)

```bash
curl -X POST https://admin.tgdplatform.com/api/admin/dr/drills \
  -H 'Content-Type: application/json' \
  -d '{
    "scope": "full",
    "source_db_url": "…",
    "staging_db_url": "…",
    "snapshot_target": "/var/backups/dr/drill.pgc"
  }'
```

Response 200 = passed. Response 500 with `detail.message =
"drill verification failed"` = verification-stage failure — restore
itself succeeded but the staging DB failed a sanity check.

## Expected timings

First-drill budget (adjust after baseline is captured):

| Stage | Budget | Typical |
|---|---|---|
| Pre-flight checks | 5 min | manual |
| `pg_dump` (full) | < 45 min | compressed custom format |
| SHA-256 verify | < 1 min | streaming hash |
| `pg_restore` | < 60 min | `--clean --if-exists --exit-on-error` |
| Verification harness | < 2 min | read-only SELECTs only |
| Human sanity check | 10 min | log in to staging auth URL |
| Report write-up | 15 min | commit `ai-queue/rd/operations/dr_drill_<date>.md` |
| **Total** | **< 4h** | **RTO budget per D-55** |

## Interpret the report

Every drill produces a JSON `DrillReport`. Fields to read carefully:

- `passed` — overall green/red.
- `failure_reason` — populated if `passed=false`. Prefix tells you
  the stage: `snapshot:` / `restore:` / `verification:`.
- `snapshot.sha256` — integrity hash. Record this; if the artifact
  ever needs to be re-restored, every future restore MUST match
  this hash via `hmac.compare_digest`.
- `restore.integrity_verified` — `true` means the on-disk sha256
  matched the manifest before `pg_restore` ran. `false` here means
  somebody skipped the guard — investigate.
- `restore.duration_s` — contributes to RTO measurement.
- `restore.rows_by_table` — post-restore row counts, useful for a
  quick "does this look like prod?" sanity check.
- `verification.checks[]` — every check with name/passed/detail.
  Focus first on `hashchain:*` failures (integrity problem) and
  `critical:*` failures (wrong DB restored?). `rowcount:*` failures
  usually mean thresholds need tuning, not a real problem.

## Escalation on failure

| What failed | First action |
|---|---|
| `snapshot:` stage | Check source DB reachability; check `pg_dump` version; check disk space. Page DB owner. |
| `restore:` integrity mismatch | DO NOT retry with the same artifact. Re-dump. Page security lead — tampered artifact is a Sev-1. |
| `restore:` pg_restore error | Inspect stderr in report. Version skew is the most common cause. Page DB owner. |
| `verification:` hashchain | Page auth/audit owner — audit chain break is Sev-1 regardless of source. |
| `verification:` critical:system_tenant | Wrong DB restored? Double-check `--source-db`. |
| `verification:` rowcount OUT OF RANGE | Likely a threshold tune. Not necessarily Sev-1; file a TD. |

## Post-drill cleanup

1. **Tear down staging DB.** A drill-restored DB is NOT for keeping.
   Drop the database: `DROP DATABASE gdx_drill;` — or, preferably,
   schedule the whole VM for destruction so any cached connection
   state is gone.
2. **Remove the snapshot artifact.** Unless you're retaining it for
   evidence per SS-34 deliverables, delete from `--snapshot-target`.
   If retaining, move to write-once storage with a documented
   retention (90 days default).
3. **Commit the drill report.** `ai-queue/rd/operations/dr_drill_<YYYY-MM-DD>.md`
   with the JSON report inline + any gaps found.
4. **File gaps as TDs.** Anything discovered during the drill that
   isn't already tracked — file via `/tech-debt`.
5. **Update the RTO baseline.** If this drill's `restore.duration_s
   + verification duration` is meaningfully different from the
   published RTO, update the published number (or optimize).
6. **Schedule the next drill.** Quarterly. Create the calendar
   invite NOW — a drill not on the calendar is a drill that won't
   happen.

## What this runbook does NOT cover

- **Key-loss recovery.** SS-34 acceptance criteria #3 requires
  exercising `GDX_LOG_REDACT_SALT` / `AUTHENTIK_SECRET_KEY` loss —
  that sub-drill has its own runbook. INTEGRATION_TODO: link here
  once written.
- **Multi-region failover.** Deferred until customer #50+ per SS-34
  scope.
- **Automatic failover.** Manual restore is acceptable at this stage.
- **Production restore.** SS-34 is staging-only. A production
  restore runbook is a separate, much more cautious document.
