# GDX Restore Runbook

**Status: CURRENT — rewritten 2026-09-06 for the one database this install has,
and drilled the same day on the production host itself (table in the last
section): the newest pre-update snapshot restored into a scratch database in
27 s with every business-table count matching live.** Until 2026-09-06 this
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
| `scripts/backup-db.sh` | `/var/backups/gdx/dispatch_dev_<ts>.sql.gz`, pruned at 30 days | nothing — the production host's cron does NOT run this script (it names a `gdx-postgres-dev` container). Kept as a dev-box template |
| **The production host's own cron** (not in this repo; read 2026-09-06) | `0 2 * * *`: `pg_dump -Fc` of the live DB from `gdx-db-1`, GPG-encrypted to `/var/backups/gdx/gdx_live_<YYYYMMDD>.dump.gpg`, pruned at 30 days | every night. **The host holds only the public key** (root's keyring: 0 secret keys, checked 2026-09-06), so a nightly dump cannot be decrypted on the host: copy it to the machine that holds the private key, `gpg --decrypt` there, then bring the plain `-Fc` file back for step 2b. Where that private key lives is not recorded anywhere in this repo — the owner has to answer that before the nightly dumps count as a restore path |

**Checked on the production host 2026-09-06:** the nightly GPG-encrypted
`-Fc` dump above runs and prunes; the pre-update snapshots from `update.sh`
sit under `backups/` at the checkout root (three, 36–38 MB each). Re-check
with `crontab -l` and `ls -lt /var/backups/gdx backups/` and record the
answer in the drill table when you drill.

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
| 2026-09-06 | **production host**, `gdx-pre-update-20260905-051116.sql.gz` (38 MB) into `gdx_restored`; plus a fresh `-Fc` dump of live into `gdx_restored2` | 27 s | pass — customers 412/412, jobs 288/288, invoices 367/367, estimates 126/126, users 14/14; audit_logs 29549 vs 29688 live (a day of activity since the snapshot), 0 rows missing chain columns; head 086; `-Fc` round trip 286/286 tables; both scratch DBs dropped; `/health` 200 throughout |
