# Denylist Backend Mode — Operator Runbook

## What this controls

The JWT denylist (see `gdx_dispatch/core/denylist.py`, wired at the router seam in
`gdx_dispatch/routers/auth/core.py::_denylist_redis_client`) has two possible storage
backends:

- **local-only** (in-process set on `request.app.state.denylist`), and
- **Redis-backed** (same in-process set, with a best-effort fan-out so a
  token revoked on worker A is rejected by worker B).

`DENYLIST_BACKEND_MODE` is the explicit operator knob for which of those
two modes a deployment runs. It was added in SS-7 Slice K so an operator
can force local-only mode on a deployment whose `REDIS_URL` is set for
other consumers (session cache, rate-limit counters, password-reset
tokens) — without having to unset `REDIS_URL` globally.

## Mode matrix

`DENYLIST_BACKEND_MODE` is parsed case-insensitively with surrounding
whitespace stripped.

| `DENYLIST_BACKEND_MODE` | `REDIS_URL`     | Resolved backend   | Notes                                                                                                                                                                             |
| ----------------------- | --------------- | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `memory`                | any             | local-only         | Explicit opt-out. `REDIS_URL` is ignored even if set.                                                                                                                              |
| `redis`                 | set + non-empty | Redis-backed       | Explicit Redis mode. Construction is lazy (no connection opened at resolve time).                                                                                                  |
| `redis`                 | unset / blank   | local-only         | Logs warning `denylist_backend_mode_redis_missing_redis_url` and falls back. Fail-open.                                                                                             |
| unset / blank           | set + non-empty | Redis-backed       | Slice J default behavior — preserved byte-for-byte.                                                                                                                               |
| unset / blank           | unset / blank   | local-only         | Slice J default behavior — preserved byte-for-byte.                                                                                                                               |
| any other value         | any             | Slice J default    | Logs warning `denylist_backend_mode_invalid` (echoed value truncated to 64 chars) and degrades to the unset-default behavior. Protects against typos (`rediss`, `REDI$`).           |

## Fail-open guarantee

Any misconfiguration or dependency failure falls back to **local-only**
denylist behavior. Specifically:

- A `DENYLIST_BACKEND_MODE=redis` with unset `REDIS_URL` logs a warning
  and returns `None` (local-only). No startup crash, no 5xx on the
  revoke endpoint.
- A malformed `REDIS_URL` that makes `redis.from_url(...)` raise is
  caught and logged via `log.exception("denylist_redis_client_build_failed")`;
  the helper returns `None` (local-only).
- An unrecognized mode value logs a warning and degrades to unset-default
  behavior (Redis iff `REDIS_URL` is valid, local otherwise).

The revoke path (`POST /auth/admin/revoke-token`) and the reader path
(`get_current_user` denylist check) never crash on Redis failure —
both fall through to in-process state. Cross-worker fan-out is
best-effort, not load-bearing.

`GET /health` extends the same fail-open contract to its visibility
probe. If the `_denylist_redis_client()` call inside the `/health`
handler raises for any reason (import error, downstream helper change,
transient import-graph issue), `gdx_dispatch/app.py` catches the exception,
logs `denylist_backend_probe_failed` (ERROR via `log.exception`,
includes stack), and reports `denylist_backend: "memory"`. `/health`
still returns `200 OK`; no 5xx is surfaced because of a probe hiccup.

## Observability

### Log events to alert on

Operator-facing log events emitted by `_denylist_redis_client()`:

- `denylist_backend_mode_redis_missing_redis_url` — WARNING (via
  `log.warning`). `DENYLIST_BACKEND_MODE=redis` was set but `REDIS_URL`
  is unset or blank. Deployment falls back to an in-memory denylist
  despite asking for Redis. Sustained occurrences indicate a
  misconfigured environment.
- `denylist_backend_mode_invalid` — WARNING (via `log.warning`). A
  typoed or unknown mode value was supplied. The echoed value is
  truncated to 64 chars for safety. The helper degrades to the
  unset-default behavior (an in-memory denylist when `REDIS_URL` is
  also unset). Investigate the env var source (compose file, k8s
  secret, systemd unit) and fix the spelling.
- `denylist_redis_client_build_failed` — ERROR (via `log.exception`,
  includes stack). `redis.from_url(...)` raised during client
  construction — URL scheme, TLS options, etc. Denylist is running
  local-only. Sustained occurrences indicate a broken `REDIS_URL`.

Operator-facing log event emitted by `gdx_dispatch/app.py` at the `/health`
probe layer (distinct from the three helper events above):

- `denylist_backend_probe_failed` — ERROR (via `log.exception`,
  includes stack). The `/health` handler tried to resolve the
  denylist backend by calling `_denylist_redis_client()` and the
  call itself raised. The handler swallows the exception, degrades
  the reported `denylist_backend` value to `"memory"`, and keeps
  `/health` at `200 OK` (fail-open). Sustained occurrences indicate
  an upstream regression in the helper or its import graph.

### Health endpoint visibility

`GET /health` returns the resolved backend in a `denylist_backend` key:

- `"memory"` — the helper returned `None` (any fail-open path or
  explicit `DENYLIST_BACKEND_MODE=memory`).
- `"redis"` — the helper returned a client.

The health response **never** echoes `REDIS_URL`, any connection
string, or any credential. The key is a read-only visibility aid for
dashboards and on-call and does not affect request routing.

### Alerting guidance

Suggested alert rules (adjust thresholds to deployment noise floor):

- **Warning: `denylist_backend_mode_invalid` ≥ 1 occurrence in 5 min** —
  page whoever owns the env var. This is almost always a config typo
  and should be fixed within the business day.
- **Warning: `denylist_backend_mode_redis_missing_redis_url` ≥ 1
  occurrence in 5 min** — page the on-call engineer. The deployment
  is asking for Redis mode but running local-only, which is a silent
  correctness gap for cross-worker revocation.
- **Error: `denylist_redis_client_build_failed` ≥ 3 occurrences in 5
  min** — page the on-call engineer immediately. The Redis URL itself
  is broken and every lazy-create path on every app boot is falling
  back to local-only.
- **Health drift: `denylist_backend` flips from `redis` to `memory`
  unexpectedly** — alert the platform team. A deployment that booted in
  Redis mode and is now reporting memory likely fell through one of the
  fail-open paths above; cross-reference with the three log events.

## Related files

- `gdx_dispatch/routers/auth/core.py::_denylist_redis_client` — mode resolution.
- `gdx_dispatch/routers/auth/core.py::_get_app_denylist` — lazy attach to `app.state`.
- `gdx_dispatch/core/denylist.py` — adapter-agnostic core class.
- `gdx_dispatch/app.py` — `/health` endpoint with `denylist_backend` visibility.
- `gdx_dispatch/tests/test_denylist.py` — mode matrix pinned by tests.
