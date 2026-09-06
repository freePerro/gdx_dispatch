# GDX Restore Runbook

**Status: CURRENT — rewritten 2026-09-06 for the one database this install has.
The commands below were drilled the same day against a throwaway Postgres
named like production (table in the last section); they have not yet been
run on the production host.** Until 2026-09-06 this
runbook restored `s3://gdx-backups/tenants/<slug>/…` and a separate "control
plane" dump — a per-tenant layout this deployment never ran, written by a
script nothing scheduled (removed the same day). What follows is built only
from mechanisms that exist in this repository, named by file.

**RTO:** 4 hours  **RPO:** 24 hours _(targets, not measurements)_

## Where a backup can come from

| Source | What it writes | Who runs it |
|---|---|---|
| `gdx_dispatch/docker/update.sh` | `backups/gdx-pre-update-<ts>.sql.gz` at the checkout root (update.sh cds there) — a plain `pg_dump -U gdx gdx \| gzip` taken **before** every image update | every production update |
| In-app **Admin → Database** (`/admin/database`; `routers/admin_db.py`, `POST /api/admin/db/backup`) | a `pg_dump -Fc` custom-format file under `/app/uploads/_db_backups/` inside the app container (the `gdx_uploads` volume) | an owner, on demand |
| `scripts/backup-db.sh` | `/var/backups/gdx/dispatch_dev_<ts>.sql.gz`, pruned at 30 days | a cron line the script's header suggests (`0 2 * * *`). ⚠ It dumps from a container named `gdx-postgres-dev`; the production database container is `gdx-db-1`. Fix the name before relying on it |

**What this repository cannot show:** whether any of these actually runs on
a schedule on the production host. A cron may exist there; check the box
(`crontab -l`, `/var/backups/gdx`, `ls backups/` at the checkout root) and
record the answer in the drill table at the bottom of this page.

## Restore into a throwaway database first — never over the live one

```bash
# 0. Identify the dump. Plain-SQL gzip (update.sh / backup-db.sh) or
#    custom format (in-app backup) — the restore command differs.
DUMP=backups/gdx-pre-update-20260905-051116.sql.gz

# 1. Create a scratch database next to the live one (same container, new name)
docker exec gdx-db-1 createdb -U gdx gdx_restored

# 2a. Plain-SQL gzip
gunzip -c "$DUMP" | docker exec -i gdx-db-1 psql -U gdx -d gdx_restored -q

# 2b. Custom format (pg_dump -Fc) — the in-app backup lives in the app
#     container, so copy it out and into the db container
docker cp "gdx-app-1:/app/uploads/_db_backups/$DUMP" /tmp/restore.dump
docker cp /tmp/restore.dump gdx-db-1:/tmp/restore.dump
docker exec gdx-db-1 pg_restore -U gdx -d gdx_restored --no-owner /tmp/restore.dump

# 3. Prove it is the data you think it is — compare against live
for t in customers jobs invoices estimates audit_logs; do
  printf '%-12s live=%s restored=%s\n' "$t" \
    "$(docker exec gdx-db-1 psql -U gdx -d gdx -tAc "select count(*) from $t")" \
    "$(docker exec gdx-db-1 psql -U gdx -d gdx_restored -tAc "select count(*) from $t")"
done
docker exec gdx-db-1 psql -U gdx -d gdx_restored -tAc \
  "select count(*) filter (where row_hash is null or prev_hash is null) from audit_logs"   # must be 0

# 4. Boot the app against it (read-only smoke) — a throwaway container on
#    another port, pointed at gdx_restored, then GET /health and log in.
#    DATABASE_URL=postgresql://gdx:<pw>@db:5432/gdx_restored

# 5. Time steps 1–4. Under 4 hours is the RTO target.

# 6. Clean up
docker exec gdx-db-1 dropdb -U gdx gdx_restored
docker exec gdx-db-1 rm -f /tmp/restore.dump
rm -f /tmp/restore.dump
```

## Restoring over production (real outage only)

1. Stop the writers first — with the project name, or nothing stops:

   ```bash
   # The production stack is compose project `gdx`, built from THREE files by
   # gdx_dispatch/docker/update.sh. Plain `docker compose -f …docker-compose.yml`
   # addresses a project that does not exist: `stop` stops nothing and `up`
   # creates a SECOND stack on the same volumes. Always use the same array
   # update.sh uses (run from the checkout root, where .env lives):
   COMPOSE=(docker compose -p gdx --env-file ./.env
     -f gdx_dispatch/docker/docker-compose.yml
     -f gdx_dispatch/docker/docker-compose.selfhost.yml)
   "${COMPOSE[@]}" stop app celery-high celery-low celery-beat
   ```

2. Rename, don't drop: `ALTER DATABASE gdx RENAME TO gdx_broken_<ts>` — keep the broken copy until the restored one has served a full day.
3. `createdb -U gdx gdx`, then step 2a/2b above into `gdx`.
4. Start the stack with `update.sh`'s normal command (the entrypoint runs
   `alembic upgrade head` on boot; a dump taken at an older migration head is
   migrated forward automatically — a dump taken at a *newer* head cannot be
   run under an older image).
5. Walk the app: login, dashboard, one customer, one invoice, Settings →
   Database Admin shows "at head".

## Full disaster recovery (new host)

1. Provision the VPS and check out this repository at the release tag that
   was running (`APP_VERSION` in the compose project's `.env`).
2. Restore `.env` from its own backup — it carries the encryption keys
   (`MASTER_ENCRYPTION_KEY`, `GDX_FERNET_KEY`); without them the restored
   PII columns are ciphertext. See `SECRETS_ROTATION_RUNBOOK.md`.
3. `"${COMPOSE[@]}" up -d db` (the array from the section above), then restore as above into `gdx`.
4. `update.sh` to pull the pinned image and start everything.
5. Point DNS at the new host. If the public origin (`GDX_PUBLIC_BASE_URL`)
   is unchanged, nothing else needs re-registering. If it changed: Outlook
   Graph subscriptions carry the notification URL they were *created* with
   (renewal only extends the expiry — `modules/outlook/subscriptions.py`),
   so delete and recreate them; the Phone.com callback is re-pointed by the
   weekly rotation task or by hand from Settings → Integrations → Phone.com.

## Drill checklist (monthly — record the run here)

- [ ] Pick the newest `gdx-pre-update-*.sql.gz`
- [ ] Restore into `gdx_restored` (steps 1–3)
- [ ] Row counts match live; audit chain columns intact
- [ ] Throwaway app boots and serves login
- [ ] Total time under 4 hours

| Date | Dump | Duration | Result |
|------|------|----------|--------|
| 2026-09-06 | throwaway: schema built by the container entrypoint (alembic head 086), dumped both ways | ~1 min | pass — steps 1, 2a, 2b, 3, 6 verbatim; 270 tables and every row count matched; not production data |
