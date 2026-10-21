# One compose layout, one app container, one-line install

**Status:** PARTIALLY BUILT (2026-10-19), audited the same day (`critique_eb81cf5ec7a8.md`: five findings, all folded in below). Branch `chore/one-layout`, local <!-- removed or still to be built; link-ok -->
only. **Built:** Build A, the one layout (base + `docker-compose.prod.yml` /
`.dev.yml` / `.reload.yml`; customer, selfhost and lab files deleted; the <!-- removed or still to be built; link-ok -->
`GDX_REQUIRE_EXISTING_DATA` lock; `update.sh` reads `COMPOSE_FILE`; guard
`tests/test_compose_layout_parity.py`). Checked with `docker compose config`
and the compose guard tests only. Build B, the s6 all-in-one app container
(`gdx_dispatch/docker/s6-overlay/`, guard
`gdx_dispatch/tests/test_s6_service_tree.py`), booted locally on an empty
database: see "As built" under B. Build C, `gdx_dispatch/docker/install.sh` and
its release asset, tested with fakes only: see "As built" under C.
Build D: HTTPS on the IP (tested with fakes and the compose script only) and
the screens after the forced password change (unit tests plus a local browser
walk): see "As built" under D. The vps2 rehearsals 1–4 ran on 2026-10-19,
including the prod copy, its switch and its rollback: see "As run" under
Rehearsal. They found the lost admin password on the HTTP fallback (fixed) and
three things the prod switch must know; the update rehearsal's `--no-pull`
pull is under C.
Production is not touched by this work: the prod switch is a separate,
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

- Prod runs `docker-compose.yml` + `docker-compose.selfhost.yml` + an <!-- removed or still to be built; link-ok -->
  **untracked** `docker-compose.serverports.yml` that lives only on the prod <!-- removed or still to be built; link-ok -->
  box.
- Customers run `docker-compose.customer.yml`, a standalone 496-line file that <!-- removed or still to be built; link-ok -->
  has drifted from the base. It lacks `GDX_TENANT_ID`, `VAPID_PRIVATE_KEY_B64`,
  `INBOUND_EMAIL_WEBHOOK_SECRET`, `GDX_SESSION_RECORDING*` and the app-only
  vars `METRICS_TOKEN`, `POWER_APPS_*`, `GDX_MICROSOFT_SECRET_KEY`,
  `OUTLOOK_BOOTSTRAP_TENANT_SLUG` and `QB_WEBHOOK_VERIFIER_TOKEN`.
- It also has **no `/app/recordings` volume**. That bug is ledger entry 1280,
  filed 2026-10-18.
- Lab, dev, demo and selfhost overlays add four more files.
  `docker-compose.lab.yml` is SaaS-era and unused (Doug, 2026-10-18). <!-- removed or still to be built; link-ok -->

Every fix lands in the file someone happened to open. Prod and customers
diverge without anything noticing.

## What already exists (do not rebuild)

Verified against code on 2026-10-19 (`origin/main` c0f5aa01):

- `docker-compose.customer.yml` is the hardened layout: <!-- removed or still to be built; link-ok -->
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
- `release.yml` attaches `docker-compose.customer.yml` to every release. The <!-- removed or still to be built; link-ok -->
  stable URL `releases/latest/download/docker-compose.customer.yml` is what <!-- removed or still to be built; link-ok -->
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
  - Old `docker-compose.customer.yml`, `docker-compose.selfhost.yml`, `docker-compose.lab.yml` and `.env.lab.example` are deleted. <!-- removed or still to be built; link-ok -->
  - `release.yml` still publishes the asset as `docker-compose.customer.yml`, copied from the new file, so the stable URL every installed customer and the README use keeps working. <!-- removed or still to be built; link-ok -->
  - The **volume keys stay the customer file's keys** (`gdx_db_data`, `gdx_uploads`, …). An existing customer install (`-p gdx`) keeps its data across the switch.
