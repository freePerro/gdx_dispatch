# GDX Restore Runbook

**Last verified:** 2026-04-15 by automated drill (see Banking Readiness item 11)

**RTO:** 4 hours  **RPO:** 24 hours

Run this runbook monthly to verify it works. Record the time taken.

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
