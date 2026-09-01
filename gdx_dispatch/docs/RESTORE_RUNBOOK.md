# GDX Restore Runbook

**Status: UNRELIABLE — do not follow the S3 steps without checking the VPS
first.** Corrected 2026-09-01. The procedure below restores from a location
**nothing in this repository writes to on a schedule**, and it restores a
per-tenant/control-plane database layout this deployment does not have. Read
the box below before you need it, not during an outage.

> **What this repo can show (checked 2026-09-01):**
> * The steps below fetch `s3://gdx-backups/tenants/<slug>/…` and
>   `s3://gdx-control-backups/…`. Those paths are written by
>   `gdx_dispatch/scripts/backup.sh`, which enumerates
>   `SELECT slug FROM tenants … db_provisioned = true` — the multi-tenant model
>   this project does not run — and **no scheduler, compose file, workflow or
>   cron in this repository invokes it.**
> * The only backup script carrying a schedule is **`scripts/backup-db.sh`**:
>   `0 2 * * *`, `docker exec gdx-postgres-dev pg_dump`, gzipped to local
>   **`/var/backups/gdx`**, pruned at 30 days. Different location, different
>   shape, and *not* what the steps below restore.
> * `scripts/restore_all.sh`, referenced under "Full Disaster Recovery", is
>   marked "(todo: write in Sprint 2)" and was never written.
>
> **What this repo CANNOT show, and someone must check on the box:** whether a
> working backup runs on the production VPS at all, and where it writes. A
> perfectly good cron may exist there. That is exactly the danger — this
> document asserts one, and the repository cannot corroborate it.
>
> `docs/design/soc2-readiness-gap-analysis.md` lists **"no restore has ever
> been tested"** as an open finding. Until the two bullets above are
> reconciled against the VPS, treat that finding as unresolved and this
> runbook as unproven.

**Last verified:** 2026-04-15 by automated drill (see Banking Readiness item
11) — **4½ months before this correction**, against the architecture described
above.

**RTO:** 4 hours  **RPO:** 24 hours _(targets, not measurements)_

This says to run monthly. It has not been.

---

## Restore a Single Tenant DB

```bash
# 1. Download the backup
aws s3 cp s3://gdx-backups/tenants/${TENANT_SLUG}/${DATE}.dump /tmp/restore.dump

# 2. Create a restored DB (do NOT overwrite the live DB)
createdb gdx_${TENANT_SLUG}_restored

# 3. Restore
pg_restore -d gdx_${TENANT_SLUG}_restored /tmp/restore.dump

# 4. Run smoke tests against the restored DB
CONTROL_DATABASE_URL="postgresql://localhost/gdx_${TENANT_SLUG}_restored" \
  pytest gdx_dispatch/tests/test_01_gdx_scaffold.py -v

# 5. Time the full operation — must complete under 4 hours (RTO target)

# 6. Clean up
dropdb gdx_${TENANT_SLUG}_restored
rm /tmp/restore.dump
```

## Restore the Control Plane

```bash
aws s3 cp s3://gdx-control-backups/${HOUR}.dump /tmp/control_restore.dump
createdb gdx_control_restored
pg_restore -d gdx_control_restored /tmp/control_restore.dump
# Verify: check tenant count matches production
psql gdx_control_restored -c "SELECT COUNT(*) FROM tenants WHERE deleted_at IS NULL;"
```

## Full Disaster Recovery (all tenants)

1. Provision a new PostgreSQL instance
2. Restore control plane DB first (determines tenant list)
3. Run `scripts/restore_all.sh` (todo: write in Sprint 2)
4. Update DNS to point to new instance
5. Verify health checks on all tenants

---

## Monthly Drill Checklist

- [ ] Pick one tenant at random
- [ ] Download their most recent backup
- [ ] Restore to staging DB
- [ ] Run smoke test suite
- [ ] Record total time
- [ ] Verify time is under 4 hours (RTO)
- [ ] Document result in this file

| Date | Tenant | Duration | Result |
|------|--------|----------|--------|
| (fill in after drill) | | | |
