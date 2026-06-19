#!/bin/bash
# backup-db.sh — Daily automated database backup for GDX Dispatch
# Cron: 0 2 * * * /opt/gdx_dispatch/scripts/backup-db.sh >> /var/log/gdx-backup.log 2>&1

set -euo pipefail

BACKUP_DIR="/var/backups/gdx"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/dispatch_dev_${TIMESTAMP}.sql.gz"
INVENTORY="${BACKUP_DIR}/inventory.log"

mkdir -p "${BACKUP_DIR}"

echo "[$(date -Iseconds)] [backup] Starting backup..."

# Dump from the running postgres container
docker exec gdx-postgres-dev pg_dump -U gdx gdx 2>/dev/null | gzip > "${BACKUP_FILE}"

SIZE=$(du -sh "${BACKUP_FILE}" | cut -f1)
echo "[$(date -Iseconds)] [backup] Written: ${BACKUP_FILE} (${SIZE})"

# Append to inventory
echo "$(date -Iseconds) dev ${BACKUP_FILE} ${SIZE} local" >> "${INVENTORY}"

# Prune backups older than 30 days
find "${BACKUP_DIR}" -name "dispatch_dev_*.sql.gz" -mtime +30 -delete
echo "[$(date -Iseconds)] [backup] Pruned backups older than 30 days"

echo "[$(date -Iseconds)] [backup] DONE"
