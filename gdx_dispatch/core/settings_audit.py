"""Audited upsert for the `tenant_settings` singleton row.

Six module routers — `billing_terms`, `catalog_policy`, `dispatch_settings`,
`numbering`, `maps_provider`, `workflow` — are the same twenty-line function
modulo their column tuple: resolve the tenant, role-check, `INSERT …
ON CONFLICT (tenant_id) DO UPDATE`, `commit()`, re-read. Not one of them wrote
an audit row, so "who changed this setting, and to what" had no answer at all.
That is invariant #1 in `ARCHITECTURAL_INVARIANTS.md`, and issue #558.

The "when" comes from the audit row's own `created_at`, which is what
invariant #1 asks for. It is NOT taken from `tenant_settings.updated_at`:
the ORM model declares that column, but it is absent from the live table
(checked 2026-09-12 against the dev database, where `tenant_settings` does not
exist at all — see FOUND_NOT_FILED). Writing a column this code cannot confirm
is there would risk breaking endpoints that work today, to improve a timestamp
the audit row already carries.

**Atomicity.** `core/audit.py::audit_or_rollback` states the contract: stage
the audit row inside the caller's transaction so the change and its trail land
together. That is why the diff here is computed from a re-read taken BEFORE the
commit — the same session sees its own staged write, so the "after" values are
accurate without giving up atomicity. Verified: make the audit write raise
under the real dependency wiring and the settings column reads back unchanged.

Precisely: **no commit happens between the upsert and the audit row.** This is
NOT "exactly one commit per request" — callers' `_read` has a create-on-read
branch that commits, and `ensure_audit_table` commits on first use per engine,
so a PATCH can issue three. Both of those land BEFORE anything is staged, which
is what makes them harmless. If you add a `read()` AFTER the upsert that can
commit, or make `_read` commit unconditionally, that harmlessness is gone —
this note exists so the next reader does not trust a simpler invariant than
the one that actually holds.

**Callers must depend on `audit_ready_db`, not `get_db`.** `ensure_audit_table`
commits the first time it runs for an engine; if that fires from inside
`log_audit_event_sync` here, it would commit the staged upsert early and undo
the guarantee above. `audit_ready_db` (`core/audit.py`) moves that
initialization in front of the handler, where committing has nothing to
disturb.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import text

from gdx_dispatch.core.audit import log_audit_event_sync, resolve_audit_actor


def _client_host(request: Any) -> str | None:
    client = getattr(request, "client", None)
    return getattr(client, "host", None) if client is not None else None


def audited_settings_upsert(
    db: Any,
    request: Any,
    user: Any,
    *,
    tenant_id: Any,
    values: dict[str, Any],
    action: str,
    read: Callable[[Any, Any], dict[str, Any]],
) -> dict[str, Any]:
    """Upsert `values` onto the tenant's settings row and record who did it.

    `read` is the caller's own `_read(db, tenant_id)`; it is used twice, once
    before the write and once after it is staged, so the audit row carries only
    the columns that actually moved. A no-op save records an empty diff rather
    than replaying every column.

    Returns the settings row as the caller's `_read` renders it.
    """
    cols = list(values)
    before = read(db, tenant_id)

    set_clause = ", ".join(f"{c} = :{c}" for c in cols)
    # S608: the interpolated names are the caller's own column tuple, never
    # request data — the values are bound parameters. Same construction as the
    # routers this replaces.
    sql = (
        f"INSERT INTO tenant_settings (tenant_id, {', '.join(cols)}) "  # noqa: S608
        f"VALUES (:tid, {', '.join(':' + c for c in cols)}) "
        f"ON CONFLICT (tenant_id) DO UPDATE SET {set_clause}"
    )
    db.execute(text(sql), {"tid": str(tenant_id), **values})

    # Same session, same transaction — this sees the staged write.
    after = read(db, tenant_id)

    # Diff over the keys the CALLER'S reader projects, not over `cols`. The two
    # are not always the same name: `modules/workflow/router.py` writes columns
    # `workflow_require_signature_on_complete` but reads them back as
    # `require_signature_on_complete`, so a column-keyed diff would look up
    # names that are not in `after` and record nothing changed.
    changed = {
        k: {"from": before.get(k), "to": after.get(k)}
        for k in after
        if before.get(k) != after.get(k)
    }

    log_audit_event_sync(
        db=db,
        tenant_id=str(tenant_id),
        user_id=resolve_audit_actor(user, request),
        action=action,
        entity_type="tenant_settings",
        entity_id=str(tenant_id),
        details={"changed": changed},
        # `getattr`, not `request.client`: handlers are called directly in
        # several tests with a SimpleNamespace stand-in that has no `client`
        # attribute at all, and an audit helper must never be the thing that
        # breaks a caller. Caught by test_payment_date.py.
        ip_address=(_client_host(request)),
        request=request,
    )
    db.commit()
    return after
