# The app container's process tree (s6-overlay)

Status: CURRENT (2026-10-19: one app container replaces the separate celery-high, celery-low and celery-beat services)

The Dockerfile copies this directory to `/etc/s6-overlay/`. `docker-compose.yml`
starts the app container with `/init` as root, and s6-overlay v3 then:

1. runs the oneshot `init-gdx` (`scripts/init-gdx`): `entrypoint.sh true` as
   appuser, so the runtime env, ORM tables, `alembic upgrade head` and the
   first-run bootstrap happen exactly once. If it fails, the container stops
   (`S6_BEHAVIOUR_IF_STAGE2_FAILS=2` in the Dockerfile).
2. starts four longruns, each depending on `init-gdx`, through
   `scripts/gdx-run`, as appuser, with migrations and bootstrap skipped:

   | service           | process                                                    |
   |-------------------|------------------------------------------------------------|
   | `svc-web`         | uvicorn, the image's CMD (`GDX_DEV_RELOAD=1`: `--reload`)  |
   | `svc-worker-high` | celery worker `-Q priority:high -c $GDX_CELERY_HIGH_CONCURRENCY` |
   | `svc-worker-low`  | celery worker `-Q priority:low -c $GDX_CELERY_LOW_CONCURRENCY`   |
   | `svc-beat`        | celery beat                                                |

s6 restarts a process that dies. `scripts/svc-finish` stops the whole container
once one has died five times in 60 seconds, so Docker's restart policy and
`docker ps` see it. `scripts/healthcheck` (the compose healthcheck) fails when
`/health` does not answer or any of the four is down.

On `docker stop`, s6 sends each process SIGTERM and SIGKILLs it after its
`timeout-kill`: 20 s for web, 45 s for the Celery processes, so a worker can
finish its running tasks. Compose's `stop_grace_period` (60 s) covers both.

Files that s6 reads:

- `s6-rc.d/<name>/type`: `oneshot` or `longrun`.
- `s6-rc.d/<name>/up` (oneshot) is parsed by execline, not a shell, so it only
  names the script.
- `s6-rc.d/<name>/run` and `finish` (longrun) are executables.
- `dependencies.d/<name>`: empty files that order the start.
- `user-bundles.d/user/contents.d/<name>`: an empty file that enables the
  service.

Scripts that need the container's environment start with
`#!/command/with-contenv sh`, because s6 starts services with an empty one.
