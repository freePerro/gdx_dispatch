#!/bin/bash
# =============================================================================
# GDX Dispatch — VPS full backup (current stack)
# =============================================================================
# Backs up the LIVE stack on the VPS: prod + demo databases, compose/env
# config, nginx binds, upload/plugin volumes, letsencrypt, crontab, and a
# docker state snapshot. Complements — does NOT replace — the 02:00
# encrypted pg_dump cron in root's crontab (30-day retention).
#
# Run ON the VPS as root:
#   bash backup_everything.sh                # full: DBs + config + volumes
#   bash backup_everything.sh --db-only      # databases only
#   bash backup_everything.sh --config-only  # config only (no DBs/volumes)
#   bash backup_everything.sh --drill        # verify latest backups, write nothing
#
# From the workstation (always runs the repo's CURRENT version — the VPS
# checkout sits on the last release tag, so don't rely on it being fresh):
#   ssh gdx-vps 'bash -s -- --db-only' < tools/backup_everything.sh
#
# Destination: /root/gdx-backups/<UTC timestamp>/   (pruned to the last 5)
# =============================================================================
# No `set -e`: every section reports its own ✓/✗; failures are collected and
# the script exits 1 if ANY section failed (fail-loudly, finish-everything).
set -uo pipefail

TS=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_ROOT=/root/gdx-backups
DEST="$BACKUP_ROOT/$TS"
REPO=/var/www/gdx_dispatch/gdx_dispatch
NIGHTLY_DIR=/var/backups/gdx
KEEP=5

FAILURES=()
ok()   { echo "  ✓ $*"; }
fail() { echo "  ✗ $*"; FAILURES+=("$*"); }

MODE=full
case "${1:-}" in
    "")            ;;
    --db-only)     MODE=db ;;
    --config-only) MODE=config ;;
    --drill)       MODE=drill ;;
    *) echo "usage: backup_everything.sh [--db-only|--config-only|--drill]"; exit 2 ;;
esac

# The host has no postgres tools — dump/verify happen inside each DB container.
dump_db() {  # dump_db <container> <outfile>
    local c=$1 out=$2
    if ! docker exec "$c" pg_dump -Fc -U gdx -d gdx > "$out" 2>/dev/null; then
        fail "pg_dump $c"; rm -f "$out"; return
    fi
    if docker cp "$out" "$c":/tmp/.bk_verify.dump >/dev/null 2>&1 \
        && docker exec "$c" pg_restore --list /tmp/.bk_verify.dump >/dev/null 2>&1; then
        docker exec "$c" rm -f /tmp/.bk_verify.dump >/dev/null 2>&1
        ok "$(basename "$out") ($(du -h "$out" | cut -f1)) [pg_restore --list verified]"
    else
        docker exec "$c" rm -f /tmp/.bk_verify.dump >/dev/null 2>&1
        fail "verify $(basename "$out")"
    fi
}

tar_dir() {  # tar_dir <outfile> <parent-dir> <name...>
    local out=$1 parent=$2; shift 2
    if tar czf "$out" -C "$parent" "$@" 2>/dev/null && tar tzf "$out" >/dev/null 2>&1; then
        ok "$(basename "$out") ($(du -h "$out" | cut -f1))"
    else
        fail "tar $(basename "$out")"; rm -f "$out"
    fi
}

tar_volume() {  # tar_volume <docker-volume-name>
    local vol=$1 src="/var/lib/docker/volumes/$1/_data"
    if [ ! -d "$src" ]; then fail "volume $vol not found"; return; fi
    tar_dir "$DEST/volumes/${vol}.tar.gz" "$src" .
}

