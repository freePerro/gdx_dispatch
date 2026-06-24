# GDX Dispatch

A single-tenant, self-hosted **field-service dispatch platform** — scheduling,
job and work-order management, estimates, invoicing, and customer records for a
garage-door service business. FastAPI backend, Vue 3 single-page frontend.

## Stack

- **Backend** — FastAPI (Python 3.11+), SQLAlchemy, served by Uvicorn
- **Frontend** — Vue 3 + PrimeVue, built with Vite
- **Database** — PostgreSQL 16
- **Cache / broker** — Redis 7
- **Background jobs** — Celery (priority queues + beat scheduler)
- **Telemetry** — Sentry (optional)

## Project structure

```
gdx_dispatch/
├── app.py            FastAPI application factory
├── main.py           ASGI entrypoint  (uvicorn gdx_dispatch.main:app)
├── routers/          HTTP API routes
├── models/           SQLAlchemy ORM models
├── core/             Celery app and shared services
├── frontend/         Vue 3 + PrimeVue SPA (Vite)
├── tests/            pytest suite
├── docker/           Docker Compose stacks + Dockerfile
└── requirements.txt  Python dependencies
```

The Vue frontend lives in [`gdx_dispatch/frontend/`](gdx_dispatch/frontend/),
with its entrypoint at [`gdx_dispatch/frontend/src/main.js`](gdx_dispatch/frontend/src/main.js)
and root component at [`gdx_dispatch/frontend/src/App.vue`](gdx_dispatch/frontend/src/App.vue).

## Quick start (Docker)

Requires Docker and Docker Compose.

```bash
cp .env.template .env
# Fill in the [REQUIRED] values (database password, SECRET_KEY, …)

docker compose -f gdx_dispatch/docker/docker-compose.yml up -d --build
```

This brings up PostgreSQL, Redis, the API, and the Celery workers. The API is
exposed on <http://localhost:8001> with a health check at `/health`.

On first start the API container automatically (and idempotently) runs database
migrations, creates the application tables, and seeds a single default tenant
plus an **initial admin user** so you can log in right away:

- Email: `GDX_ADMIN_EMAIL` (default `admin@example.com`)
- Password: `GDX_ADMIN_PASSWORD` if you set it, otherwise a random password is
  generated and printed to the container logs:

  ```bash
  docker compose -f gdx_dispatch/docker/docker-compose.yml logs app | grep -A4 "initial admin account"
  ```

The admin is created with `must_change_password` set — change it after your
first login.

## Self-hosting (running a published release)

The Quick start above builds the image locally. To instead run a pre-built,
pinned release from GitHub Container Registry, layer the self-host overlay on
top of the base compose file and pin the version in `.env`:

```bash
cp .env.template .env          # fill in [REQUIRED] values
echo 'APP_VERSION=1.0.0' >> .env   # or leave unset to track :latest

docker compose -p gdx --env-file ./.env \
  -f gdx_dispatch/docker/docker-compose.yml \
  -f gdx_dispatch/docker/docker-compose.selfhost.yml up -d
```

This pulls `ghcr.io/freeperro/gdx_dispatch:<APP_VERSION>` instead of building.
The image tag is the single source of truth for the running version (reported
by `/health` and the admin **update-check**).

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
pip install -r gdx_dispatch/requirements.txt
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

Additional guides live under [`docs/`](docs/) and
[`gdx_dispatch/docs/`](gdx_dispatch/docs/) — see `CONTRIBUTING.md`,
`DEVELOPER_GUIDE.md`, and `ADMIN_GUIDE.md` to get started.

## License

Released under the [MIT License](LICENSE).
