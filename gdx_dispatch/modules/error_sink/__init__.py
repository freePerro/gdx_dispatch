"""Self-hosted server-side error sink (UX audit F-18 / 2026-04-29).

Replaces Sentry. Doug 2026-04-29: "we could build our own inhouse ai
to watch it and fix it since we already know the schema and the plan?
Not a today project. lets do d a self-hosted error sink."

This module owns:
  - record_server_error()  — write path, called from the global
    FastAPI exception handler. Best-effort; never raises.
  - GET/PATCH /api/admin/errors  — admin read + resolve workflow.
  - server_errors table on the control plane (migration 041).

The AI watcher hook is intentionally *not* wired today. The schema is
shaped so it can be added later: group_fingerprint dedups error classes,
resolution_note captures human triage, git_sha enables release-bisect."""
from gdx_dispatch.modules.error_sink.service import record_server_error  # noqa: F401