# ── drill: read-only verification of the newest backups ─────────────────
if [ "$MODE" = drill ]; then
    echo "=== Backup drill (read-only) — $TS ==="
    nightly=$(ls -t "$NIGHTLY_DIR"/gdx_live_*.dump.gpg 2>/dev/null | head -1)
    if [ -z "$nightly" ]; then
        fail "no nightly gdx_live_*.dump.gpg found in $NIGHTLY_DIR"
    else
        age_h=$(( ($(date +%s) - $(stat -c %Y "$nightly")) / 3600 ))
        size=$(stat -c %s "$nightly")
        [ "$age_h" -le 26 ] && ok "nightly $(basename "$nightly") age ${age_h}h" \
                            || fail "nightly is ${age_h}h old (>26h — cron broken?)"
        [ "$size" -ge 5000000 ] && ok "nightly size $(du -h "$nightly" | cut -f1)" \
                                || fail "nightly suspiciously small ($size bytes)"
    fi
    latest=$(ls -dt "$BACKUP_ROOT"/*/ 2>/dev/null | head -1)
    if [ -z "$latest" ]; then
        fail "no full backups in $BACKUP_ROOT yet"
    else
        echo "  latest full backup: $latest"
        prod_dump="$latest/databases/gdx_prod.dump"
        if [ -f "$prod_dump" ]; then
            if docker cp "$prod_dump" gdx-db-1:/tmp/.bk_verify.dump >/dev/null 2>&1 \
                && docker exec gdx-db-1 pg_restore --list /tmp/.bk_verify.dump >/dev/null 2>&1; then
                ok "prod dump restorable (pg_restore --list)"
            else
                fail "prod dump in latest full backup does NOT verify"
            fi
            docker exec gdx-db-1 rm -f /tmp/.bk_verify.dump >/dev/null 2>&1
        else
            fail "latest full backup has no databases/gdx_prod.dump"
        fi
        while IFS= read -r t; do
            tar tzf "$t" >/dev/null 2>&1 && ok "$(basename "$t") readable" \
                                         || fail "$(basename "$t") unreadable"
        done < <(find "$latest" -name '*.tar.gz' 2>/dev/null)
    fi
    echo ""
    if [ ${#FAILURES[@]} -gt 0 ]; then
        echo "DRILL FAILED (${#FAILURES[@]}):"; printf '  - %s\n' "${FAILURES[@]}"; exit 1
    fi
    echo "DRILL OK"; exit 0
fi

# ── backup proper ───────────────────────────────────────────────────────
mkdir -p "$DEST"
echo "=== GDX VPS backup — $TS (mode: $MODE) ==="
echo "Destination: $DEST"

if [ "$MODE" != config ]; then
    echo "▸ Databases..."
    mkdir -p "$DEST/databases"
    dump_db gdx-db-1      "$DEST/databases/gdx_prod.dump"
    dump_db gdx-demo-db-1 "$DEST/databases/gdx_demo.dump"
fi

if [ "$MODE" != db ]; then
    echo "▸ Config..."
    mkdir -p "$DEST/config"
    # Compose files + .env + local overrides for prod AND demo (docker/demo).
    tar_dir "$DEST/config/compose-docker-dir.tar.gz" "$REPO" docker
    # gdx-nginx binds its config + landing pages from the legacy path.
    tar_dir "$DEST/config/nginx-binds.tar.gz" /var/www/gdx infra landing
    tar_dir "$DEST/config/letsencrypt.tar.gz" /etc letsencrypt
    crontab -l > "$DEST/config/root-crontab.txt" 2>/dev/null \
        && ok "root-crontab.txt" || fail "crontab export"
    {
        echo "# git state of $REPO"
        git -C "$REPO" describe --tags --always 2>/dev/null
        git -C "$REPO" status --short 2>/dev/null
        echo; echo "# docker ps"; docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'
        echo; echo "# volumes";   docker volume ls --format '{{.Name}}'
        echo; echo "# ufw";       ufw status verbose 2>/dev/null
    } > "$DEST/config/system-state.txt" && ok "system-state.txt"
fi

if [ "$MODE" = full ]; then
    echo "▸ Volumes (uploads + plugins, prod + demo)..."
    mkdir -p "$DEST/volumes"
    tar_volume docker_gdx_uploads
    tar_volume gdx_gdx_plugins
    tar_volume gdx-demo_demo_uploads
    tar_volume gdx-demo_demo_plugins
    # docker_db_data is NOT tarred: the pg_dump above is the restorable
    # form; a file-level copy of a running postgres dir is not.
fi

echo "▸ Manifest..."
( cd "$DEST" && find . -type f ! -name MANIFEST.txt -exec sha256sum {} \; ) \
    > "$DEST/MANIFEST.txt" 2>/dev/null
{
    echo "# $TS mode=$MODE host=$(hostname)"
    echo "# total: $(du -sh "$DEST" | cut -f1)"
} >> "$DEST/MANIFEST.txt"
ok "MANIFEST.txt ($(grep -c . "$DEST/MANIFEST.txt") lines)"

echo "▸ Pruning (keep last $KEEP)..."
if [ "$BACKUP_ROOT" = /root/gdx-backups ]; then
    ls -dt "$BACKUP_ROOT"/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | while IFS= read -r old; do
        rm -rf "$old" && echo "  pruned $old"
    done
fi

echo ""
echo "Total: $(du -sh "$DEST" | cut -f1)  →  $DEST"
if [ ${#FAILURES[@]} -gt 0 ]; then
    echo "BACKUP FINISHED WITH FAILURES (${#FAILURES[@]}):"
    printf '  - %s\n' "${FAILURES[@]}"
    exit 1
fi
echo "BACKUP OK"
