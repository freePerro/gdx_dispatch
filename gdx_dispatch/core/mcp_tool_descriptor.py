"""SS-18 slice A — MCP ToolDescriptor Pydantic model.

A ``ToolDescriptor`` is the wire-visible contract for a single MCP tool.
It is **decoupled from transport** (D-40): SS-19 ships the HTTP/SSE
adapter that serialises descriptors; SS-18 ships the descriptor shape
and the in-process registry.

Design rules (aligned with the SS-18 plan + v3 patch P38)
---------------------------------------------------------
* ``capabilities_required`` is a list of ``(action, resource_type)``
  **tuples**, NEVER colon-strings. Colon-string capability encoding was
  retired in the v3 patch; downstream capability checks consume the
  tuple form directly.
* ``sensitivity_class`` mirrors SS-15 ``custom_field_sensitivity``:
  ``public`` | ``internal`` | ``restricted``. Tools classified as
  ``restricted`` require a caller capability that carries
  ``restricted=True``.
* ``input_schema`` / ``output_schema`` are JSON Schema dicts. They are
  validated at descriptor-construction time to fail loudly on malformed
  schemas — a broken schema in production hides the real problem
  (silent client-side validation failures).
* ``approval_required`` flags descriptors whose handlers must go
  through the SS-15 ``status='pending_approval'`` gate before any
  side-effect runs. The registry-level ``describe_tool`` surfaces this
  so MCP clients can show the "this will require approval" affordance.
* ``blast_radius`` governs the execution safety level:
  ``green`` (immediate) | ``yellow`` (approval required) | ``red`` (admin required).
  This is the primary driver for execution gating.

The descriptor is deliberately frozen / immutable at the Pydantic level
so a mutated dict in one caller cannot drift the shared registry entry.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SensitivityClass = Literal["public", "internal", "restricted"]
VALID_SENSITIVITY: frozenset[str] = frozenset({"public", "internal", "restricted"})

BlastRadius = Literal["green", "yellow", "red"]


class ToolDescriptor(BaseModel):
    """Descriptor for a single MCP tool.

    Handlers are NOT carried on the descriptor — they live in the
    registry's handler map. That keeps the wire-visible ``ToolDescriptor``
    JSON-serialisable without custom encoders.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(..., min_length=1, max_length=2000)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    # Tuples of (action, resource_type). NOT colon-strings (v3 patch P38).
    # Declared as list[Any] so the custom validator can produce a
    # clearer error for colon-strings (v3 patch P38) before Pydantic's
    # built-in tuple-coercion error fires.
    capabilities_required: list[Any] = Field(default_factory=list)
    sensitivity_class: SensitivityClass = "internal"
    approval_required: bool = False
    blast_radius: BlastRadius = "green"
    version: str = "1"

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("tool name must not be empty")
        # MCP tool names are dotted (e.g. "customer.lookup"). Keep a
        # narrow character set so registry lookups are unambiguous.
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
        bad = set(v) - allowed
        if bad:
            raise ValueError(
                f"tool name {v!r} contains disallowed characters: {sorted(bad)!r}"
            )
        return v

    @field_validator("capabilities_required", mode="before")
    @classmethod
    def _validate_caps(cls, v):
        if not isinstance(v, list):
            raise ValueError("capabilities_required must be a list")
        normalised: list[tuple[str, str]] = []
        for item in v:
            if isinstance(item, str):
                # v3 patch P38: colon-strings are banned.
                raise ValueError(
                    f"capabilities_required must be (action, resource_type) "
                    f"tuples, not colon-strings: got {item!r}"
                )
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError(
                    f"capability entry must be a 2-tuple (action, resource_type), got {item!r}"
                )
            action, resource_type = item
            if not isinstance(action, str) or not action:
                raise ValueError(f"capability action must be a non-empty string: {item!r}")
            if not isinstance(resource_type, str) or not resource_type:
                raise ValueError(f"capability resource_type must be a non-empty string: {item!r}")
            normalised.append((action, resource_type))
        return normalised

    @field_validator("input_schema", "output_schema")
    @classmethod
    def _validate_schema_shape(cls, v: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(v, dict):
            raise ValueError("schema must be a JSON Schema dict")
        if v and "type" not in v and "$ref" not in v and "oneOf" not in v and "anyOf" not in v:
            # A well-formed JSON Schema declares a top-level structural
            # keyword. Empty dict is treated as "unspecified" and is
            # allowed for tools that take/return no structured data.
            raise ValueError(
                "schema must declare 'type', '$ref', 'oneOf', or 'anyOf' at the top level"
            )
        return v

    def to_public_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict for list endpoints (tuples → lists)."""
        d = self.model_dump()
        d["capabilities_required"] = [list(c) for c in self.capabilities_required]
        return d
