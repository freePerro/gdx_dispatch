"""Server-side error sink — write path.

Called from `gdx_dispatch.core.error_handler.global_exception_handler` for any
unhandled exception or 5xx HTTPException. Best-effort: a failure here
never propagates back to the request — the original error response
must always reach the client.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import traceback as tb_mod
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from gdx_dispatch.core.database import SessionLocal, tenant_context

log = logging.getLogger(__name__)


# Cap traceback length so a single pathological exception can't blow up
# the table. ~8KB lets us keep the full stack frame for almost everything.
_TRACEBACK_MAX = 8192
_QUERY_MAX = 1024
_REFERER_MAX = 512
_UA_MAX = 256
_MSG_MAX = 2000

_GIT_SHA = os.environ.get("GIT_SHA") or os.environ.get("APP_GIT_SHA") or None
_TOP_FRAME_RE = re.compile(r'  File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<fn>\S+)')


def _fingerprint(path: str | None, exc_class: str | None, traceback_text: str) -> str:
    """Group identical errors. Hash of route + class + the top non-stdlib
    frame in the traceback. 12 hex chars is enough — collisions across
    different bug shapes are extremely unlikely for a single tenant."""
    top_frame = ""
    for m in _TOP_FRAME_RE.finditer(traceback_text or ""):
        f = m.group("file") or ""
        # Skip stdlib + venv frames; the interesting frame lives in /app/.
        if "/site-packages/" in f or f.startswith("/usr/local/lib/python"):
            continue
        top_frame = f"{f}:{m.group('line')}:{m.group('fn')}"
        break
    seed = f"{path or ''}|{exc_class or ''}|{top_frame}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]


def record_server_error(
    *,
    request: Any | None,
    exc: BaseException,
    status_code: int,
    request_id: str | None = None,
) -> None:
    """Persist one error to the control-plane sink. Swallows everything."""
    try:
        path = getattr(getattr(request, "url", None), "path", None) if request else None
        method = getattr(request, "method", None) if request else None
        query = str(getattr(getattr(request, "url", None), "query", "") or "")[:_QUERY_MAX]
        headers = getattr(request, "headers", {}) or {}
        referer = (headers.get("referer") or "")[:_REFERER_MAX] if headers else ""
        ua = (headers.get("user-agent") or "")[:_UA_MAX] if headers else ""

        tenant = getattr(getattr(request, "state", None), "tenant", None) if request else None
        tenant_id_raw = (tenant or {}).get("id") if isinstance(tenant, dict) else None
        # Validate UUID; fall back to NULL on garbage so the row still lands.
        try:
            tenant_id = str(uuid.UUID(str(tenant_id_raw))) if tenant_id_raw else None
        except Exception:
            tenant_id = None

        principal = getattr(getattr(request, "state", None), "user", None) if request else None
        user_id = None
        user_email = None
        if isinstance(principal, dict):
            user_id = str(principal.get("sub") or principal.get("user_id") or "") or None
            user_email = (principal.get("email") or "") or None

        traceback_text = "".join(tb_mod.format_exception(type(exc), exc, exc.__traceback__))[-_TRACEBACK_MAX:]
        exc_class = type(exc).__name__
        message = str(exc)[:_MSG_MAX]
        fp = _fingerprint(path, exc_class, traceback_text)

        # We open a fresh control session — the request's session may already
        # be in a rollback state by the time the handler runs.
        ctx = tenant_context(tenant_id) if tenant_id else _NullCtx()
        with ctx, SessionLocal() as cdb:
            cdb.execute(
                text(
                    "INSERT INTO server_errors ("
                    "  id, tenant_id, request_id, method, path, status_code, "
                    "  exception_class, exception_message, traceback, "
                    "  user_id, user_email, query_string, referer, user_agent, "
                    "  git_sha, group_fingerprint, occurred_at"
                    ") VALUES ("
                    "  :id, :tid, :rid, :m, :p, :sc, :ec, :em, :tb, "
                    "  :uid, :ue, :qs, :ref, :ua, :gs, :fp, :ts"
                    ")"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tid": tenant_id,
                    "rid": (request_id or "")[:64] or None,
                    "m": method,
                    "p": path,
                    "sc": int(status_code),
                    "ec": exc_class[:200],
                    "em": message,
                    "tb": traceback_text,
                    "uid": user_id,
                    "ue": user_email,
                    "qs": query or None,
                    "ref": referer or None,
                    "ua": ua or None,
                    "gs": _GIT_SHA,
                    "fp": fp,
                    "ts": datetime.now(timezone.utc),
                },
            )
            cdb.commit()
    except Exception:  # noqa: BLE001
        # The sink failing must NEVER block the original error response.
        log.exception("server_error_sink_write_failed")


class _NullCtx:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> None:
        return None
