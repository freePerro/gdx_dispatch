#!/bin/sh
# GDX Dispatch installer and updater: one command for both.
#
#   curl -fsSL https://github.com/freePerro/gdx_dispatch/releases/latest/download/install.sh | sudo sh
#   curl -fsSL …/install.sh | sudo sh -s -- --domain dispatch.example.com --email you@example.com
#
# Each release publishes this file with GDX_RELEASE set to its own tag, so the
# compose file it downloads and the image version it pins always match. Run it
# again (the latest one) to update: it snapshots the database first, then pulls
# and restarts, keeping every volume and the secrets in them.
#
# Options: see usage() below.
#
# POSIX sh on purpose: `curl | sh` runs dash on Debian and Ubuntu.
set -eu

GDX_RELEASE="__GDX_RELEASE__"
REPO_URL="https://github.com/freePerro/gdx_dispatch"
PROJECT=gdx
HEALTH_TIMEOUT="${GDX_HEALTH_TIMEOUT:-600}"

usage() {
  cat <<'USAGE'
Options:
  --domain NAME      serve https://NAME (its DNS A record must point here)
  --email ADDRESS    login email of the first admin account
  --ip ADDRESS       with no domain, serve https://ADDRESS (a public IPv4;
                     default: this server's own when it is public);
                     --ip none serves plain HTTP
  --dir PATH         install directory (default /opt/gdx, or $GDX_DIR)
  --yes              never prompt
  --install-docker   install Docker with get.docker.com when it is missing
  --version X.Y.Z    a copy not taken from a release: which image to run
  --compose-file F   use this compose file instead of downloading one
  --no-pull          use images already loaded (rehearsing a local build)
USAGE
}
say() { printf '[gdx] %s\n' "$*"; }
warn() { printf '[gdx] WARNING: %s\n' "$*" >&2; }
die() { printf '[gdx] %s\n' "$*" >&2; exit 1; }

# Values land in .env, which compose reads and entrypoint.sh sources: keep them
# to characters that need no quoting.
check_values() {
  case "$domain" in *[!A-Za-z0-9.-]*) die "not a domain name: $domain" ;; esac
  case "$email" in *[!A-Za-z0-9.@_+-]*) die "not an email address: $email" ;; esac
  if [ -n "$email" ]; then
    case "$email" in *@*.*) ;; *) die "not an email address: $email" ;; esac
  fi
  case "$public_ip" in
    ''|none) ;;
    *[!0-9.]*|*..*|.*|*.) die "not an IPv4 address: $public_ip" ;;
    *.*.*.*.*) die "not an IPv4 address: $public_ip" ;;
    *.*.*.*)
      for o in $(printf '%s' "$public_ip" | tr . ' '); do
        [ "${#o}" -le 3 ] && [ "$o" -le 255 ] || die "not an IPv4 address: $public_ip"
      done ;;
    *) die "not an IPv4 address: $public_ip" ;;
  esac
}
# The address this server sends from, when it is a public one. `ip route get`
# sends nothing; behind NAT the source is private and the answer is empty
# (nothing outside is asked: --ip names the address then).
public_ipv4() {
  a="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
  case "$a" in
    10.*|127.*|169.254.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[01].*) ;;
    100.6[4-9].*|100.[7-9][0-9].*|100.1[01][0-9].*|100.12[0-7].*) ;;  # CGNAT, Tailscale
    *) printf '%s' "$a" ;;
  esac
}
wait_healthy() {
  app_id="$(compose ps -q app)"
  [ -n "$app_id" ] || die "the app container was not created; see: docker compose -p $PROJECT logs"
  waited=0
  status=""
  while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
    status="$(docker inspect -f '{{.State.Health.Status}}' "$app_id" 2>/dev/null || true)"
    [ "$status" = healthy ] && return 0
    if [ $((waited % 30)) = 0 ] && [ "$waited" -gt 0 ]; then
      say "…still starting (${waited}s, $status)"
    fi
    sleep 5
    waited=$((waited + 5))
  done
  warn "the app is not healthy after ${HEALTH_TIMEOUT}s (status: ${status:-unknown})."
  warn "watch it: cd $dir && docker compose -p $PROJECT logs -f app"
  [ "$existing" = 1 ] && warn "a long migration may still be running: do not restore $snapshot.gz while it is."
  exit 1
}
# Caddy asks Let's Encrypt for the certificate once the app is healthy; it
# usually has one within seconds. curl checks it against the system's CAs.
# --connect-to sends the probe to this host's own Caddy: behind NAT (the case
# --ip is for) the public address is unreachable from inside unless the router
# does hairpin NAT, and the probe would drop a certificate that was issued.
# (--resolve cannot do it: curl skips it for a URL that is an IP address.)
https_works() {
  command -v curl >/dev/null 2>&1 || { warn "curl is missing: https://$1 was not checked."; return 0; }
  # Where compose published 443 (GDX_HTTPS_PORT may move or bind it): asked of
  # compose, not parsed from .env, whose quoting, comment and shell-override
  # rules a copy would get wrong. A wildcard bind answers on loopback.
  b="$(compose port caddy 443 </dev/null 2>/dev/null | head -n 1)"
  port="${b##*:}"
  addr="${b%:*}"
  case "$addr" in ""|0.0.0.0|"[::]"|::) addr=127.0.0.1 ;; esac
  n=0
  until curl -fsS -o /dev/null --connect-timeout 5 --max-time 10 --connect-to "$1:443:$addr:${port:-443}" "https://$1/health" 2>/dev/null; do
    n=$((n + 1)); [ "$n" -lt 24 ] || return 1
    sleep 5
  done
}
major() { v="${1#v}"; printf '%s' "${v%%.*}"; }
set_env() {  # FILE KEY VALUE
  grep -v "^$2=" "$1" > "$1.tmp" || true
  printf '%s=%s\n' "$2" "$3" >> "$1.tmp"
  mv "$1.tmp" "$1"
}
compose() { docker compose -p "$PROJECT" --env-file "$env_file" -f "$compose_file" "$@"; }