- **`gdx_dispatch/docker/docker-compose.prod.yml` (tracked)** replaces the untracked prod `serverports.yml`. It contains: <!-- removed or still to be built; link-ok -->
  - app on `127.0.0.1:8002`
  - db and redis with no host ports
  - Caddy off via a never-enabled profile, because prod keeps its nginx + certbot. (Written as "host nginx"; it is the `gdx-nginx` container, which reaches app by Docker DNS on `gdx_default`, so app joins that network too. Found by /audit on 2026-10-20, after rehearsals 1–5 had passed without it.)
  - volumes mapped onto prod's existing names (`external: true` + `name:`): `docker_db_data`, `docker_gdx_uploads`, `gdx_gdx_recordings`, `gdx_gdx_plugins`, `gdx_redis_data`
  - prod-only env: `TENANT_BASE_DOMAIN`, and `OPENAI_API_KEY` for the app only (not plugin-host)
  - worker concurrency 4/2
  - **No new volume for prod data. Postgres and redis images are identical** (`postgres:16-alpine`, verified on prod: 16.13 musl, `en_US.utf8`, libc provider, so there is no collation change).
- **Running the base alone must never be able to serve an empty prod** (audit finding 1). The new base's db volume is a plain compose volume (`gdx_gdx_db_data` under `-p gdx`). A run on prod without the override would create an empty database, seed a new admin, return 200, and attach to prod's *real* redis, recordings and plugins. Two independent locks:
  1. Prod's `.env` sets `COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml`, so a hand-typed `docker compose up` in that directory includes the override. Check at build time that Compose reads `COMPOSE_FILE` from the `.env` that `update.sh` uses.
  2. A new `GDX_REQUIRE_EXISTING_DATA=1` in prod's `.env`, passed through the base's `x-app-env`: `bootstrap_app` refuses (non-zero exit) when the tenant table is empty, so the container crash-loops loudly instead of seeding.
  - In addition, `install.sh` refuses to run on a host that has a `docker_db_data` volume ("this host runs the prod layout; use update.sh"). <!-- removed or still to be built; link-ok -->
- `docker-compose.dev.yml` becomes an overlay on the new base (`build:` instead of `image:`, dev ports, `GDX_ENV=dev`). The demo compose stays standalone for now and is listed as an open item, not silently left. <!-- removed or still to be built; link-ok -->
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
- **As built (2026-10-19, local boot of image `gdx-aio-test`, compose project of its own, empty volumes):**
  - The tree lives in `gdx_dispatch/docker/s6-overlay/` (copied to `/etc/s6-overlay/`), not `s6-rc.d/` at the docker root.
  - First boot failed: s6 does not start services in the image's WORKDIR, so `gdx_dispatch` did not import. The init and run scripts now `cd /app`. The failed oneshot stopped the container as designed.
  - Healthy in 35 s on an empty database (all migrations, bootstrap). All four processes run as appuser; `celery inspect ping` answers from `high@` and `low@`.
  - SIGKILL of a worker: s6 restarts it in place. Five in 60 s: the container dies with exit code 1 and the restart policy brings it back healthy.
  - That trip first hung: s6's default `S6_SERVICES_GRACETIME` (3 s) is shorter than a Celery warm shutdown, so the kill stage hit the supervisors mid-stop, s6 respawned them and a worker came back up. The Dockerfile sets 50 s, past the 45 s `timeout-kill`.
  - `docker stop` takes about 6 s, exit 0. Stopping beat by hand makes the healthcheck fail. `GDX_DEV_RELOAD=1` runs uvicorn `--reload`.
  - From the commit audit: update.sh's first `up` now removes orphans, so the split layout's celery-beat
    cannot run beside the new beat (there is no beat lock); and update.sh refuses an image with no `/init`
    (an older `:latest` or a pinned older release under these compose files) before anything is recreated.
    Rolling back past this change needs the old tag checked out, not only an older `APP_VERSION`.
  - Not tested: a stop while a task is running (the 45 s warm-shutdown window), memory under load, arm64.

### C. One-line install (`install.sh`, published as a release asset) <!-- removed or still to be built; link-ok -->

The command is `curl -fsSL https://github.com/freePerro/gdx_dispatch/releases/latest/download/install.sh | sh`. The same script is the update command: re-running it is idempotent. <!-- removed or still to be built; link-ok -->

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

