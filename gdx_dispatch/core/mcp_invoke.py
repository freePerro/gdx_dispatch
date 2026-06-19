"""SS-19 slice B — MCP tool invocation orchestrator.

The HTTP router (``gdx_dispatch/routers/mcp_execute.py``) + SSE router
(``gdx_dispatch/routers/mcp_sse.py``) delegate all business logic to this
module so the two transport surfaces share a single code path.

Canonical order of operations (order matters — do NOT rearrange)
----------------------------------------------------------------
1. **Resolve** the tool via the SS-18 registry.
   → miss ⇒ ``tool_not_found`` (404).
2. **Validate** input against ``descriptor.input_schema``.
   → miss ⇒ ``input_invalid`` (400).
3. **Capability check** via :func:`gdx_dispatch.core.mcp_registry.check_capability`.
   → miss ⇒ ``capability_denied`` (403).
4. **Approval gate** — if ``descriptor.approval_required`` and this is
   the first call (no ``approval_ref``), return 202
   ``approval_required``.
5. **Execute** handler. Wrap in try/except — any exception maps to
   ``execution_error`` (500) with the error detail + trace_id.
6. **Audit** — record an MCP execution log row (redacted sensitive
   inputs per descriptor metadata).

The order must be:
    resolve → validate → capability → approval → execute → audit
so that error determinism is stable. Changing the order (e.g. cap
check before input validation) would let a principal probe which
inputs are valid by observing which error fires — a classic info leak.

TODO
----------------
* Swap the ``_dummy_log_execution`` stub with the real SQLAlchemy
  writer once the SS-19 migration lands. The shape is stable.
* Wire ``emit_event`` for ``gdx.mcp.tool_called.v1`` once the SS-23
  event-bus module is integrated.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.mcp_error_schema import (
    ERROR_APPROVAL_REQUIRED,
    ERROR_CAPABILITY_DENIED,
    ERROR_EXECUTION_ERROR,
    ERROR_INPUT_INVALID,
    ERROR_TOOL_NOT_FOUND,
    build_error,
    new_trace_id,
)
from gdx_dispatch.core.mcp_registry import check_capability, get_tool
from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor

logger = logging.getLogger(__name__)


@dataclass
class InvocationResult:
    """Outcome of a single MCP tool invocation.

    ``ok`` is False whenever an error envelope was produced, *including*
    approval_required (202). Transport layers inspect ``error_type`` to
    decide the HTTP status via :func:`mcp_error_schema.status_for`.
    """

    ok: bool
    trace_id: str
    descriptor: ToolDescriptor | None = None
    result: Any = None
    error_type: str | None = None
    error_body: dict[str, Any] | None = None
    duration_ms: float = 0.0
    audit_row: dict[str, Any] = field(default_factory=dict)


# ── Input validation ───────────────────────────────────────────────────────
#
# We hand-roll the subset of JSON Schema we care about rather than
# pulling in jsonschema as a transport-layer dep. The descriptors in
# gdx_dispatch/core/mcp_tools/*.py use a narrow slice (type=object,
# required=[...], properties={}, type=string/number/boolean on fields),
# so this is sufficient. For future expansion, swap to jsonschema at a
# single call site.


def _validate_input(
    schema: dict[str, Any], payload: Any, _path: str = ""
) -> list[str]:
    """Return a list of validation error strings; empty list = valid.

    Recurses into nested ``object`` schemas via ``properties[k]``. Honors
    ``additionalProperties: false`` at any level by rejecting keys not
    declared in ``properties``. Without that flag, unknown keys remain
    tolerated for forward compat.
    """
    errors: list[str] = []
    if not schema:
        return errors
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(payload, dict):
            label = _path or "<root>"
            return [f"{label}: expected object, got {type(payload).__name__}"]
        required = schema.get("required", [])
        for field_name in required:
            if field_name not in payload:
                here = f"{_path}.{field_name}" if _path else field_name
                errors.append(f"missing required field {here!r}")
        props = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, val in payload.items():
            prop_schema = props.get(key)
            child_path = f"{_path}.{key}" if _path else key
            if prop_schema is None:
                if additional is False:
                    errors.append(f"unexpected field {child_path!r}")
                # else: tolerated (forward compat)
                continue
            prop_type = prop_schema.get("type")
            if prop_type == "object":
                # Recurse into nested object.
                errors.extend(_validate_input(prop_schema, val, child_path))
            elif prop_type == "array":
                if not isinstance(val, list):
                    errors.append(
                        f"field {child_path!r}: expected array, got {type(val).__name__}"
                    )
                else:
                    items_schema = prop_schema.get("items")
                    if isinstance(items_schema, dict):
                        for idx, item in enumerate(val):
                            errors.extend(
                                _validate_input(
                                    items_schema, item, f"{child_path}[{idx}]"
                                )
                            )
            elif prop_type and not _typematch(prop_type, val):
                errors.append(
                    f"field {child_path!r}: expected {prop_type}, got {type(val).__name__}"
                )
    elif expected_type and not _typematch(expected_type, payload):
        label = _path or "<root>"
        errors.append(f"{label}: expected {expected_type}, got {type(payload).__name__}")
    return errors


def _filter_payload_to_schema(schema: dict[str, Any], payload: Any) -> Any:
    """Drop keys not declared in ``schema.properties`` before invocation.

    0.9-s R2: the invocation path unpacks ``payload`` as ``**kwargs`` into
    the handler (lines below). Unless the tool's schema explicitly sets
    ``additionalProperties: false``, the base validator (``_validate_input``
    above) tolerates unknown keys for forward compat. Without this filter,
    those unknown keys would reach any handler written with ``**kwargs``.
    Filter here regardless so extra request fields can't smuggle data into
    the handler even when the tool author forgets the strict flag.

    Only applies at the top level; nested objects are unchanged (handlers
    are responsible for their own internal shapes).
    """
    if not isinstance(payload, dict) or not schema:
        return payload
    props = schema.get("properties") or {}
    if not props:
        return payload
    return {k: v for k, v in payload.items() if k in props}


def _typematch(expected: str, val: Any) -> bool:
    if expected == "string":
        return isinstance(val, str)
    if expected == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if expected == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if expected == "boolean":
        return isinstance(val, bool)
    if expected == "object":
        return isinstance(val, dict)
    if expected == "array":
        return isinstance(val, list)
    if expected == "null":
        return val is None
    # Unknown expected type: accept (conservative).
    return True


# ── Input redaction for audit ──────────────────────────────────────────────


def _redact_sensitive(descriptor: ToolDescriptor, payload: Any) -> Any:
    """Return a copy of ``payload`` with sensitive fields masked.

    A field is sensitive when its property schema carries
    ``"sensitive": true``. Sensitive values are replaced with the
    sentinel string ``"[REDACTED]"``. Non-sensitive fields are copied
    verbatim.
    """
    if not isinstance(payload, dict):
        return payload
    props = (descriptor.input_schema or {}).get("properties", {})
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if props.get(k, {}).get("sensitive") is True:
            out[k] = "[REDACTED]"
        else:
            out[k] = v
    return out


def _input_hash(payload: Any) -> str:
    """Stable sha256 over a JSON-canonical form of the payload."""
    try:
        canonical = json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        canonical = repr(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Audit ──────────────────────────────────────────────────────────────────


def _real_log_execution(row: dict[str, Any], db: Session | None = None) -> None:
    """Write the SS-19 execution row into audit_logs.

    Best-effort: an audit-write failure does NOT propagate. Tool-invocation
    success must not depend on audit-table health (a broken audit table
    would otherwise take down every tenant that exercises a tool)."""
    if db is None:
        logger.debug("mcp.invoke.audit %s", row)
        return

    # Truncate details to ~8KB to prevent audit table bloat.
    details: Any = row
    try:
        serialized = json.dumps(row, default=str)
        if len(serialized.encode("utf-8")) > 8192:
            details = {"truncated_details": serialized[:8192]}
    except Exception:  # noqa: BLE001 — best-effort serialization
        details = {"error": "failed to serialize audit details"}

    try:
        log_audit_event_sync(
            db,
            action="mcp.tool_invoke",
            tenant_id=row.get("tenant_id"),
            user_id=row.get("identity_id"),
            entity_type="mcp_tool",
            entity_id=row.get("tool_name"),
            details=details,
        )
    except Exception as exc:  # noqa: BLE001 — audit must never fail the request
        logger.warning("mcp.invoke.audit_write_failed: %s | row=%s", exc, row)


# ── Main entrypoint ────────────────────────────────────────────────────────


async def invoke_tool(
    name: str,
    payload: Any,
    *,
    principal: Any,
    db: Session | None = None,
    approval_ref: str | None = None,
    log_fn: Any = None,
    trace_id: str | None = None,
) -> InvocationResult:
    """Resolve + validate + cap-check + execute a registered MCP tool.

    Parameters
    ----------
    name
        Dotted MCP tool name (e.g. ``"customer.lookup"``).
    payload
        The ``input`` dict supplied by the caller (already JSON-decoded).
    principal
        Caller identity — must expose ``.capabilities`` (iterable of
        dicts). Shape follows ``RouterPrincipal`` from SS-18.
    db
        Database session for audit logging.
    approval_ref
        Set by the transport layer on a second call for an
        approval-gated tool. When present AND the descriptor is
        ``approval_required``, the handler runs normally.
    log_fn
        Audit row writer; defaults to the stub.
    trace_id
        Carry-through trace id; one is generated if absent.

    Returns
    -------
    InvocationResult
        ``ok=True`` when the handler ran successfully. Otherwise
        ``error_type`` + ``error_body`` are populated and transport
        decides HTTP status via ``mcp_error_schema.status_for``.
    """
    tid = trace_id or new_trace_id()
    # If log_fn is provided, we use it. Otherwise we use the real logger.
    # Note: the signature of log_fn in the original code was (row: dict).
    # The new _real_log_execution is (row, db).
    # To maintain compatibility with the existing call sites that pass a
    # lambda row: ..., we wrap the real logger.
    log = log_fn or (lambda row: _real_log_execution(row, db=db))
    t0 = time.perf_counter()

    # 1. Resolve
    resolved = get_tool(name)
    if resolved is None:
        body = build_error(
            ERROR_TOOL_NOT_FOUND,
            f"tool {name!r} is not registered",
            trace_id=tid,
            tool=name,
        )
        return InvocationResult(
            ok=False, trace_id=tid, error_type=ERROR_TOOL_NOT_FOUND, error_body=body
        )
    descriptor, handler = resolved

    # 2. Validate input — BEFORE capability check, by spec.
    errors = _validate_input(descriptor.input_schema, payload)
    if errors:
        body = build_error(
            ERROR_INPUT_INVALID,
            "input failed schema validation",
            trace_id=tid,
            tool=name,
            errors=errors,
        )
        return InvocationResult(
            ok=False,
            trace_id=tid,
            descriptor=descriptor,
            error_type=ERROR_INPUT_INVALID,
            error_body=body,
        )

    # 3. Capability check — deterministic, no side-effects.
    if not check_capability(principal, descriptor):
        body = build_error(
            ERROR_CAPABILITY_DENIED,
            f"tool {name!r} requires capabilities "
            f"{[list(c) for c in descriptor.capabilities_required]!r}",
            trace_id=tid,
            tool=name,
            capabilities_required=[list(c) for c in descriptor.capabilities_required],
            sensitivity_class=descriptor.sensitivity_class,
        )
        audit_row = _make_audit_row(
            descriptor=descriptor,
            payload=payload,
            principal=principal,
            outcome="denied",
            trace_id=tid,
        )
        log(audit_row)
        return InvocationResult(
            ok=False,
            trace_id=tid,
            descriptor=descriptor,
            error_type=ERROR_CAPABILITY_DENIED,
            error_body=body,
            audit_row=audit_row,
        )

    # 4. Blast Radius & Approval Gate
    # blast_radius is the source of truth; approval_required is honored for
    # backward compat with already-registered tools that pre-date S9.
    effective_approval_required = (
        descriptor.approval_required
        or descriptor.blast_radius in ("yellow", "red")
    )

    # 4a. Red-level tools require admin capability. This check fires
    # REGARDLESS of approval_ref — caller cannot bypass the admin gate by
    # supplying any approval_ref. The entity_type is the tool name (we don't
    # have a separate entity_type field on ToolDescriptor; if/when one lands,
    # this lookup gets one line tighter).
    if descriptor.blast_radius == "red":
        principal_caps = list(getattr(principal, "capabilities", []) or [])

        def _is_admin(cap: Any) -> bool:
            # Caps may be tuples (action, resource) or dicts {"action":..., "resource_type":...}.
            if isinstance(cap, dict):
                action = cap.get("action")
                resource = cap.get("resource_type") or cap.get("resource")
            else:
                try:
                    action, resource = cap[0], cap[1]
                except (TypeError, IndexError, KeyError):
                    logger.warning(
                        "mcp_invoke: malformed capability entry %r — denying",
                        cap,
                    )
                    return False
            # Security #4: the broad ("*","*") capability — which every
            # mcp:invoke token carries — must NOT clear the red-tool admin gate,
            # or any MCP token could invoke destructive tools. Only an explicit
            # admin capability (("admin", <tool>) or ("admin","*")) qualifies.
            return action == "admin" and resource in (name, "*")

        if not any(_is_admin(c) for c in principal_caps):
            body = build_error(
                ERROR_CAPABILITY_DENIED,
                f"tool {name!r} (red blast radius) requires admin capability",
                trace_id=tid,
                tool=name,
                capabilities_required=[("admin", name), ("*", "*")],
                sensitivity_class=descriptor.sensitivity_class,
            )
            audit_row = _make_audit_row(
                descriptor=descriptor,
                payload=payload,
                principal=principal,
                outcome="denied",
                trace_id=tid,
            )
            log(audit_row)
            return InvocationResult(
                ok=False,
                trace_id=tid,
                descriptor=descriptor,
                error_type=ERROR_CAPABILITY_DENIED,
                error_body=body,
                audit_row=audit_row,
            )

    # 4b. Approval gate — first call on yellow/red without approval_ref → 202.
    if effective_approval_required and not approval_ref:

        body = build_error(
            ERROR_APPROVAL_REQUIRED,
            f"tool {name!r} is approval-gated; rotation staged",
            trace_id=tid,
            tool=name,
            status="pending_approval",
        )
        # We still invoke the handler so it can stage the intent
        # (pat.rotate returns rotation_id in this state). 0.9-s R2:
        # filter to declared schema properties before unpacking.
        filtered = _filter_payload_to_schema(descriptor.input_schema, payload)
        try:
            staged = await handler(
                principal=principal, db=db, **(filtered if isinstance(filtered, dict) else {})
            )
            if isinstance(staged, dict):
                body.setdefault("result", staged)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mcp.invoke approval-stage failed tool=%s err=%s", name, exc)
        audit_row = _make_audit_row(
            descriptor=descriptor,
            payload=payload,
            principal=principal,
            outcome="pending_approval",
            trace_id=tid,
        )
        log(audit_row)
        return InvocationResult(
            ok=False,
            trace_id=tid,
            descriptor=descriptor,
            error_type=ERROR_APPROVAL_REQUIRED,
            error_body=body,
            audit_row=audit_row,
        )

    # 5. Execute. 0.9-s R2: filter to schema properties so undeclared
    # keys from the request body never reach handlers via **kwargs.
    filtered = _filter_payload_to_schema(descriptor.input_schema, payload)
    try:
        result = await handler(
            principal=principal,
            db=db,
            **(filtered if isinstance(filtered, dict) else {}),
        )
    except Exception as exc:  # noqa: BLE001 — ALL handler failures captured here
        body = build_error(
            ERROR_EXECUTION_ERROR,
            f"handler raised {type(exc).__name__}: {exc}",
            trace_id=tid,
            tool=name,
        )
        audit_row = _make_audit_row(
            descriptor=descriptor,
            payload=payload,
            principal=principal,
            outcome="error",
            trace_id=tid,
            error_detail=f"{type(exc).__name__}: {exc}",
        )
        log(audit_row)
        logger.exception("mcp.invoke handler raised tool=%s", name)
        return InvocationResult(
            ok=False,
            trace_id=tid,
            descriptor=descriptor,
            error_type=ERROR_EXECUTION_ERROR,
            error_body=body,
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            audit_row=audit_row,
        )

    # 6. Audit allowed + return
    duration_ms = (time.perf_counter() - t0) * 1000.0
    audit_row = _make_audit_row(
        descriptor=descriptor,
        payload=payload,
        principal=principal,
        outcome="allowed",
        trace_id=tid,
        duration_ms=duration_ms,
    )
    log(audit_row)
    return InvocationResult(
        ok=True,
        trace_id=tid,
        descriptor=descriptor,
        result=result,
        duration_ms=duration_ms,
        audit_row=audit_row,
    )


def _make_audit_row(
    *,
    descriptor: ToolDescriptor,
    payload: Any,
    principal: Any,
    outcome: str,
    trace_id: str,
    duration_ms: float | None = None,
    error_detail: str | None = None,
) -> dict[str, Any]:
    """Build a plain-dict audit row matching the SS-19 model stub shape."""
    redacted = _redact_sensitive(descriptor, payload)
    caps_snapshot = list(getattr(principal, "capabilities", []) or [])
    tenant_id = getattr(principal, "tenant_id", None)
    identity_id = getattr(principal, "identity_id", None)
    return {
        "tool_name": descriptor.name,
        "tenant_id": tenant_id,
        "identity_id": str(identity_id) if identity_id is not None else None,
        "capabilities_snapshot": caps_snapshot,
        "input_hash": _input_hash(redacted),
        "input_redacted": redacted,
        "outcome": outcome,
        "trace_id": trace_id,
        "duration_ms": duration_ms,
        "error_detail": error_detail,
    }
