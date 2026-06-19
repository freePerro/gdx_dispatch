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

## Quick start (Docker)

Requires Docker and Docker Compose.

```bash
cp .env.template .env
# Fill in the [REQUIRED] values (database password, SECRET_KEY, …)

docker compose -f gdx_dispatch/docker/docker-compose.yml up -d --build
```

This brings up PostgreSQL, Redis, the API, and the Celery workers. The API is
exposed on <http://localhost:8001> with a health check at `/health`.

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