# Under `curl | sh` stdin IS this script, read as it runs: a command that reads
# stdin (compose exec forwards it, even with -T) would swallow the lines not
# yet read. So every command runs inside main(), which the shell has read in
# full before the call at the bottom, and the exec calls read /dev/null.
main() {
dir="${GDX_DIR:-/opt/gdx}"
domain=""
email=""
public_ip=""
assume_yes=0
install_docker=0
version=""
compose_src=""
pull=1
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) domain="${2:?--domain needs a value}"; shift 2 ;;
    --email) email="${2:?--email needs a value}"; shift 2 ;;
    --ip) public_ip="${2:?--ip needs a value}"; shift 2 ;;
    --dir) dir="${2:?--dir needs a value}"; shift 2 ;;
    --yes|-y) assume_yes=1; shift ;;
    --install-docker) install_docker=1; shift ;;
    --version) version="${2:?--version needs a value}"; shift 2 ;;
    --compose-file) compose_src="${2:?--compose-file needs a value}"; shift 2 ;;
    --no-pull) pull=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

# ── Which release ──────────────────────────────────────────────────────────
if [ -z "$version" ]; then
  case "$GDX_RELEASE" in
    __*) die "this copy of install.sh is not from a release; pass --version X.Y.Z (and --compose-file to use a local file)" ;;
  esac
  version="$GDX_RELEASE"
fi
version="${version#v}"   # images are tagged 1.2.3, releases v1.2.3
case "$version" in
  *[!A-Za-z0-9._-]*|"") die "not a version: $version" ;;
esac

check_values

# ── Preflight ──────────────────────────────────────────────────────────────
if ! command -v docker >/dev/null 2>&1; then
  if [ "$install_docker" = 1 ]; then
    say "Installing Docker (get.docker.com)…"
    curl -fsSL https://get.docker.com | sh
  else
    die "Docker is not installed. Install it (https://docs.docker.com/engine/install/) or rerun with --install-docker."
  fi
fi
docker info >/dev/null 2>&1 \
  || die "cannot reach the Docker daemon: run as root (… | sudo sh) or as a user in the docker group."

engine="$(docker version --format '{{.Server.Version}}' 2>/dev/null || true)"
case "$(major "$engine")" in
  ''|*[!0-9]*) die "could not read the Docker version" ;;
esac
[ "$(major "$engine")" -ge 24 ] || die "Docker $engine is too old; 24 or newer is needed."
compose_v="$(docker compose version --short 2>/dev/null || true)"
case "$(major "$compose_v")" in
  ''|*[!0-9]*) die "Docker Compose v2 is missing (the 'docker compose' plugin)." ;;
esac
[ "$(major "$compose_v")" -ge 2 ] || die "Docker Compose $compose_v is too old; v2 or newer is needed."
# `up --pull never` (see Start); without it --no-pull cannot start a local image.
up_pull=""
docker compose up --help 2>/dev/null | grep -q -- '--pull' && up_pull="--pull never"
[ -n "$up_pull" ] || [ "$pull" = 1 ] || die "this Docker Compose has no 'up --pull'; --no-pull needs a newer one."

