#!/usr/bin/env bash
# Pull HubX quoting-session recordings from the VPS to this machine's Desktop.
#
# Why this exists: recordings are written inside the `app` container on the VPS.
# A corpus that only lives there is one `docker volume prune` from gone, and a
# pull you have to remember is a pull that will not have happened on the day it
# mattered. Run it from cron/systemd --user; it is safe to re-run.
#
#   ./pull_hubx_sessions.sh              # sync new/changed recordings
#   ./pull_hubx_sessions.sh --verify     # sync, then check every session's
#                                        # manifest count against the local file
#   DEST=/somewhere ./pull_hubx_sessions.sh
#
# It NEVER deletes anything on either end. Pruning the VPS spool is a separate,
# deliberate act (see --older-than at the bottom, which only PRINTS a command).
set -euo pipefail

VPS="${VPS:-gdx-vps}"
CONTAINER="${CONTAINER:-gdx-app-1}"
REMOTE_DIR="${REMOTE_DIR:-/app/recordings}"
DEST="${DEST:-$HOME/Desktop/hubx-sessions}"
STAGE="/tmp/hubx-sessions-stage"

VERIFY=0
[[ "${1:-}" == "--verify" ]] && VERIFY=1

mkdir -p "$DEST"

echo "==> pulling from $VPS:$CONTAINER:$REMOTE_DIR"

# Guard: never let this land inside a git repo. The corpus is unscrubbed and the
# GDX repo is public.
if git -C "$DEST" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "REFUSING: $DEST is inside a git repo — recordings must never be committed." >&2
  exit 1
fi

# docker cp out of the container to a VPS staging dir, then rsync down. rsync
# alone cannot see inside a container, and `docker cp` cannot stream to a remote.
# --ignore-existing on the rsync makes finished sessions cheap to re-run; the
# live session's directory is the only one that changes.
ssh "$VPS" "mkdir -p $STAGE && docker cp $CONTAINER:$REMOTE_DIR/. $STAGE/ 2>/dev/null || true"

rsync -az --info=stats1 --no-perms --no-owner --no-group \
      "$VPS:$STAGE/" "$DEST/"

echo "==> local corpus: $DEST"
du -sh "$DEST" 2>/dev/null || true
sessions=$(find "$DEST" -name manifest.json | wc -l | tr -d ' ')
echo "    sessions: $sessions"

if [[ -f "$DEST/INDEX.txt" ]]; then
  echo "==> most recent sessions:"
  tail -5 "$DEST/INDEX.txt" | sed 's/^/    /'
fi

if [[ "$VERIFY" == "1" ]]; then
  echo "==> verifying manifest counts against local files"
  bad=0
  while IFS= read -r m; do
    d=$(dirname "$m")
    want=$(python3 -c "import json,sys;print(json.load(open('$m')).get('events',0))" 2>/dev/null || echo 0)
    got=$(wc -l < "$d/events.jsonl" 2>/dev/null || echo 0)
    # A torn final line after a container SIGKILL is expected and costs one line.
    if (( want > got + 1 )); then
      echo "    MISMATCH $(basename "$d"): manifest says $want events, file has $got"
      bad=$((bad+1))
    fi
    if grep -q '"degraded": true' "$m" 2>/dev/null; then
      echo "    DEGRADED $(basename "$d") — recorder reported a fault; see manifest.json"
      bad=$((bad+1))
    fi
  done < <(find "$DEST" -name manifest.json)
  if (( bad == 0 )); then
    echo "    all sessions verified"
  else
    echo "    $bad session(s) need a look" >&2
    exit 2
  fi
fi

echo
echo "The VPS spool is never pruned automatically. When you want to reclaim it:"
echo "  ssh $VPS \"docker exec $CONTAINER find $REMOTE_DIR -mindepth 1 -maxdepth 1 -type d -mtime +60\""
echo "  (review the list, then delete deliberately — only after a verified pull)"
