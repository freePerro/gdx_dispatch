# One compose layout, one app container, one-line install

**Status:** PLAN (2026-10-19), audited the same day (`critique_eb81cf5ec7a8.md`: five findings, all folded in below). Nothing built yet. Branch `chore/one-layout`, local
only. Production is not touched by this work: the prod switch is a separate,
maintainer-run step after review (see "Prod switch" below).

**Parent plan:** `hostinger-guided-onboarding-plan.md` (PARTIALLY BUILT). That
plan shipped the customer compose, `secrets-init` and the zero-env first boot.
This plan finishes its install half: it makes the customer compose the *only*
compose, puts the app's four processes in one container, and adds the install
script. It does not touch that plan's funnel, landing page or partner items.

## Why

Doug, 2026-10-18: "could the gdx app work in one docker instead of however
many it is?", widened to "the whole works, not just the layout".

The pain measured on 2026-10-18 is **layout drift**, not container count:

- Prod runs `docker-compose.yml` + `docker-compose.selfhost.yml` + an
  **untracked** `docker-compose.serverports.yml` that lives only on the prod
  box.
- Customers run `docker-compose.customer.yml`, a standalone 496-line file that
  has drifted from the base. It lacks `GDX_TENANT_ID`, `VAPID_PRIVATE_KEY_B64`,
  `INBOUND_EMAIL_WEBHOOK_SECRET`, `GDX_SESSION_RECORDING*` and the app-only
  vars `METRICS_TOKEN`, `POWER_APPS_*`, `GDX_MICROSOFT_SECRET_KEY`,
  `OUTLOOK_BOOTSTRAP_TENANT_SLUG` and `QB_WEBHOOK_VERIFIER_TOKEN`.
- It also has **no `/app/recordings` volume**. That bug is ledger entry 1280,
  filed 2026-10-18.
- Lab, dev, demo and selfhost overlays add four more files.
  `docker-compose.lab.yml` is SaaS-era and unused (Doug, 2026-10-18).

Every fix lands in the file someone happened to open. Prod and customers
diverge without anything noticing.

## What already exists (do not rebuild)

Verified against code on 2026-10-19 (`origin/main` c0f5aa01):

- `docker-compose.customer.yml` is the hardened layout:
  - `secrets-init`, which mints keys into the `gdx_secrets` volume
  - redis with `--requirepass`
  - `POSTGRES_PASSWORD_FILE`
  - three networks (backend, edge, automation)
  - Caddy auto-TLS on `GDX_DOMAIN`, or plain :80 without it
  - a backup sidecar
  - n8n behind a compose profile
  - healthcheck ordering, with only `app` migrating
- `tools/mint_runtime_env.py`: precedence is env, then stored, then minted.
  `DB_PASSWORD` is stored-wins. Operator values must be shell-safe.
  `entrypoint.sh` sources `GDX_RUNTIME_ENV_FILE` over the container env.
- `release.yml` attaches `docker-compose.customer.yml` to every release. The
  stable URL `releases/latest/download/docker-compose.customer.yml` is what
  the README tells strangers to curl.
- **A fresh-stranger install works today.** Re-run 2026-10-19 on a clean VPS
  (Docker 29.7.2, Compose v5.5.0, 15 GB) by following the README exactly:
  - 9/9 containers up in 1:39
  - the admin banner printed
  - `http://<ip>/` returned 200
  - the first-run walk is recorded under "Install friction"
- `update.sh` covers snapshot, pull, app first and health wait, but it only
  knows the prod layout.

## Rival and related plans

- `hostinger-guided-onboarding-plan.md` is the parent (cited above, and that
  plan now cites this one).
- `archive/n8n-automation-plan.md` set the automation network and profile
  shape that this plan keeps unchanged.
- No other plan in `docs/design/` (recursive grep for customer compose,
  serverports, all-in-one, s6 and supervisord) names these files.

## Research (cited)

Gathered 2026-10-18/19:

