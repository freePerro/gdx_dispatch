"""SS-19 platform additions stub.

Isolation rule (same as SS-15 / SS-17 / SS-18): any new tables or
columns required for SS-19 land here, NOT in
``gdx_dispatch/models/platform.py`` or ``gdx_dispatch/models/platform_extensions.py``.
Integration is a single conscious merge at sprint end.

Tables required by SS-19
========================

``mcp_execution_log``
  Per-invocation execution record written by the MCP execute router
  (``gdx_dispatch/routers/mcp_execute.py``) and SSE router
  (``gdx_dispatch/routers/mcp_sse.py``). Complements the SS-18
  ``mcp_tool_execution_audit`` table: whereas audit-table focuses on
  capability + approval outcome, this table focuses on runtime
  metadata (duration, result hash, chunk count for streaming).

  Columns (see ``SS19_MCP_EXECUTION_LOG_COLUMNS`` below):
    * ``id`` — UUID PK
    * ``trace_id`` — VARCHAR(64) NOT NULL (UUID4 from
      ``mcp_error_schema.new_trace_id``; links log ⇄ error bodies ⇄
      downstream event rows)
    * ``tool_name`` — VARCHAR(120) NOT NULL
    * ``tenant_id`` — VARCHAR(64) NOT NULL
    * ``identity_id`` — VARCHAR(36) NOT NULL (caller)
    * ``capabilities_snapshot`` — JSON NOT NULL (caller caps at call
      time; denormalised so a later cap revoke doesn't rewrite
      history)
    * ``input_hash`` — VARCHAR(64) NOT NULL (sha256 over canonicalised
      + redacted input)
    * ``input_redacted`` — JSON NOT NULL (input with sensitive fields
      replaced by "[REDACTED]"; see
      ``gdx_dispatch.core.mcp_invoke._redact_sensitive`` + the
      ``"sensitive": true`` marker on descriptor input_schema
      properties)
    * ``outcome`` — VARCHAR(32) NOT NULL
      (``allowed`` | ``denied`` | ``pending_approval`` | ``error``)
    * ``error_type`` — VARCHAR(32) NULL
      (matches ``mcp_error_schema`` slugs when present)
    * ``error_detail`` — TEXT NULL
    * ``duration_ms`` — DOUBLE PRECISION NULL (null for denied-before-
      execute rows)
    * ``chunk_count`` — INTEGER NULL (SSE streams only)
    * ``started_at`` — TIMESTAMPTZ NOT NULL DEFAULT now()
    * ``completed_at`` — TIMESTAMPTZ NULL

Proposed descriptor fields (ToolDescriptor)
-------------------------------------------
SS-19 did NOT modify ``gdx_dispatch/core/mcp_tool_descriptor.py`` (SS-18 code is
off-limits per orchestrator rules). These fields are **proposed** for
the integration merge:

* ``streaming: bool = False`` — explicit flag that the SSE router
  consults when deciding whether ``GET /api/mcp/events?tool=...`` is
  valid. Today the SSE router falls back to introspecting whether the
  handler is an ``async`` generator, OR whether
  ``output_schema["x-mcp-streaming"] == True``. A proper field is
  cleaner and should land when SS-18 + SS-19 reconcile.
* ``approval_ttl_seconds: int | None = None`` — how long a staged
  approval stays valid. Today enforcement lives outside the descriptor
  (SS-15 approval flow); documenting here for visibility.

TODO
  * Translate the specs below into real ``Table(...)`` declarations on
    ``ControlBase.metadata`` via a single edit to
    ``gdx_dispatch/models/platform_extensions.py`` at SS-19 integration time.
  * Rename ``TODO_ss19_mcp_execute_XXXX.py`` in
    ``gdx_dispatch/migrations/versions/`` (control-plane alembic) to the next
    sequential number and set ``down_revision`` to the current head at
    integration time.
  * Do NOT import this module from ``platform_extensions.py`` —
    keeping it inert preserves the base platform graph.
  * Propose the two descriptor fields above to the SS-18 owner; update
    ``gdx_dispatch/routers/mcp_sse.py::_is_streaming`` to consult the explicit
    flag once it lands.
"""
from __future__ import annotations


# Plain dict specs, deliberately not bound to a SQLAlchemy mapper so
# the presence of this module does not mutate ControlBase metadata.

SS19_MCP_EXECUTION_LOG_COLUMNS: list[tuple[str, str]] = [
    ("id", "UUID, primary_key=True"),
    ("trace_id", "String(64), nullable=False, index=True"),
    ("tool_name", "String(120), nullable=False, index=True"),
    ("tenant_id", "String(64), nullable=False, index=True"),
    ("identity_id", "String(36), nullable=False"),
    ("capabilities_snapshot", "JSON, nullable=False"),
    ("input_hash", "String(64), nullable=False"),
    ("input_redacted", "JSON, nullable=False"),
    ("outcome", "String(32), nullable=False"),
    ("error_type", "String(32), nullable=True"),
    ("error_detail", "Text, nullable=True"),
    ("duration_ms", "Float, nullable=True"),
    ("chunk_count", "Integer, nullable=True"),
    (
        "started_at",
        "DateTime(timezone=True), nullable=False, server_default=sa.func.now()",
    ),
    ("completed_at", "DateTime(timezone=True), nullable=True"),
]


VALID_OUTCOMES: frozenset[str] = frozenset(
    {"allowed", "denied", "pending_approval", "error"}
)


# Proposed additions to ToolDescriptor — documented here, NOT applied
# to gdx_dispatch/core/mcp_tool_descriptor.py (SS-18 code is immutable in this
# sprint per orchestrator rules).
PROPOSED_DESCRIPTOR_FIELDS: dict[str, str] = {
    "streaming": "bool = False",
    "approval_ttl_seconds": "int | None = None",
}
