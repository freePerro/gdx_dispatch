# GDX Dispatch

A single-tenant, self-hosted **field-service management platform** for
service business — scheduling and dispatch, jobs and work orders,
estimates and proposals, invoicing, inventory, payroll, and customer records.
FastAPI backend, Vue 3 single-page frontend.

## Features

The platform spans the full field-service workflow, organized as feature
modules under [`gdx_dispatch/modules/`](gdx_dispatch/modules/) and HTTP routes
under [`gdx_dispatch/routers/`](gdx_dispatch/routers/):

- **Dispatch & scheduling** — appointments, GPS dispatch, service areas
- **Sales** — estimates, proposals, change orders, catalogs & pricing
- **Operations** — work orders, inventory, purchase orders, timeclock
- **Finance** — invoicing, payroll, commissions, tax, QuickBooks sync, vendor statements
- **Customer engagement** — customer portal, segments, winback & follow-ups, communications, notifications
- **Telephony & email** — Phone.com (click-to-call, MMS) and Outlook integrations
- **AI assists** — AI estimates, communication drafting, health scoring, forecasting
- **Plugins** — operator-installable modules run in a sandboxed plugin-host
  container ([`gdx_dispatch/plugin_host/`](gdx_dispatch/plugin_host/), ADR-013)

Access is **permission-driven** with role canonicalization — see
[`docs/ROLE_AND_NAV_NAMING_CONVENTIONS.md`](gdx_dispatch/docs/ROLE_AND_NAV_NAMING_CONVENTIONS.md).

## Stack

- **Backend** — FastAPI (Python 3.11+), SQLAlchemy 2, served by Uvicorn
- **Frontend** — Vue 3 + PrimeVue, built with Vite
- **Database** — PostgreSQL **16 or newer** (migration 029's `grant_helpers.py` uses `pg_input_is_valid`, which does not exist on 15 — a fresh install on 15 fails at that migration; every compose file pins `postgres:16-alpine`), schema managed by Alembic
- **Cache / broker** — Redis 7
- **Background jobs** — Celery (priority queues + beat scheduler)
- **Telemetry** — in-app error sink (`modules/error_sink`) + OpenTelemetry (optional)

## Project structure

The application package is `gdx_dispatch/`; the inner `gdx_dispatch/gdx_dispatch/`
directory holds the Python source.

```
gdx_dispatch/                  repo root
├── gdx_dispatch/              application package
│   ├── app.py                 FastAPI application factory
│   ├── main.py                ASGI entrypoint  (uvicorn gdx_dispatch.main:app)
│   ├── celery_app.py          Celery app (workers + beat)
│   ├── routers/               HTTP API routes
│   ├── api/                   public API router
│   ├── modules/               feature modules (quickbooks, payroll, inventory, …)
│   ├── models/                SQLAlchemy ORM models
│   ├── services/              domain services (pricing engine, …)
│   ├── core/                  shared services, settings, auth helpers
│   ├── integrations/          third-party integration glue
│   ├── plugin_host/           sandboxed plugin runtime (separate container)
│   ├── plugin_api/            stable API surface exposed to plugins
│   ├── tasks/                 Celery task definitions
│   ├── migrations/            Alembic migrations (+ squashed baseline)
│   ├── frontend/              Vue 3 + PrimeVue SPA (Vite)
│   ├── docs/                  developer, admin, and runbook guides
│   ├── docker/                Docker Compose stacks + Dockerfiles
│   ├── tests/                 pytest suite
│   ├── requirements.txt       Python dependencies (ranges)
│   └── requirements.lock      every package pinned; what CI and the images install
├── docs/                      ops & runbook docs
└── pyproject.toml
```

The Vue frontend lives in [`gdx_dispatch/frontend/`](gdx_dispatch/frontend/),
with its entrypoint at [`gdx_dispatch/frontend/src/main.js`](gdx_dispatch/frontend/src/main.js)
and root component at [`gdx_dispatch/frontend/src/App.vue`](gdx_dispatch/frontend/src/App.vue).

## Quick start (Docker)

Requires Docker and Docker Compose. Local development runs the same compose
layout every install runs (`gdx_dispatch/docker/docker-compose.yml`) plus the
dev overlay (`docker-compose.dev.yml`), which builds the images from this
checkout. `dev-up.sh` wraps both, stamps the git SHA into the build, and creates
the dev overlay's two `external` volumes (so `docker compose down -v` can never
delete the dev database) on first run:

