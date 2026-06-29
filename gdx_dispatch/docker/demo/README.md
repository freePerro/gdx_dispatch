# Public demo — gdxdispatch.com

A fully isolated, public, shared-login demo of GDX Dispatch. Separate compose
project (`gdx-demo`), separate database/volumes/network — it shares **nothing**
with production (`gdx`). All outbound integrations are neutered. State resets
nightly from a golden snapshot.

| | Production | Demo |
|---|---|---|
| Project | `gdx` | `gdx-demo` |
| App port (loopback) | `127.0.0.1:8002` | `127.0.0.1:8003` |
| DB | `gdx-db-1` (external vol) | `gdx-demo-db-1` (managed vol) |
| Domain | gdx.teamgaragedoor.com | gdxdispatch.com |
| Integrations | live | **all disabled** |
| Data | real | fake, nightly reset |

## Files
- `docker-compose.demo.yml` — the isolated stack (db, redis, app only).
- `.env.demo.example` — env template; copy to `.env.demo` (gitignored) and fill.
- `gen-keys.sh` — generates fresh demo-only secrets.
- `seed_demo.py` — one-time believable-dataset seed + clears the forced password change.
- `reset-demo.sh` — nightly golden-snapshot restore (cron).
- `nginx-gdxdispatch.conf` — the vhost to add to the live nginx config (includes the `location = /` landing block).
- `landing/index.html` — the marketing/auto-login page served at the root.

## Landing page
The bare `/login` screen is a confusing front door, so nginx serves a static branded page at
**`location = /` only** (the SPA still owns `/login`, `/dashboard`, `/api/*`, `/assets/*`). It explains
the demo, shows the credentials, and a one-click **"Enter the demo"** button POSTs `/auth/login` and seeds
the SPA's sessionStorage (`gdx_access_token` + `gdx_user` + `gdx_tenant_slug`), then redirects to
`/dashboard` (falls back to `/login` on any error). The file is served from
`/var/www/gdx/landing/gdxdemo/index.html` — a **directory** bind-mount into gdx-nginx, so edits go live
with **no nginx restart** (only `nginx.conf` edits need the restart). ⚠️ The demo password is hardcoded in
this file twice (visible `<code>` + JS `DEMO_PASSWORD`) — keep it in sync if you rotate the login.

## Integration neutering (why it's safe for strangers)
Every outbound side-effect is gated on a non-empty env var; we leave them empty,
so each is a silent no-op (audited in code 2026-06-29):
- **Email** (SMTP / Outlook Graph): `PLATFORM_SMTP_PASS`, `POWER_APPS_*`, `GDX_MICROSOFT_SECRET_KEY` empty → `no_email_provider_connected`.
- **SMS/voice** (Twilio/Phone.com): `TWILIO_*` empty → `{"sent": false, "reason": "not configured"}`.
- **Stripe**: `STRIPE_SECRET_KEY` empty → API auth error, no charge.
- **QuickBooks**: `QB_CLIENT_*` empty + no Celery worker → never fires.
- **SSO / Maps / tenant-DNS**: all client IDs/secrets empty → flows fail closed.

No Celery workers run in the demo, so even queued side-effects don't execute.

---

## First-time deploy (on the VPS, as root)

```bash
cd /var/www/gdx_dispatch                       # the live gdx_dispatch checkout
git pull                                        # bring in gdx_dispatch/docker/demo/
cd gdx_dispatch/docker/demo

# 1. Secrets — fresh, demo-only.
cp .env.demo.example .env.demo
bash gen-keys.sh >> .env.demo                   # appends SECRET_KEY/JWT/FERNET/DB_PASSWORD/etc.
#   then edit .env.demo: replace the REPLACE_* placeholders the generator
#   didn't fill (GDX_TENANT_ID is filled by gen-keys; set GDX_ADMIN_PASSWORD to
#   a memorable shared password, or use the generated one).

# 2. Bring up the isolated stack (pulls the pinned GHCR image, runs migrations
#    + bootstrap → creates the demo admin from GDX_ADMIN_EMAIL/PASSWORD).
docker compose -p gdx-demo --env-file ./.env.demo -f ./docker-compose.demo.yml up -d
#    wait for health:
until curl -sf http://127.0.0.1:8003/health >/dev/null; do sleep 3; done

# 3. Seed the believable dataset + clear the forced password change.
#    The image predates these files, so copy the script in, then run it with
#    PYTHONPATH=/app (else ModuleNotFoundError: gdx_dispatch).
docker cp ./seed_demo.py gdx-demo-app-1:/tmp/seed_demo.py
docker exec -e PYTHONPATH=/app -w /app -i gdx-demo-app-1 python /tmp/seed_demo.py

# 4. Capture the golden snapshot (the nightly reset restores this).
docker exec gdx-demo-db-1 pg_dump -U gdx -d gdx | gzip > ./golden.sql.gz

# 5. Landing page: drop it where gdx-nginx can serve it (dir-mounted, no restart).
mkdir -p /var/www/gdx/landing/gdxdemo
cp ./landing/index.html /var/www/gdx/landing/gdxdemo/index.html

# 6. nginx: carve gdxdispatch.com out of the vanity-redirect block and add the
#    demo vhost. Edit the LIVE config /var/www/gdx/infra/nginx.conf:
#    - remove `gdxdispatch.com www.gdxdispatch.com` from the "Vanity domains" server_name
#    - append the contents of nginx-gdxdispatch.conf (incl. the `location = /` landing block)
#    Give the proxy a leg into the demo network:
docker network connect gdx-demo_default gdx-nginx
#    ⚠️ nginx.conf is a SINGLE-FILE bind mount — editing changes the inode and
#    `nginx -s reload` reads STALE content. Validate in a throwaway, then RESTART:
docker run --rm -v /var/www/gdx/infra/nginx.conf:/etc/nginx/conf.d/default.conf:ro \
  -v /etc/letsencrypt:/etc/letsencrypt:ro nginx:alpine nginx -t
docker restart gdx-nginx

# 7. DNS: already done — gdxdispatch.com is on Cloudflare (proxied) and already
#    reaches the origin. (If re-pointing: A record to the VPS, orange cloud.
#    Cloudflare is in Full mode, so the *.teamgaragedoor.com origin cert is accepted.)

# 8. Install the nightly reset cron.
( crontab -l 2>/dev/null; echo "0 8 * * * /var/www/gdx_dispatch/gdx_dispatch/docker/demo/reset-demo.sh >> /var/log/gdx-demo-reset.log 2>&1" ) | crontab -
```

Visit https://gdxdispatch.com → the landing page → "Enter the demo" (one-click), or log in with
`GDX_ADMIN_EMAIL` / `GDX_ADMIN_PASSWORD`.

## Updating the demo to a new release
```bash
cd /var/www/gdx_dispatch/gdx_dispatch/docker/demo
sed -i 's/^APP_VERSION=.*/APP_VERSION=<new>/' .env.demo
docker compose -p gdx-demo --env-file ./.env.demo -f ./docker-compose.demo.yml up -d
# re-seed + re-snapshot only if the schema changed enough to want fresh golden data.
```

## Teardown
```bash
docker compose -p gdx-demo --env-file ./.env.demo -f ./docker-compose.demo.yml down -v
docker network disconnect gdx-demo_default gdx-nginx 2>/dev/null || true
# then revert the nginx vhost + DNS.
```
`down -v` is safe here — demo volumes only. It can never touch prod's external volumes.