- **As built (2026-10-19), where it differs from the steps above:**
  - `gdx_dispatch/docker/install.sh`, POSIX sh (`curl | sh` runs dash). The README command is `… | sudo sh`: it needs the Docker daemon and writes `/opt/gdx`. <!-- built in this change; link-ok -->
  - No `--no-proxy`: the base compose gives the app no way in except through Caddy, so a no-proxy install would serve nothing.
  - `.env` is not "only if absent": `APP_VERSION` is rewritten on every run (that is the update), `GDX_DOMAIN` and `GDX_ADMIN_EMAIL` only when passed; every other line is kept. Values are restricted to characters that need no quoting, because `entrypoint.sh` sources the file. Mode 600, set on every run: the old README's `echo >` left it 644.
  - The snapshot is `pg_dump` through `docker compose exec db`, not the backup sidecar (which may not be running), run on the files the install ran before: the new `.env` and compose file are staged as `.new` and moved in only after the dump succeeds, so a failed snapshot really leaves nothing changed (the first audit of C caught the old order rewriting both first). The database start for the snapshot passes `--pull never` too: the old files' `secrets-init` sets `pull_policy: always`, and the vps2 update rehearsal (2026-10-19) caught a `--no-pull` run pulling the old `:latest` there, which on a normal update could replace the image the install runs before its snapshot. The one exception is an install with data volumes but no compose file, whose snapshot starts on the new file: its images are pulled only after the snapshot, so that start may pull unless `--no-pull` was given (audit of D). An install from the README before the script has `docker-compose.customer.yml` and maybe no `.env`; the snapshot runs on that file (with `--env-file /dev/null`), and the script then renames it to `docker-compose.yml` (second audit). The pull also runs on the staged files, before they are moved in: a failed pull leaves the old pin and compose file. The final `up` passes `--pull never` when Compose has the flag (checked with `up --help`): `app`, `secrets-init` and `plugin-host` set `pull_policy: always`, so a plain `up` pulls a second time after the switch and, under `--no-pull`, fails on an image that exists only locally (fourth audit). A Compose without the flag refuses `--no-pull` in preflight, before any file is written (fifth audit). <!-- release asset name; link-ok -->
  - Every command runs inside `main()`, called on the last line. Under `curl | sh` stdin is the script, and `compose exec` forwards stdin even with `-T`: on a host where `sh` is bash, the first audit of C measured an update that stopped after the snapshot and exited 0. The exec calls also read `/dev/null`.
  - It refuses two installs it must not take over: a `gdx` stack whose containers run from another directory, and a `.env` with `COMPOSE_FILE` (a checkout that `update.sh` manages).
  - Rehearsal flags: `--version` (an unstamped copy refuses to run without it), `--compose-file`, `--no-pull`.
  - Release: `release.yml` publishes `install.sh` beside the compose file with `GDX_RELEASE` stamped to the tag, and its image gate now also boots the image under `/init` with Postgres and Redis and requires all four processes up (twice, 30 s apart) before the push.
  - Tests: `gdx_dispatch/tests/test_install_sh.py` (20 at Build C; 32 after D) runs the real script under `sh` with fake `docker`, `curl`, `ss`, `ip` and `sleep`, and four of them pipe it into `sh` and `bash` as the one-liner does, against a fake `compose exec` that reads stdin; the `main()` wrapper, the snapshot-on-old-files order, the old-README file, pull-before-switch, the relative `--compose-file`, the symlinked working directory and four earlier guards were mutation-checked. The admin banner was seen in `docker logs` of an s6 boot in the same format the script parses.
  - Run for real on vps2 on 2026-10-19: a fresh install and an update over it (rehearsals 2 and 3), and the switch from the split layout (rehearsal 4); see "As run" under Rehearsal.

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
- **As built, HTTPS with no domain (2026-10-19):**
  - `install.sh` decides `GDX_PUBLIC_IP` once and keeps it in `.env`: the source address of `ip -4 route get 1.1.1.1`, unless it is private, loopback, link-local or CGNAT/Tailscale (100.64/10), in which case it writes an empty value (plain HTTP). It never asks an outside service for the address. `--ip ADDRESS` sets it, `--ip none` empties it, and a domain wins over it.
  - The fallback lives in `install.sh`, not in Caddy. Caddy redirects HTTP to HTTPS by default, so a site whose certificate never arrives is a dead redirect. After the app is healthy, the script probes `https://<ip>/health` for about two minutes (24 tries, 5 s apart); on failure it empties `GDX_PUBLIC_IP`, re-runs `up`, and tells the user to open the ports and rerun with `--ip`.
  - The Caddy branch serves the IP under `tls { issuer acme { profile shortlived } }`, because Let's Encrypt issues IP certificates only under that profile (generally available 2026-01-15, https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability), plus `default_sni <ip>`, because a browser sends no SNI for an IP address (https://caddyserver.com/docs/caddyfile/options). Caddy's IP-certificate fix is caddy#7399, closed 2026-04-25; each branch's output passed `caddy adapt` in caddy:2-alpine v2.11.7.
  - `GDX_PUBLIC_IP` is in the shared app env, so `secrets-init` re-mints on a change: `mint_runtime_env.py` derives `https://<ip>` for the base URLs when no domain is set, and drops a stored IP-shaped URL once the address is cleared (a stored domain URL is kept, as before).
  - Only the IP is redirected to HTTPS. The IP branch also writes an `http://` catch-all, so a LAN, Tailscale or host name keeps plain HTTP. Without it Caddy's own redirect sent every Host on port 80 to an https address with no certificate behind it (Build D audit, 2026-10-19; measured on caddy:2-alpine and again on vps2).
  - The probe runs against this host's own Caddy (`curl --connect-to <ip>:443:<addr>:<port>`), at the address and port that `compose port caddy 443` reports, so a `GDX_HTTPS_PORT` that moves or binds 443 is followed without parsing `.env` (a copy of compose's parsing got three forms wrong in the re-audits). A wildcard bind is dialled on 127.0.0.1; a bound address as is. Behind NAT, which is what `--ip` is for, the public address is unreachable from inside unless the router does hairpin NAT, so a probe of the public address would drop a certificate that was issued. `--resolve` cannot do this: curl skips it for a URL that is an IP address.
  - An existing install updated by this `install.sh` with no `GDX_PUBLIC_IP` line in its `.env` gets the address detected and HTTPS turned on, as a first run does. Only `http://<ip>` changes (it redirects); other names stay on HTTP. `--ip none` keeps it off.
  - IPv4 only.
  - Without `curl` on the host the probe cannot run: the script says so (`curl is missing: https://<ip> was not checked.`) and keeps HTTPS rather than dropping a working certificate to plain HTTP.
  - Tests: `gdx_dispatch/tests/test_caddyfile_branches.py` runs the compose file's own Caddy script per branch; `test_install_sh.py` covers the probe, the fallback, the private-address filter, decide-once and the flag; `test_mint_runtime_env.py` the derived URL and its removal. Each guard was mutation-checked.
  - Run for real on vps2 on 2026-10-19 (rehearsal 2), and again after the audit's catch-all and probe fixes: a fresh `install.sh --ip 2.24.120.242` came up on `https://2.24.120.242` with a trusted Let's Encrypt certificate (issuer YE2, SAN `IP Address:2.24.120.242`), with no fallback. `http://` with the IP as Host got 308 to https, and with Host `dispatch.lan` or `100.101.1.2` got 200. That install was then wiped. The fallback path itself is covered only by the fake-docker test.
