#!/usr/bin/env bash
# gdx/scripts/backup.sh — Daily backup of all tenant DBs + control plane DB to S3
#
# Usage: ./backup.sh
# Env vars required:
#   CONTROL_DATABASE_URL   — psql-compatible URL for control plane DB
#   DB_HOST, DB_PORT, DB_USER, DB_PASSWORD — PostgreSQL credentials
#   AWS_S3_BACKUP_BUCKET   — e.g. gdx-backups
#   AWS_KMS_KEY_ID         — KMS key ARN for server-side encryption
#
# RTO: 4 hours  RPO: 24 hours
# Test restore monthly: see gdx_dispatch/docs/RESTORE_RUNBOOK.md

set -euo pipefail

DATE=$(date +%Y-%m-%d)
HOUR=$(date +%Y-%m-%dT%H)
BACKUP_DIR=$(mktemp -d)
BUCKET="${AWS_S3_BACKUP_BUCKET:-gdx-backups}"
KMS="${AWS_KMS_KEY_ID:-}"
PGCONN="postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT:-5432}"

log() { echo "$(date -Iseconds) [backup] $*"; }

cleanup() { rm -rf "$BACKUP_DIR"; }
trap cleanup EXIT

# ── Control plane backup (hourly) ──────────────────────────────────────────
log "Backing up control plane DB..."
pg_dump -Fc "${PGCONN}/gdx_control" > "${BACKUP_DIR}/gdx_control.dump"
aws s3 cp "${BACKUP_DIR}/gdx_control.dump" \
  "s3://${BUCKET}/control/${HOUR}.dump" \
  ${KMS:+--sse aws:kms --sse-kms-key-id "$KMS"}
log "Control plane backup uploaded."

# ── Tenant backups (daily) ─────────────────────────────────────────────────
log "Fetching active tenant list..."
SLUGS=$(psql "${CONTROL_DATABASE_URL}" -At \
  -c "SELECT slug FROM tenants WHERE deleted_at IS NULL AND db_provisioned = true ORDER BY slug;")

COUNT=0
for slug in $SLUGS; do
  log "Backing up tenant: ${slug}"
  pg_dump -Fc "${PGCONN}/gdx_${slug}" > "${BACKUP_DIR}/${slug}.dump"
  aws s3 cp "${BACKUP_DIR}/${slug}.dump" \
    "s3://${BUCKET}/tenants/${slug}/${DATE}.dump" \
    ${KMS:+--sse aws:kms --sse-kms-key-id "$KMS"}
  rm "${BACKUP_DIR}/${slug}.dump"
  COUNT=$((COUNT + 1))
  log "  ✓ ${slug} uploaded."
done

log "Backup complete. ${COUNT} tenant(s) + control plane."

# ── Retention cleanup (30-day) ─────────────────────────────────────────────
CUTOFF=$(date -d '30 days ago' +%Y-%m-%d 2>/dev/null || date -v-30d +%Y-%m-%d)
log "Cleaning up backups older than ${CUTOFF}..."
aws s3 ls "s3://${BUCKET}/tenants/" --recursive \
  | awk '{print $4}' \
  | grep -E '/[0-9]{4}-[0-9]{2}-[0-9]{2}\.dump$' \
  | while read -r key; do
      file_date=$(basename "$key" .dump)
      if [[ "$file_date" < "$CUTOFF" ]]; then
        aws s3 rm "s3://${BUCKET}/${key}"
        log "Deleted old backup: s3://${BUCKET}/${key}"
      fi
    done

log "Retention cleanup done."
