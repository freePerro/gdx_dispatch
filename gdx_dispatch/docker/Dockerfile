# ── Stage 1: Build Vue frontend ──────────────────────────────────────────────
FROM node:20-slim AS frontend-builder

WORKDIR /build
COPY gdx_dispatch/frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY gdx_dispatch/frontend/ ./

# Inject GIT_SHA_SHORT just before the build so this RUN's cache key
# changes whenever the SHA changes (otherwise Docker would re-use the
# cached "npm run build" layer with a stale 'unknown' bundle).
ARG GIT_SHA_SHORT=unknown
RUN GIT_SHA_SHORT="${GIT_SHA_SHORT}" npm run build && \
    echo "Built bundle for SHA=${GIT_SHA_SHORT}"

# ── Stage 2: Python application ─────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# System deps. postgresql-client-16 is pinned to match the server (postgres:16-alpine
# in docker-compose) — pave_tenant_db.py runs pg_dump/psql here, and a version
# mismatch (e.g. pg_dump 17 against a v16 server) injects unknown GUCs like
# `transaction_timeout` that ON_ERROR_STOP=on would refuse. Sourced from the
# official PGDG apt repo since Debian bookworm only ships client-15.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg xz-utils \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
         -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" \
         > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends \
        postgresql-client-16 \
        libgobject-2.0-0 libpango-1.0-0 libpangocairo-1.0-0 libcairo2 \
        libgdk-pixbuf-2.0-0 libffi-dev shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

# s6-overlay: the process supervisor that lets one app container run web,
# both Celery workers and beat (docker-compose.yml runs /init as the
# entrypoint; a bare `docker run` of this image still runs the CMD below, web
# alone). Pinned by version and checksum; the sums are upstream's .sha256
# files for 3.2.3.2. TARGETARCH is set by BuildKit (amd64 or arm64).
ARG S6_OVERLAY_VERSION=3.2.3.2
ARG TARGETARCH
RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
      amd64) arch=x86_64;  sum=e6befcc96a437a3831386ecfc51808c5d3e939dc5fe3c02ae9284599e8aa2408 ;; \
      arm64) arch=aarch64; sum=b17f17a82e7a515c682a91edaf2ffdabb73f891981b6c1fd712115693a2f8b4c ;; \
      *) echo "no s6-overlay build for ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    base="https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}"; \
    curl -fsSL -o /tmp/s6-noarch.tar.xz "${base}/s6-overlay-noarch.tar.xz"; \
    curl -fsSL -o /tmp/s6-arch.tar.xz "${base}/s6-overlay-${arch}.tar.xz"; \
    echo "5379750ed30a84bbd2e2dd74847ba6b5bd29cd0b2e3ea2ec58049b57eb2eda12  /tmp/s6-noarch.tar.xz" | sha256sum -c -; \
    echo "${sum}  /tmp/s6-arch.tar.xz" | sha256sum -c -; \
    tar -C / -Jxpf /tmp/s6-noarch.tar.xz; \
    tar -C / -Jxpf /tmp/s6-arch.tar.xz; \
    rm /tmp/s6-*.tar.xz
# A failed boot step (migrations, bootstrap) stops the container instead of
# leaving the processes waiting on it.
# At shutdown s6 waits S6_SERVICES_GRACETIME for the processes to stop before
# killing everything. The default 3 s is shorter than a Celery warm shutdown:
# a halt from svc-finish then killed the supervisors mid-stop, s6 respawned
# them, a worker came back up and the container never exited (tested
# 2026-10-19). 50 s outlasts the longest timeout-kill (45 s, which SIGKILLs a
# stuck process), so every stop completes; compose's stop_grace_period is 60 s.
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_SERVICES_GRACETIME=50000

# Install uv for fast dependency installation, then install Python deps from
# the LOCK, not the ranges in requirements.txt: every package is pinned there,
# so this build installs the set CI tested instead of whatever PyPI resolves to
# today (GDXA-467; the GDXA-14 SQLAlchemy 2.1 break was the ranges re-resolving).
# --no-deps: a package missing from the lock fails the import, loudly, rather
# than being resolved fresh here; `uv pip check` refuses an inconsistent set.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY gdx_dispatch/requirements.lock ./requirements.lock
RUN uv pip install --system --no-cache --no-deps -r requirements.lock \
    && uv pip check --system

# Copy application
COPY gdx_dispatch/ ./gdx_dispatch/

# The s6 service tree (gdx_dispatch/docker/s6-overlay/README.md).
COPY gdx_dispatch/docker/s6-overlay/ /etc/s6-overlay/
RUN chmod +x /etc/s6-overlay/scripts/* /etc/s6-overlay/s6-rc.d/*/run /etc/s6-overlay/s6-rc.d/*/finish

# Copy built frontend from stage 1
COPY --from=frontend-builder /build/dist ./gdx_dispatch/frontend/dist

# The release this image is. The Release workflow passes the tag's version;
# local and CI builds leave it empty. core/app_version.py reads it when
# APP_VERSION is `latest` or unset, so an install tracking :latest still
# knows which release it is running (the What's new banner keys on it).
ARG GDX_IMAGE_VERSION=""
ENV GDX_IMAGE_VERSION=${GDX_IMAGE_VERSION}

# Non-root user. `/app/uploads` and `/app/recordings` are pre-created
# here so the `gdx_uploads` / `gdx_recordings` named volumes inherit
# appuser ownership on first mount (Docker adopts the image's
# target-path permissions when the volume is empty). Without this
# pre-creation the volume initializes as root:root and the app cannot
# write.
RUN useradd -m appuser \
    && mkdir -p /app/uploads /app/recordings /plugins \
    && chmod +x /app/gdx_dispatch/docker/entrypoint.sh \
    && chown -R appuser /app /plugins
USER appuser

EXPOSE 8000

# The entrypoint runs migrations + first-run bootstrap (idempotent) and then
# exec's CMD. This is what lets a fresh `docker compose up` actually log in.
# Compose replaces both with /init (s6), whose web process runs the same
# command (s6-overlay/scripts/gdx-run; test_s6_service_tree keeps them equal).
ENTRYPOINT ["/app/gdx_dispatch/docker/entrypoint.sh"]
# --timeout-worker-healthcheck: the multiprocess manager pings each worker and
# SIGKILLs any that miss the deadline (default 5s). Brief CPU/GIL stalls were
# tripping it in prod, killing healthy workers mid-request → sporadic 502s
# ("upstream prematurely closed connection" in nginx). 30s still reaps a truly
# hung worker but tolerates transient stalls.
CMD ["uvicorn", "gdx_dispatch.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--timeout-worker-healthcheck", "30", \
     "--access-log"]