- **As built, after the forced password change (2026-10-19):**
  - "Setup needed" is decided by the server: `GET /api/onboarding/state` adds `setup_needed`, true only while onboarding has no `completed_at` **and** the company has no customers. A bare missing `completed_at` is not enough: the audit of D found production with no onboarding row at all (the endpoint creates it on first read) and 434 customers, so that check would have sent its owners into the wizard, with its demo-data button, after a forced change, and stopped the tour for all ten owners and admins.
  - `UserProfileView.vue`: a change made while held shows a toast, then sends an owner or admin whose state says `setup_needed` to `/onboarding`, and everyone else (or anyone whose state read fails) to `/dashboard`. A change made by choice stays on the page.
  - `AppLayout.vue`: the dashboard tour waits, for an owner or admin, while the state says `setup_needed` or cannot be read. Other roles are not held back.
  - The shell's 403s while held: `useApi.js` refuses, without a network call, every `/api/` request a held session would be refused (all but `GET /api/users/me` and `POST /api/users/me/change-password`; `/auth/` is not under `/api/`). It throws the server's own `password_change_required` error, so callers and the toast handler behave as before. The raw-fetch loaders skip too: `auth.loadPermissions`, `theme.loadBranding` and `useTenantModules` (which does not cache the skip). When the hold clears, `AppLayout.vue` remounts the sidebar, topbar, bottom nav and What's New banner (keyed on the hold) and `ThemeProvider.vue` loads branding, so the shell fills in without a reload. The client list of allowed calls mirrors the server's `@allowed_while_password_change_required` (two endpoints); a third one added there would be refused by the client until it is added here too.
  - The Maps card's referrer hint shows this install's own origin.
  - Tests: `src/views/__tests__/UserProfileForcedChange.spec.js`, `src/components/__tests__/AppLayoutTourGate.spec.js`, `src/components/__tests__/ShellPasswordHold.spec.js` and new cases in `src/router/__tests__/passwordChangeRequired.spec.js` (all under `gdx_dispatch/frontend/`), and `gdx_dispatch/tests/test_onboarding.py` for `setup_needed`. Each of the six hold guards and the three `setup_needed` checks (server, profile, tour) was mutation-checked.