```bash
cp .env.template .env
# Secrets are minted on first boot; .env only needs what you want to override.

gdx_dispatch/docker/dev-up.sh --build            # add --reload for hot reload
```

This brings up PostgreSQL, Redis, the API, the plugin host and the Celery
workers as compose project `docker`. The API is exposed on
<http://localhost:8001> with a health check at `/health`.

On first start the API container automatically (and idempotently) runs database
migrations, creates the application tables, and seeds a single default tenant
plus an **initial admin user** so you can log in right away:

- Email: `GDX_ADMIN_EMAIL` (default `admin@example.com`)
- Password: `GDX_ADMIN_PASSWORD` if you set it, otherwise a random password is
  generated and printed to the container logs:

  ```bash
  docker logs docker-app-1 2>&1 | grep -A4 "initial admin account"
  ```

The admin is created with `must_change_password` set — change it after your
first login.

## Self-hosting on your own VPS (one command, no configuration)

The easiest way to run GDX Dispatch is the compose file published with every
release. It is `gdx_dispatch/docker/docker-compose.yml`, the same layout
production runs, attached to the release as `docker-compose.customer.yml`. It needs **no `.env`, no cloned repo, and no <!-- release asset name; link-ok -->
secret to fill in** — a `secrets-init` container mints strong secrets on first
boot and persists them in a volume, so restarts and updates keep your logins
and encrypted data.

