"""SS-18 platform additions stub.

Isolation rule (same as SS-15 / SS-17): any new tables or columns
required for SS-18 land here, NOT in ``gdx_dispatch/models/platform.py`` or
``gdx_dispatch/models/platform_extensions.py``. Integration is a single
conscious merge at sprint end, not a surprise diff on the base
platform graph.

Tables required by SS-18
========================

``mcp_tool_registration``
  Persistent record of each MCP tool known to the platform. The
  in-memory ``gdx_dispatch.core.mcp_registry`` is the working catalog; this
  table is the audit-grade "what tools did we expose, since when, at
  what version" record.

  Columns:
    * ``id`` — UUID PK
    * ``name`` — VARCHAR(120) UNIQUE NOT NULL (dotted name)
    * ``version`` — VARCHAR(32) NOT NULL DEFAULT '1'
    * ``sensitivity_class`` — VARCHAR(32) NOT NULL DEFAULT 'internal'
      (``public`` | ``internal`` | ``restricted`` per SS-15 taxonomy)
    * ``capabilities_required`` — JSON NOT NULL (list of
      ``[action, resource_type]`` pairs — NOT colon-strings, per v3
      patch P38)
    * ``descriptor`` — JSON NOT NULL (full descriptor snapshot)
    * ``enabled`` — BOOLEAN NOT NULL DEFAULT true
    * ``created_at`` — TIMESTAMPTZ NOT NULL DEFAULT now()
    * ``updated_at`` — TIMESTAMPTZ NOT NULL DEFAULT now()

``mcp_tool_execution_audit``
  Per-call audit log. Every capability-gated handler invocation
  records exactly one row BEFORE the handler runs (outcome is patched
  post-hoc) so denied calls are recorded just as reliably as allowed
  ones.

  Columns:
    * ``id`` — UUID PK
    * ``tool_name`` — VARCHAR(120) NOT NULL
    * ``tenant_id`` — VARCHAR(64) NOT NULL (scope)
    * ``identity_id`` — UUID NOT NULL (caller)
    * ``capabilities_snapshot`` — JSON NOT NULL (caller's caps at call time)
    * ``input_hash`` — VARCHAR(64) NOT NULL (sha256 of canonicalised input)
    * ``outcome`` — VARCHAR(32) NOT NULL
      (``pending`` | ``allowed`` | ``denied`` | ``error`` | ``pending_approval``)
    * ``approval_ref`` — UUID NULL (FK to access_tokens.id when
      approval-gated; nullable for non-approval tools)
    * ``started_at`` — TIMESTAMPTZ NOT NULL DEFAULT now()
    * ``completed_at`` — TIMESTAMPTZ NULL
    * ``error_detail`` — TEXT NULL

INTEGRATION TODO
  * Translate the specs below into real ``Table(...)`` declarations on
    ``ControlBase.metadata`` via a single edit to
    ``gdx_dispatch/models/platform_extensions.py`` at SS-18 integration time.
  * Rename ``TODO_ss18_mcp_registry_XXXX.py`` in
    ``gdx_dispatch/migrations/versions/`` (control-plane alembic) to the next
    sequential number and set ``down_revision`` to the current head at
    integration time.
  * Do NOT import this module from ``platform_extensions.py`` —
    keeping it inert preserves the base platform graph.
"""
from __future__ import annotations


# Plain dict specs, deliberately not bound to a SQLAlchemy mapper so
# the presence of this module does not mutate ControlBase metadata.

SS18_MCP_TOOL_REGISTRATION_COLUMNS: list[tuple[str, str]] = [
    ("id", "UUID, primary_key=True"),
    ("name", "String(120), nullable=False, unique=True"),
    ("version", "String(32), nullable=False, server_default=sa.text(\"'1'\")"),
    (
        "sensitivity_class",
        "String(32), nullable=False, server_default=sa.text(\"'internal'\")",
    ),
    ("capabilities_required", "JSON, nullable=False"),
    ("descriptor", "JSON, nullable=False"),
    ("enabled", "Boolean, nullable=False, server_default=sa.text('true')"),
    ("created_at", "DateTime(timezone=True), nullable=False, server_default=sa.func.now()"),
    ("updated_at", "DateTime(timezone=True), nullable=False, server_default=sa.func.now()"),
]


SS18_MCP_TOOL_EXECUTION_AUDIT_COLUMNS: list[tuple[str, str]] = [
    ("id", "UUID, primary_key=True"),
    ("tool_name", "String(120), nullable=False"),
    ("tenant_id", "String(64), nullable=False"),
    ("identity_id", "UUID, nullable=False"),
    ("capabilities_snapshot", "JSON, nullable=False"),
    ("input_hash", "String(64), nullable=False"),
    ("outcome", "String(32), nullable=False"),
    ("approval_ref", "UUID, nullable=True"),
    ("started_at", "DateTime(timezone=True), nullable=False, server_default=sa.func.now()"),
    ("completed_at", "DateTime(timezone=True), nullable=True"),
    ("error_detail", "Text, nullable=True"),
]


VALID_OUTCOMES: frozenset[str] = frozenset(
    {"pending", "allowed", "denied", "error", "pending_approval"}
)