## Rehearsal (gdx-vps2 only; prod is read-only)

1. Fresh-stranger install of the **current release**: done 2026-10-19 (above).
2. Wipe; install the **branch build** (image built locally and loaded on vps2, no registry push), through `install.sh`. Repeat the walk. <!-- removed or still to be built; link-ok -->
3. Update path: install the current release, then run branch `install.sh` over it. Check that orphans are removed, data stays, and keys survive (old JWT still valid). <!-- removed or still to be built; link-ok -->
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

### As run (2026-10-19, gdx-vps2)

The branch image was loaded on vps2 as `1.152.0-rehearsal` and never pushed.

- **2, fresh install of the branch:** `install.sh` on HTTP, then a browser walk. These passed: the forced password change and its hold, the wizard, the dashboard, customers and settings, dark mode, 390 px with no sideways scroll, and logout and login again. The "Password changed" toast was not seen; the next login with the new password proves the change. `install.sh --ip 2.24.120.242` was then rerun over that install. It switched the site to HTTPS with a trusted Let's Encrypt IP certificate (issuer YE1, SAN `IP Address:2.24.120.242`), and `/health` returned 200 with certificate verification on. **Bug found and fixed:** when the certificate could not be had, the HTTP fallback recreated `app`. That emptied its logs, so the first-boot admin password was never printed. `install.sh` now reads it straight after the first healthy boot, and the fake-docker test was red before the fix.
- **3, update over the release:** the rerun pulled nothing ("Pulling" ×0) and took snapshot `gdx-pre-update-…sql.gz` first. A token issued before the update still returned 200 after `app` and `plugin-host` were recreated.
- **4, prod copy:** these numbered steps are the rehearsal, not prod.
  1. ufw default-deny, plus `DOCKER-USER` drops for container egress and for new inbound on `eth0`. Container egress was proven blocked before any data landed.
  2. Prod was only read: a `pg_basebackup` of the database, and a tar of uploads, recordings, plugins and redis, streamed through the maintainer's PC without touching its disk, plus the `.env`. The live redis AOF changed during its tar, so tar warned; redis loads a truncated AOF tail by default.
  3. **Old layout first** (v1.151.0 files plus prod's `serverports.yml`), then checked: <!-- untracked prod file; link-ok -->
     - the counts equal prod's at copy time: 434 customers, 321 jobs, 14 users, 405 invoices
     - all 52 encrypted customer addresses decrypt (10 are encrypted empty strings)
     - login works after a password reset on the copy
     - three job photos come back as JPEGs through the API
     - a session recording reads as `appuser`
  4. **Switch:** the new files, the two `.env` lines and plain `update.sh`. A shim turned only `compose pull` into a no-op and added `--pull never` to `up`, because the rehearsal tag is not in a registry. Results:
     - the snapshot ran first
     - the celery containers were removed as orphans
     - every check in step 3 passed again
     - the app published only on `127.0.0.1:8002`
     - all four s6 processes ran (web with 2 workers, worker-high, worker-low, beat)
     - beat sent 20 tasks in 3 minutes; its only errors were outbound calls (Phone.com, the plugin catalogue) refused by the egress block
  5. **Rollback** as written under "Prod switch" step 8: the old files, the old pin and the two lines deleted, then the old command. Celery came back on 1.151.0 and every check passed a third time.
  6. **Wiped** 2026-10-19T09:49:59Z: every container and volume, `/root/prodcopy` (the copied `.env` and both snapshots), the iptables rules, and ufw back to inactive. `docker volume ls` showed none left.
- **4 again, 2026-10-20, on the merged tree (f0def3c2, main through v1.152.0):** the same egress block. This time prod was copied straight to vps2 (prod → vps2 over SSH, nothing written on prod or the PC), including prod's whole git checkout at the v1.152.0 commit, so the switch moved that checkout to the branch commit as a release pull would.
  1. Old layout (v1.152.0, prod's three files): 435 customers, 323 jobs, 14 users, 406 invoices, equal to prod at copy time. All 53 encrypted addresses decrypt (10 to empty strings); login after a reset on the copy; three job photos as JPEGs through the API; a recording reads as `appuser`.
  2. Switch: branch app image `1.152.0-rehearsal` built from f0def3c2 with the release build args. The plugin-host image was prod's own 1.152.0, retagged, because the branch changes only a comment in `Dockerfile.plugin-host` and nothing it runs. Then the two `.env` lines and plain `update.sh` with the pull shim. Snapshot first; celery containers removed as orphans; app on `127.0.0.1:8002` only; db, uploads, recordings, redis and plugins mounted from prod's volume names, with no new empty data volume; every check in 1 passed again; all four s6 longruns up; beat sent 17 tasks in 3 minutes with no error lines.
  3. Rollback per step 8: the env file came back byte-identical to the copy's original, app healthy in about 90 s, celery back in its own containers, every check passed a third time.
  4. New: the `backup` sidecar got an anonymous volume, because `postgres:16-alpine` declares one and the sidecar mounted nothing there. Fixed with a tmpfs at that path, guarded in `test_compose_layout_parity.py`.
  5. Left on vps2, stopped, with the copied data and `.env`: not yet wiped.
- **4 a third time, 2026-10-20, through the nginx front.** /audit found what the earlier runs could not see: they checked the app on `127.0.0.1:8002`, but prod's TLS front is the `gdx-nginx` container (configured in `/var/www/gdx/infra`, outside this repo), and it proxies to `http://gdx-app-1:8000` by Docker DNS on `gdx_default`, the old layout's default network. The base puts app on `backend`, `edge` and `automation` only. On the same vps2 copy, with a copy of that container (prod's `nginx.conf`, nginx 1.29.5, self-signed certificates at the same paths, published on 127.0.0.1 only, attached to `gdx_default`), each step was checked through nginx with `/health`, `/` and the wildcard host:
  1. Old layout: 200.
  2. Switch on f0def3c2 as committed: `update.sh` exit 0 and app healthy, and **502 on every domain**. Prod would have done the same.
  3. `docker-compose.prod.yml` puts app on `gdx_default` as an external network named `proxy`, then `update.sh` again: 200; app on all four networks, the four s6 longruns up, counts 435/323/14/406 unchanged. Guarded by `test_prod_app_stays_reachable_by_the_nginx_container`, which fails on f0def3c2's overlay.
  4. Rollback per step 8: env byte-identical, app healthy in 89 s, 200; `gdx_default` survives the switch and the rollback because nginx is attached to it.
  5. The `backup` container made in 2 kept its anonymous volume when 3 recreated it with the tmpfs, because compose carries anonymous volumes across a recreate. Prod has no backup container yet, so its first one starts clean; an install that already ran the sidecar keeps an empty stray volume until `docker compose up -V` or a manual `docker volume rm`.
  6. Left on vps2, stopped, with the copied data, `.env` and the nginx copy: not yet wiped.
  7. Found by the follow-up /audit, after this run: a bare `docker compose` on prod, which `COMPOSE_FILE` is meant to make equivalent to `update.sh`, names the project after the first file's directory (`docker`), not `gdx`. It would start `docker-db-1` on prod's external database volume beside `gdx-db-1`, and a `docker-app-1` nginx does not ask for. `docker-compose.prod.yml` now sets `name: gdx` (`docker compose config` on a copy with only `COMPOSE_FILE` in `.env`: `gdx`; base alone: `docker`), and the nginx test pins it. The vps2 run above went through `update.sh`, whose `-p gdx` overrides `name:`, so it is unaffected.
- **For the prod switch**, from 4:
  1. A bare `docker exec gdx-app-1 python …` no longer sees the database. `DATABASE_URL` and the keys live only in the minted runtime env, which `entrypoint.sh` loads for the four processes, so such a command falls back to an empty SQLite file without an error. Runbooks and agents that exec into the app need `sh -c 'set -a; . "$GDX_RUNTIME_ENV_FILE"; set +a; exec python …'`. Recorded on the found-not-filed list.
  2. The rollback leaves the new `backup` sidecar running and `secrets-init` exited, because the old files do not name them (step 8 now removes them).
  3. `update.sh` pulls once with `pull`, and `up` pulls again, because `app` sets `pull_policy: always`. That is harmless against a registry, but it is two pulls.

## Prod switch (NOT part of this work; maintainer-run after merge)

1. Tag the overlapping ledger entries `deferred`.
2. Release.
3. Leave the old `serverports.yml` where it is. It is untracked, so no pull touches it, and the rollback runs with it. Do not copy `docker-compose.prod.yml` over it: that file is tracked and `COMPOSE_FILE` names it in place. (Copying it over was this step until the 2026-10-19 audit, finding 5: the rollback would then publish db and redis on 127.0.0.1:5432/6379, which the old file removed on purpose.) <!-- removed or still to be built; link-ok -->
4. Add both locks to prod's repo-root `.env` before the update. Prod's `.env` had neither on 2026-10-19 (audit finding 1), and nothing else adds them:
   - `COMPOSE_FILE=gdx_dispatch/docker/docker-compose.yml:gdx_dispatch/docker/docker-compose.prod.yml`
   - `GDX_REQUIRE_EXISTING_DATA=1`
5. Add `PLATFORM_SMTP_HOST`/`USER` to prod `.env`.
6. Change the deploy skills (gdxproductionupdate, gdxship) to run plain `update.sh`, with no `EXTRA_COMPOSE=…serverports.yml` and no `HEALTH_PORT`. The old command layers the old overlay on the new base, which declares none of prod's volumes: the database would come up on a new, empty `gdx_gdx_db_data`, bootstrap would seed it and `/health` would go green (audit finding 2). `update.sh` now refuses that run, because the volume it resolves does not exist, but the skills should not depend on the refusal. <!-- removed or still to be built; link-ok -->
7. Run `update.sh` (snapshot first). Then check through the front, not the loopback port: `/health` on the public domain answers 200, and `docker network inspect gdx_default` lists `gdx-app-1` beside `gdx-nginx`. `update.sh` checks only `127.0.0.1:8002`, which stays green when nginx cannot reach the app (2026-10-20 rehearsal, "As run").
8. **Rollback:** prod's `/var/www/gdx_dispatch` is a git checkout, and `docker-compose.yml` and `selfhost.yml` are tracked there. So the switch's own pull rewrites them, and a `--legacy` flag in the new tree would point at files that are gone (audit finding 2 of the earlier audit). Instead: <!-- removed or still to be built; link-ok -->
   1. `git checkout <previous tag>` in that checkout, which restores the old files and the old `update.sh`.
   2. Pin the previous `APP_VERSION`, and delete the two lines from step 4 so a bare `docker compose` does not name the new files.
   3. Run the old command, with `serverports.yml` still in place (step 3 kept it). <!-- removed or still to be built; link-ok -->

   4. `docker rm -f gdx-backup-1 gdx-secrets-init-1`: the old files do not name them, so the old command leaves the backup sidecar running (rehearsal, 2026-10-19).

   The volumes the new layout adds (secrets, backups) are left alone. Rehearsed on vps2 on 2026-10-19 (see "As run").

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
     - So with no domain, Caddy serves **real HTTPS on the IP**. `install.sh` detects the public IPv4 and passes it as `GDX_PUBLIC_IP`. <!-- removed or still to be built; link-ok -->
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