Requirements: a Linux VPS with [Docker + Compose v2](https://get.docker.com)
(any Hostinger/DigitalOcean "Docker" image works), ports 80/443 reachable,
and — optionally — a domain with an A record pointing at the server.

```bash
mkdir gdx && cd gdx
curl -fsSL https://github.com/freePerro/gdx_dispatch/releases/latest/download/docker-compose.customer.yml -o docker-compose.customer.yml  # release asset name; link-ok
echo 'GDX_DOMAIN=dispatch.example.com' > .env   # optional — enables automatic HTTPS via Let's Encrypt
docker compose -p gdx -f docker-compose.customer.yml up -d  # release asset name; link-ok
```

That's the whole install. On first boot the stack mints its secrets, runs all
database migrations, seeds your admin account, and (with `GDX_DOMAIN` set)
obtains a real TLS certificate automatically. Without a domain it serves plain
HTTP on the server's IP. Read your one-time admin password from the app log:

```bash
docker compose -p gdx logs app | grep -A5 "initial admin"
```

Optional settings go in the same `.env` (all documented in the compose file's
header): `GDX_TENANT_NAME`, `GDX_ADMIN_EMAIL`, `GDX_ADMIN_PASSWORD`,
`APP_VERSION` to pin a release, and integration keys (Stripe, QuickBooks,
Google Maps, SMTP) — every integration is optional and its feature simply
stays off while blank. A backup sidecar dumps the database daily to the
`gdx_backups` volume (30-day retention). To update:

```bash
docker compose -p gdx -f docker-compose.customer.yml pull  # release asset name; link-ok
docker compose -p gdx -f docker-compose.customer.yml up -d  # release asset name; link-ok
```

Data lives in named volumes (`gdx_db_data`, `gdx_uploads`, `gdx_recordings`,
`gdx_plugins`, `gdx_redis_data`, `gdx_backups`, `gdx_secrets`);
`docker compose down` without `-v` never touches them.

Full guides on the wiki:
[Getting Started](https://github.com/freePerro/gdx_dispatch/wiki/Getting-Started) ·
[Updating](https://github.com/freePerro/gdx_dispatch/wiki/Updating) ·
[Backup & Restore](https://github.com/freePerro/gdx_dispatch/wiki/Backup-and-Restore)

## Self-hosting from a checkout

The same file runs from a clone of this repository, which is how production
runs. It pulls `ghcr.io/freeperro/gdx_dispatch:<APP_VERSION>` (`latest` when
unset); nothing is built. The image tag is the single source of truth for the
running version (reported by `/health` and the admin **update-check**).

```bash
echo 'APP_VERSION=1.0.0' >> .env   # or leave unset to track :latest
docker compose -p gdx --env-file ./.env -f gdx_dispatch/docker/docker-compose.yml up -d
```

A host that needs something the base does not do (production terminates TLS in
its own nginx and keeps volumes that predate this layout) adds an overlay and
names both files in `.env`, so every command and `update.sh` agree on them:

```bash
COMPOSE_FILE=gdx_dispatch/docker/docker-compose.yml:gdx_dispatch/docker/docker-compose.prod.yml
```

### Updating

To upgrade to a newer release, bump `APP_VERSION` in `.env` (or leave it on
`latest`) and run the updater — it snapshots the database, pulls the new image,
applies migrations on boot, and only starts the workers once `/health` is green:

```bash
./gdx_dispatch/docker/update.sh
# or pin explicitly:  APP_VERSION=1.1.0 ./gdx_dispatch/docker/update.sh
```

The pre-update database snapshot is written to `backups/` — keep it; it's your
rollback if a migration goes wrong (the script prints the exact restore steps if
the new image fails to come up). Admins can check whether a newer release exists
from **Settings → update-check** (`GET /api/admin/update-check`), which compares
the running version against the latest GitHub release.

## Local development

**Backend:**

```bash
python -m venv .venv && source .venv/bin/activate
pip install --no-deps -r gdx_dispatch/requirements.lock   # the set CI tests and the images ship
uvicorn gdx_dispatch.main:app --reload --port 8000
```

**Frontend:**

```bash
cd gdx_dispatch/frontend
npm install
npm run dev      # Vite dev server
npm run build    # production build (served by the backend in Docker)
```

## Configuration

All configuration is via environment variables. Copy `.env.template` to `.env`
and fill in the values — lines marked `[REQUIRED]` must be set for the app to
start; the template documents every option. **Never commit your `.env`.**

## Tests

**Backend** (pytest):

```bash
.venv/bin/pytest gdx_dispatch/tests/ -q
```

**Frontend** (Vitest):

```bash
cd gdx_dispatch/frontend && npx vitest run
```

## Documentation

Developer and admin guides live under
[`gdx_dispatch/docs/`](gdx_dispatch/docs/) — start with
[`CONTRIBUTING.md`](gdx_dispatch/docs/CONTRIBUTING.md),
[`DEVELOPER_GUIDE.md`](gdx_dispatch/docs/DEVELOPER_GUIDE.md), and
[`ADMIN_GUIDE.md`](gdx_dispatch/docs/ADMIN_GUIDE.md). Operations runbooks live
under [`docs/`](docs/).

## License

GDX Dispatch is released under the [GNU Affero General Public License v3](LICENSE)
(AGPLv3), with one additional permission: the
[Plugin Exception](PLUGIN-EXCEPTION.md).

What that means in practice:

- **The core stays open.** If you modify GDX Dispatch and run it for others
  to use — including as a hosted service — you must make your modified
  source available to those users (AGPLv3 §13). Improvements to the core
  can't be taken private.
- **Plugins can be anything.** Plugins that talk to GDX Dispatch only
  through the Plugin API may be released under any license, including
  proprietary ones. See the exception for the exact conditions.

Versions released before this change were published under the MIT License
and remain available under those terms.