- **s6-overlay v3.2.3.2** (released 2026-07-16):
  [README @v3.2.3.2](https://github.com/just-containers/s6-overlay/blob/v3.2.3.2/README.md).
  - Install two tarballs, then use `ENTRYPOINT ["/init"]`. Map arch amd64→x86_64 and arm64→aarch64.
  - Service folders live under `/etc/s6-overlay/s6-rc.d/`.
  - A `oneshot` service gates the `longrun` services that list it in their `dependencies.d`.
  - Run scripts need `#!/command/with-contenv` to see the container env.
  - `S6_BEHAVIOUR_IF_STAGE2_FAILS=2` stops the container when a oneshot fails. It does not cover a longrun that crashes later.
  - A `finish` script plus [`s6-permafailon`](https://skarnet.org/software/s6/s6-permafailon.html) and `/run/s6/basedir/bin/halt` turn a crash loop into a container exit.
  - The README advises against `USER` for more than a few services. Start as root and drop with `s6-setuidgid`.
  - [s6-rc-compile](https://skarnet.org/software/s6-rc/s6-rc-compile.html) has no stop timeout by default. Use the `timeout-kill` file from [servicedir](https://skarnet.org/software/s6/servicedir.html).
- **Prior art: paperless-ngx v3.3.2** (2026-10-16) moved from supervisord to s6 in
  [PR #8886](https://github.com/paperless-ngx/paperless-ngx/pull/8886) (merged 2025-02-07).
  - Its web server, Celery worker and beat run in one container, with `init-*` oneshots for migrations.
  - It sets `S6_BEHAVIOUR_IF_STAGE2_FAILS=2`.
  - Its healthcheck covers only the web server. We will do better (see below).
  - [PR #14277](https://github.com/paperless-ngx/paperless-ngx/pull/14277): a `USER` line in the image makes s6 exit 0 silently. This is the reason for the "root then drop" design.
- **Counter-evidence considered:**
  - netbox-docker 5.1.1 keeps a separate worker container ([compose](https://github.com/netbox-community/netbox-docker/blob/release/docker-compose.yml)).
  - Synapse calls its supervisord all-in-one image "not intended for production".
  - The real cost is shared memory limits: [Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/) and [cgroup-v2](https://docs.kernel.org/admin-guide/cgroup-v2.html) (`memory.oom.group`=0). The OOM killer picks one process, and s6 restarts it without Docker knowing.
  - Accepted for a single-tenant shop on an 8–16 GB box. The healthcheck below makes a dead worker visible.
- **supervisord 4.3.0** (2025-08-23) was rejected. [Its own docs](http://supervisord.org/) say it is not meant to be PID 1. It has no dependency ordering, and it has no stop-on-FATAL without an event listener.

## Design

### A. One layout

- **`gdx_dispatch/docker/docker-compose.yml` becomes the customer file's content**, plus the env parity it lacked.
  - Old `docker-compose.customer.yml`, `docker-compose.selfhost.yml`, `docker-compose.lab.yml` and `.env.lab.example` are deleted.
  - `release.yml` still publishes the asset as `docker-compose.customer.yml`, copied from the new file, so the stable URL every installed customer and the README use keeps working.
  - The **volume keys stay the customer file's keys** (`gdx_db_data`, `gdx_uploads`, …). An existing customer install (`-p gdx`) keeps its data across the switch.
- **`gdx_dispatch/docker/docker-compose.prod.yml` (tracked)** replaces the untracked prod `serverports.yml`. It contains:
  - app on `127.0.0.1:8002`
  - db and redis with no host ports
  - Caddy off via a never-enabled profile, because prod keeps host nginx + certbot
  - volumes mapped onto prod's existing names (`external: true` + `name:`): `docker_db_data`, `docker_gdx_uploads`, `gdx_gdx_recordings`, `gdx_gdx_plugins`, `gdx_redis_data`
  - prod-only env: `TENANT_BASE_DOMAIN`, and `OPENAI_API_KEY` for the app only (not plugin-host)
  - worker concurrency 4/2
  - **No new volume for prod data. Postgres and redis images are identical** (`postgres:16-alpine`, verified on prod: 16.13 musl, `en_US.utf8`, libc provider, so there is no collation change).
- **Running the base alone must never be able to serve an empty prod** (audit finding 1). The new base's db volume is a plain compose volume (`gdx_gdx_db_data` under `-p gdx`). A run on prod without the override would create an empty database, seed a new admin, return 200, and attach to prod's *real* redis, recordings and plugins. Two independent locks:
  1. Prod's `.env` sets `COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml`, so a hand-typed `docker compose up` in that directory includes the override. Check at build time that Compose reads `COMPOSE_FILE` from the `.env` that `update.sh` uses.
  2. A new `GDX_REQUIRE_EXISTING_DATA=1` in prod's `.env`, passed through the base's `x-app-env`: `bootstrap_app` refuses (non-zero exit) when the tenant table is empty, so the container crash-loops loudly instead of seeding.
  - In addition, `install.sh` refuses to run on a host that has a `docker_db_data` volume ("this host runs the prod layout; use update.sh").
- `docker-compose.dev.yml` becomes an overlay on the new base (`build:` instead of `image:`, dev ports, `GDX_ENV=dev`). The demo compose stays standalone for now and is listed as an open item, not silently left.
- **Recordings:** add `gdx_recordings:/app/recordings` to the app (closes ledger 1280).
- **Env parity:** port every var the old base passed and the customer file lacked into `x-app-env` (list in "Why"). `PLATFORM_SMTP_HOST`/`USER` keep the customer file's blank defaults; prod sets them in its `.env`, because today prod silently relies on the base's `smtp.titan.email` default.
- **Redis password on prod:** prod redis has no password today. The new layout gives it one from `secrets-init`. The AOF data does not store the password, so the existing volume is reused unchanged.
- **Guards** (tests that parse the compose files, rewritten to the new file set):
  - `test_compose_env_passthrough.py`
  - `test_celery_stripe_env.py`
  - `test_compose_redis_durability.py`
  - `test_compose_external_volume_docs.py`
  - `test_env_template_drift.py`
  - `test_saas_surfaces_retired.py`
  - **New `test_compose_layout_parity.py`:**
    - the base mounts uploads, recordings, plugins and secrets on the app
    - `docker-compose.prod.yml` maps exactly the five prod **data** volumes (db, uploads, recordings, plugins, redis) as `external: true` + `name:`, and nothing else. The secrets, backups and Caddy volumes are created fresh on prod, because none exist there (audit 2026-10-19, finding 3).
    - no compose file other than the base, prod, dev and demo exists in `gdx_dispatch/docker/`
- `update.sh` and the README/`.env.template` references move to the new pair.

### B. The app's four processes in one container (s6-overlay)

- **The image keeps today's defaults:** `USER appuser`, `ENTRYPOINT entrypoint.sh`, `CMD uvicorn`. That way the demo compose and any split-mode run behave exactly as today. The image *adds* s6-overlay 3.2.3.2, sha256-verified with an arch map.
- **All-in-one mode is opt-in per compose service:** `user: "0:0"`, `entrypoint: ["/init"]`, no command. The service keeps the name **`app`**, so these all keep working unchanged:
  - Caddy's `app:8000`
  - README `logs app`
  - `update.sh`
  - `pull_browser_sessions.sh`'s `gdx-app-1`
  - every `PLUGIN_HOST_URL` caller
- **s6-rc tree** (`gdx_dispatch/docker/s6-rc.d/`):
  - `init-gdx` (oneshot): its `up` line runs a script with a `#!/command/with-contenv sh` shebang, and that script runs `s6-setuidgid appuser env HOME=/home/appuser entrypoint.sh true`. s6 clears the environment for services by default (`S6_KEEP_ENV=0`), so without `with-contenv` the oneshot would see no `DATABASE_URL` and Alembic would fall back to `alembic.ini` (audit finding 4). It reuses the exact runtime-env load + `create_orm_tables` + `alembic upgrade head` + bootstrap path, then exits. A failed migration fails the oneshot, and `S6_BEHAVIOUR_IF_STAGE2_FAILS=2` stops the container, the same outcome as today's `set -e` crash-loop under `restart: unless-stopped`.
  - `svc-web`, `svc-worker-high`, `svc-worker-low`, `svc-beat` (longruns, each depending on `init-gdx`): `#!/command/with-contenv sh`, then `exec s6-setuidgid appuser env HOME=/home/appuser GDX_SKIP_MIGRATIONS=1 GDX_SKIP_BOOTSTRAP=1 entrypoint.sh <today's command>`. `s6-setuidgid` does not reset HOME, which the PDF font cache needs. Workers get distinct node names, `-n high@%h` and `-n low@%h`; otherwise both default to `celery@<hostname>` and inspect, ping and revoke collapse onto one node (audit finding 5). Each one gets the runtime env the same way the split containers do, and `exec` keeps signals going to the real process.
  - Concurrency comes from `GDX_CELERY_HIGH_CONCURRENCY` (default 2) and `GDX_CELERY_LOW_CONCURRENCY` (default 1).
  - Each longrun gets a `finish` script with `s6-permafailon 60 5 die`: five deaths in 60 s halts the container with a non-zero code, so Docker's restart policy and `docker ps` see it.
  - `timeout-kill` is set so a stuck warm shutdown cannot hang the stop. Compose `stop_grace_period: 60s` must exceed it.
- **Healthcheck:** `/health` 200 **and** `s6-svstat -u` true for all four longruns. A dead beat or worker turns the container unhealthy, which is better than paperless.
- **What is given up** (stated, accepted for this deployment size):
  - per-service memory limits, and OOM picking one process
  - `celery-low` and `beat` now share the app's networks (backend, edge, automation)
  - a worker can no longer be scaled on its own (`--scale`)
- **Stays separate:** db and redis (stock images, data volumes untouched); plugin-host (separate Playwright image and its isolation boundary); Caddy; backup; n8n.
- Net result: 9 containers become **6**, or 5 on prod with Caddy off. The app becomes **one container**.
- **Migration from the split layout:** the old `celery-*` containers become orphans. The install/update path always runs `up -d --remove-orphans`.

### C. One-line install (`install.sh`, published as a release asset)

The command is `curl -fsSL https://github.com/freePerro/gdx_dispatch/releases/latest/download/install.sh | sh`. The same script is the update command: re-running it is idempotent.

1. Preflight:
   - Docker ≥ 24 and Compose v2. If Docker is missing, print the official install link and exit. Docker is installed for the stranger only with `--install-docker`, which uses get.docker.com.
   - ports 80/443 free (unless `--no-proxy`)
   - ≥ 4 GB RAM (warn) and ≥ 10 GB disk (warn)
2. Install dir `/opt/gdx` (override `GDX_DIR`). Download the **release-pinned** compose: the script knows its own version, which keeps the asset and image in lock-step.
3. Write `.env` only if absent, from flags or prompts when a TTY is present: `--domain`, `--email`. `APP_VERSION` is pinned. Nothing else is needed; `secrets-init` mints the rest.
4. If a stack already exists: a pg_dump snapshot through the backup sidecar comes first.
5. `docker compose -p gdx pull && up -d --remove-orphans`, then wait for app healthy (≤ 10 min, with progress).
6. Print the URL, and on first install the admin email and the generated password, read from the bootstrap banner. Say it will be changed on first login.

A shellcheck-clean script gets a test that runs it in a container with a fake `docker` shim (argument assertions), plus the real VPS run.

### D. First-run gaps (from the 2026-10-19 walk)

- **HTTPS with no domain:** Caddy gets a Let's Encrypt IP certificate (friction 1). In the base compose, the Caddy no-domain branch becomes `GDX_PUBLIC_IP` → IP cert, falling back to plain :80. `secrets-init` derives `GDX_PUBLIC_BASE_URL=https://<ip>` when no domain is set.
- **After the forced password change** (friction 2–3):
  - Show a success toast.
  - Route an owner whose onboarding is incomplete (`/api/onboarding` status, which already exists) to `/onboarding`. Everyone else goes to `/dashboard`.
  - The dashboard tour does not auto-start until onboarding is complete.
  - The shell skips its data calls while `mustChangePassword` is set, so the ten 403 console errors go away.
- **Out of scope:**
  - An email step in the wizard. Email settings already have a full UI (Settings → Email Configuration).
  - Stripe in the UI (product shape).
  - Re-locking `/onboarding` after completion (friction 5; harmless).
- **Boot without integrations:** measured clean (friction 6). Nothing to build.

## Rehearsal (gdx-vps2 only; prod is read-only)

1. Fresh-stranger install of the **current release**: done 2026-10-19 (above).
2. Wipe; install the **branch build** (image built locally and loaded on vps2, no registry push), through `install.sh`. Repeat the walk.
3. Update path: install the current release, then run branch `install.sh` over it. Check that orphans are removed, data stays, and keys survive (old JWT still valid).
4. **Prod-copy rehearsal:**
   1. **Before any prod data lands:**
      - ufw default-deny incoming (SSH allowed)
      - **egress blocked for containers** with an iptables `DOCKER-USER` drop for traffic leaving the Docker subnets, after images are pulled
      - Reason: restored rows hold real customer emails, phone numbers and DB-stored integration credentials, and beat would act on them.
      - Docker-published ports bypass ufw, so the prod override's loopback-only bind is what keeps them private, not ufw.
   2. Stream the prod data across without writing it on this PC:
      - **a physical copy of the database:** `pg_basebackup -Ft -X fetch` from the prod db container (read-only; it costs one checkpoint), restored into a volume named `docker_db_data` on vps2. A `pg_dump` would not rehearse the switch, which reuses the existing data directory, its role password and the stored-wins `DB_PASSWORD` (audit finding 4).
      - tar copies of the redis, uploads, recordings and plugins volumes under their prod names
      - the prod `.env`
      - Prod is read only, and nothing is stopped there.
   3. Bring up base + `docker-compose.prod.yml` with prod's keys. Verify:
      - customers and jobs count match
      - an encrypted field decrypts
      - a photo and a recording open
      - login works (password reset on the copy, not the real account)
      - all four processes run, and beat ticks without outbound errors
   4. Wipe vps2's volumes and the copied `.env` afterwards. Record the wipe.

## Prod switch (NOT part of this work; maintainer-run after merge)

1. Tag the overlapping ledger entries `deferred`.
2. Release.
3. Copy the tracked prod override over `serverports.yml`.
4. Add `PLATFORM_SMTP_HOST`/`USER` to prod `.env`.
5. Run `update.sh` (snapshot first).
6. **Rollback:** prod's `/var/www/gdx_dispatch` is a git checkout, and `docker-compose.yml` and `selfhost.yml` are tracked there. So the switch's own pull rewrites them, and a `--legacy` flag in the new tree would point at files that are gone (audit finding 2). Instead:
   1. `git checkout <previous tag>` in that checkout, which restores the old files and the old `update.sh`.
   2. Pin the previous `APP_VERSION`.
   3. Run the old command, with `serverports.yml` still in place (it is untracked and never deleted).

   The volumes the new layout adds (secrets, backups) are left alone. Rehearse the rollback on vps2 too.

## Not in scope

- The demo compose merge.
- Hostinger funnel items 4–8.
- Removing the CLOUDFLARE_* and AUTH_COOKIE_SECURE prod env names, which no service reads. Recorded on the found-not-filed list.

## Install friction (2026-10-19 walk, latest release at the time)

The README path was run verbatim on a clean VPS and walked in a real browser
through a loopback tunnel. Every step passed. What a stranger hits:

1. **Auth cookies are hard-coded `Secure`** (`routers/auth/core.py:467,475,561,566,722,728`).
   - The README's first-boot address is `http://<server-ip>`. Over plain HTTP, browsers drop the refresh cookie.
   - A walk over `http://<public-ip>` measured: login works and reloads keep the session, because the access token sits in `sessionStorage` (`stores/auth.js:10`).
   - But `/auth/refresh` and new tabs need the refresh cookie, which a browser never stores from an HTTP origin. So the session should end at access-token expiry. The rehearsal walk confirms that by waiting past expiry.
   - **Fix, measured 2026-10-19:**
     - Let's Encrypt issues IPv4 certificates under the `shortlived` profile (GA January 2026, 160-hour lifetime, http-01 / tls-alpn-01 only; [Help Net Security 2026-01-20](https://www.helpnetsecurity.com/2026/01/20/lets-encrypt-6-day-tls-certificates/)).
     - Caddy supports them since the fix discussed in [caddy#7399](https://github.com/caddyserver/caddy/issues/7399) (closed 2026-04-25). `caddy:2-alpine` is v2.11.7 (released 2026-10-03).
     - On the rehearsal VPS, a Caddyfile with `default_sni <ip>` and `<ip> { tls { issuer acme { profile shortlived } } }` got a trusted certificate on the first try: issuer `Let's Encrypt YE2`, SAN `IP Address:2.24.120.242`, valid 10-19 to 10-25.
     - Plain `curl https://<ip>/` verified it against the system trust store.
     - So with no domain, Caddy serves **real HTTPS on the IP**. `install.sh` detects the public IPv4 and passes it as `GDX_PUBLIC_IP`.
     - If that address is not public (no IPv4 detected, or the issue fails), Caddy falls back to today's plain :80, and the install output says logins will expire every few minutes until a domain is set.
     - `secure=True` stays.
2. The forced password change lands on the full `/profile` page.
   - Ten 403s fire, each logged as a console error.
   - The only hint is one alert line.
   - On success there is no toast and no redirect.
   - Fix: after a successful change, toast, then route an owner with onboarding incomplete to `/onboarding` (item D), else `/dashboard`.
   - Gate the shell's data calls while `must_change_password` is set.
3. The onboarding wizard is not offered. A 5-step dashboard tour pops up instead and points at the dashboard. Fix: the redirect in 2. The tour waits until onboarding is complete.
4. The wizard and Settings have no public URL or domain field. The Maps hint hard-codes `*.example.com`. Stripe is env-only ("Set STRIPE_SECRET_KEY on the server").
   - In scope: the install script's `--domain`, which sets `GDX_DOMAIN`. `secrets-init` already derives `GDX_PUBLIC_BASE_URL`, and the Maps hint should read it.
   - Out of scope (product shape, Doug's call): Stripe in the UI.
5. `/schedule` is "Page not found" (dispatch lives at `/dispatch`). Recorded as found, not filed. It is not an install problem.
6. Boot without integrations is clean. Dashboard, Customers, Jobs, Dispatch and Settings show no API errors and no warning banners with every integration blank. Nothing to build for that part of D.