if [ -n "$compose_src" ]; then
  [ -r "$compose_src" ] || die "cannot read $compose_src"
  case "$compose_src" in /*) ;; *) compose_src="$PWD/$compose_src" ;; esac
fi
mkdir -p "$dir" 2>/dev/null || die "cannot create $dir: run as root (… | sudo sh) or pass --dir."
dir="$(cd "$dir" && pwd -P)"
cd "$dir"

# A stack named gdx that runs from another directory is someone else's
# install (an older README install, or a checkout run by update.sh). Taking it
# over from here would restart it without its .env. Compose records the path
# it was run from, which may go through a symlink: compare resolved paths.
other="$(docker ps -a --filter "label=com.docker.compose.project=$PROJECT" \
  --format '{{.Label "com.docker.compose.project.working_dir"}}' | sort -u | while IFS= read -r wd; do
    [ "$(cd "$wd" 2>/dev/null && pwd -P)" = "$dir" ] || printf '%s\n' "$wd"
  done)"
if [ -n "$other" ]; then
  die "a '$PROJECT' stack already runs from $(printf '%s' "$other" | head -n 1); rerun with --dir set to that directory."
fi
if [ -f .env ] && grep -q '^COMPOSE_FILE=' .env; then
  die "$dir/.env sets COMPOSE_FILE: this install is a checkout with overlays; update it with gdx_dispatch/docker/update.sh."
fi

existing=0
docker volume inspect "${PROJECT}_gdx_db_data" >/dev/null 2>&1 && existing=1

if [ "$existing" = 0 ]; then
  mem_kb="$(sed -n 's/^MemTotal: *\([0-9]*\) kB/\1/p' /proc/meminfo 2>/dev/null || true)"
  [ -n "$mem_kb" ] && [ "$mem_kb" -lt 3900000 ] && warn "less than 4 GB of RAM; the app may run out of memory."
  disk_kb="$(df -Pk "$dir" 2>/dev/null | awk 'NR==2 {print $4}')"
  [ -n "$disk_kb" ] && [ "$disk_kb" -lt 10000000 ] && warn "less than 10 GB free in $dir."
  if command -v ss >/dev/null 2>&1; then
    for port in 80 443; do
      if [ -n "$(ss -Hltn "sport = :$port" 2>/dev/null)" ]; then
        die "port $port is already in use; GDX serves the web on 80 and 443."
      fi
    done
  fi
fi

# ── Settings (.env) ────────────────────────────────────────────────────────
first_env=0
[ -f .env ] || first_env=1
if [ "$first_env" = 1 ] && [ "$existing" = 1 ]; then
  warn "data volumes from an earlier install exist but $dir/.env does not; earlier settings (GDX_DOMAIN …) are not carried over."
fi
if [ "$first_env" = 1 ] && [ "$assume_yes" = 0 ] && [ -z "$domain$email" ] \
   && [ -r /dev/tty ] && { : </dev/tty; } 2>/dev/null; then
  printf 'Domain for HTTPS (blank: HTTPS on this server'"'"'s public IP): ' >/dev/tty
  read -r domain </dev/tty || domain=""
  printf 'Admin login email [admin@example.com]: ' >/dev/tty
  read -r email </dev/tty || email=""
  check_values
fi

# Staged as .new files: an existing install is snapshotted with the files it
# runs now, and nothing is replaced unless the snapshot and the pull succeed.
trap 'rm -f .env.new docker-compose.yml.new' EXIT
umask 077
if [ -f .env ]; then cp .env .env.new; else : > .env.new; fi
chmod 600 .env.new   # an older README install wrote .env world-readable
set_env .env.new APP_VERSION "$version"
[ -z "$domain" ] || set_env .env.new GDX_DOMAIN "$domain"
[ -z "$email" ] || set_env .env.new GDX_ADMIN_EMAIL "$email"
# HTTPS with no domain: Caddy gets a Let's Encrypt certificate for the IP
# itself. Decided once, on the first run with no domain: an empty
# GDX_PUBLIC_IP line means plain HTTP until --ip or --domain says otherwise.
if [ "$public_ip" = none ]; then
  set_env .env.new GDX_PUBLIC_IP ""
elif [ -n "$public_ip" ]; then
  set_env .env.new GDX_PUBLIC_IP "$public_ip"
elif ! grep -q '^GDX_DOMAIN=.' .env.new && ! grep -q '^GDX_PUBLIC_IP=' .env.new; then
  set_env .env.new GDX_PUBLIC_IP "$(public_ipv4)"
fi

# ── Compose file ───────────────────────────────────────────────────────────
if [ -n "$compose_src" ]; then
  cp "$compose_src" docker-compose.yml.new
else
  curl -fsSL "$REPO_URL/releases/download/v$version/docker-compose.customer.yml" -o docker-compose.yml.new \
    || die "could not download the compose file for v$version."
fi
chmod 644 docker-compose.yml.new

# ── Snapshot before touching an existing install ──────────────────────────
snapshot=""
if [ "$existing" = 1 ]; then
  # An install from the README before this script named its file
  # docker-compose.customer.yml and may have no .env at all.
  env_file=/dev/null
  [ -f .env ] && env_file=.env
  # The old files' secrets-init (a dependency of db) sets pull_policy:
  # always, so a plain `up` pulled here: --no-pull broke, and a normal update
  # could replace the image the install runs before its snapshot (vps2
  # rehearsal, 2026-10-19). On the old files the snapshot uses what is on disk.
  snap_pull="$up_pull"
  if [ -f docker-compose.yml ]; then
    compose_file=docker-compose.yml
  elif [ -f docker-compose.customer.yml ]; then
    compose_file=docker-compose.customer.yml
  else
    env_file=.env.new compose_file=docker-compose.yml.new
    warn "no compose file in $dir: the database starts on the new one for the snapshot."
    # The new version's images are not pulled until after the snapshot.
    [ "$pull" = 1 ] && snap_pull=""
  fi
  # shellcheck disable=SC2086  # snap_pull is empty or two words
  compose up -d $snap_pull db </dev/null
  i=0
  until compose exec -T db pg_isready -U gdx -d gdx </dev/null >/dev/null 2>&1; do
    i=$((i + 1)); [ "$i" -le 60 ] || die "the database did not come up; nothing was changed."
    sleep 2
  done
  mkdir -p backups
  snapshot="backups/gdx-pre-update-$(date +%Y%m%d-%H%M%S).sql"
  say "Snapshotting the database -> $dir/$snapshot.gz"
  if ! compose exec -T db pg_dump -U gdx gdx </dev/null > "$snapshot"; then
    rm -f "$snapshot"
    die "the snapshot failed; nothing was changed."
  fi
  gzip "$snapshot"
fi

# ── Pull, then switch to the new files ─────────────────────────────────────
env_file=.env.new compose_file=docker-compose.yml.new
if [ "$pull" = 1 ]; then
  say "Pulling images for $version…"
  if ! compose pull </dev/null; then
    [ "$existing" = 1 ] && die "the pull failed; the install still runs its old files and images."
    die "the pull failed; nothing was installed. Run the same command again."
  fi
fi
mv .env.new .env
mv docker-compose.yml.new docker-compose.yml
env_file=.env compose_file=docker-compose.yml
if [ -f docker-compose.customer.yml ]; then
  rm -f docker-compose.customer.yml
  say "docker-compose.customer.yml is now docker-compose.yml."
fi

# ── Start ──────────────────────────────────────────────────────────────────
say "Starting (the first boot runs migrations and creates the admin account)…"
# app, secrets-init and plugin-host set pull_policy: always, so a plain `up`
# pulls again: after the pull above (a registry blip there would fail after the
# switch) and, with --no-pull, for an image that exists only on this machine.
# shellcheck disable=SC2086  # up_pull is empty or two words
compose up -d --remove-orphans $up_pull </dev/null
wait_healthy
# Read the first boot's banner now: the HTTP fallback below recreates app, and
# a recreated container's logs start empty (vps2 rehearsal, 2026-10-19).
banner=""
if [ "$existing" = 0 ]; then
  banner="$(compose logs app 2>&1 | grep -A3 'initial admin account created' | tail -n 3 || true)"
fi

cert_ip=""
grep -q '^GDX_DOMAIN=.' .env || cert_ip="$(sed -n 's/^GDX_PUBLIC_IP=//p' .env | tail -n 1)"
if [ -n "$cert_ip" ] && ! https_works "$cert_ip"; then
  # A certificate Caddy cannot get leaves only a redirect to a dead https://.
  warn "no certificate for https://$cert_ip after about 2 minutes: Let's Encrypt must reach ports 80 and 443 on that address from the internet."
  warn "serving plain HTTP instead; once those ports are open, rerun with --ip $cert_ip (or --domain)."
  set_env .env GDX_PUBLIC_IP ""
  cert_ip=""
  # shellcheck disable=SC2086
  compose up -d $up_pull </dev/null
  wait_healthy
fi

# ── Done ───────────────────────────────────────────────────────────────────
if grep -q '^GDX_DOMAIN=.' .env; then
  url="https://$(sed -n 's/^GDX_DOMAIN=//p' .env | tail -n 1)"
elif [ -n "$cert_ip" ]; then
  url="https://$cert_ip"
else
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p')"
  url="http://${ip:-<server-ip>}"
  warn "over plain HTTP a browser drops the login's refresh cookie, so sessions end after 15 minutes. Use --domain, or --ip with a public address."
fi
say "GDX Dispatch $version is running at $url"
if [ "$existing" = 0 ]; then
  if [ -n "$banner" ]; then
    printf '%s\n' "$banner" | sed -n 's/^.*\(email: .*\)$/  \1/p; s/^.*\(password: .*\)$/  \1/p'
    say "You will be asked to change this password at the first login."
  else
    say "Admin login: cd $dir && docker compose -p $PROJECT logs app | grep -A5 'initial admin'"
  fi
fi
}

main "$@"
