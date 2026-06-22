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
        ca-certificates curl gnupg \
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

# Install uv for fast dependency installation, then install Python deps
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY gdx_dispatch/requirements.txt ./requirements.txt
RUN uv pip install --system --no-cache -r requirements.txt

# Copy application
COPY gdx_dispatch/ ./gdx_dispatch/

# Copy built frontend from stage 1
COPY --from=frontend-builder /build/dist ./gdx_dispatch/frontend/dist

# Non-root user. `/app/uploads` is pre-created here so the
# `gdx_uploads` named volume inherits appuser ownership on first
# mount (Docker adopts the image's target-path permissions when the
# volume is empty). Without this pre-creation the volume initializes
# as root:root and the app cannot write.
RUN useradd -m appuser \
    && mkdir -p /app/uploads \
    && chmod +x /app/gdx_dispatch/docker/entrypoint.sh \
    && chown -R appuser /app
USER appuser

EXPOSE 8000

# The entrypoint runs migrations + first-run bootstrap (idempotent) and then
# exec's CMD. This is what lets a fresh `docker compose up` actually log in.
ENTRYPOINT ["/app/gdx_dispatch/docker/entrypoint.sh"]
CMD ["uvicorn", "gdx_dispatch.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--access-log"]
