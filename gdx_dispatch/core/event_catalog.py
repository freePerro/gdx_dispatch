"""SS-23 Slice A: Event Catalog Registry + JSON-Schema validation.

Registers one JSON Schema file per event type (canonical name:
``gdx.<domain>.<action>.v<N>``). The catalog is the single source of
truth for what a valid event payload looks like.

Public surface:
    - ``validate_event(event_type, payload)`` — raises ``EventSchemaError``
      on unknown type or schema violation.
    - ``list_event_types()`` — returns ``[{"event_type": ..., "schema": ...}]``
      for introspection / dev portal.
    - ``register_schema(event_type, schema)`` — programmatic registration
      (used by tests; normal flow is file discovery).

TODO:
    - ``gdx_dispatch/core/events.py::emit_event`` should call ``validate_event()``
      before adding the outbox row. Intentionally NOT wired here —
      SS-24 owns the emit_event wire-in so this slice stays additive.
    - Router (``gdx_dispatch/routers/event_catalog.py``) is not yet mounted in
      ``gdx_dispatch/main.py``. Mount comes with SS-24 integration.

Validator choice:
    Uses ``jsonschema`` (installed: 4.26.0). A minimal stdlib fallback
    lives below as ``_stdlib_validate`` and is only used if the import
    fails, so this module still works on a trimmed runtime.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

try:  # preferred: real validator
    import jsonschema as _jsonschema  # type: ignore

    _HAVE_JSONSCHEMA = True
except ImportError:  # pragma: no cover — fallback path (trimmed runtime)
    # Narrow: only swallow "package not installed". Anything else
    # (e.g. a broken jsonschema install raising ValueError at import)
    # should propagate — silent fallback to the stdlib validator would
    # hide a real environmental breakage.
    _jsonschema = None  # type: ignore
    _HAVE_JSONSCHEMA = False


EVENT_TYPE_SEPARATOR = "."
SCHEMA_DIR = Path(__file__).parent / "event_schemas"


class EventSchemaError(ValueError):
    """Raised on unknown event type or schema violation."""


# ── registry ────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, dict[str, Any]] = {}


def _valid_event_name(name: str) -> bool:
    # gdx_dispatch.<domain>.<action>.v<N>  — all lowercase, dot-separated, explicit version
    parts = name.split(EVENT_TYPE_SEPARATOR)
    if len(parts) < 4:
        return False
    if parts[0] not in ("gdx_dispatch", "gdx"):  # accept both during transition
        return False
    if not parts[-1].startswith("v") or not parts[-1][1:].isdigit():
        return False
    return name == name.lower()


def register_schema(event_type: str, schema: dict[str, Any]) -> None:
    """Programmatic registration. Overwrites any prior registration."""
    if not _valid_event_name(event_type):
        raise EventSchemaError(
            f"invalid event_type {event_type!r}: must be gdx_dispatch.<domain>.<action>.v<N>"
        )
    _REGISTRY[event_type] = schema


def _discover_schemas() -> None:
    """Scan ``event_schemas/`` and register every ``<name>.json`` found."""
    if not SCHEMA_DIR.is_dir():
        return
    for path in sorted(SCHEMA_DIR.glob("*.json")):
        # Narrow to per-file shapes: one bad schema file shouldn't block
        # discovery of the rest. Anything broader (MemoryError etc.)
        # must propagate — it's not a per-file problem.
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(
                "event_catalog.schema_read_failed",
                extra={
                    "op": "read_schema",
                    "path": str(path),
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )
            continue
        # title wins, else filename stem
        event_type = data.get("title") or path.stem
        try:
            register_schema(event_type, data)
        except EventSchemaError as exc:
            logger.error(
                "event_catalog.schema_register_failed",
                extra={
                    "op": "register_schema",
                    "path": str(path),
                    "event_type": event_type,
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )


# ── validation ──────────────────────────────────────────────────────────────


def _stdlib_validate(payload: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Minimal JSON-Schema subset: type, required, properties, additionalProperties.

    Supports object / string / integer / number / boolean / array / null.
    Enough for the payload shapes SS-23 ships; NOT a full validator.
    """
    t = schema.get("type")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "null": type(None),
    }
    if t and t in type_map:
        expected = type_map[t]
        if t == "integer" and isinstance(payload, bool):
            # bools are ints in Python — JSON-Schema treats them distinctly
            raise EventSchemaError(f"{path}: expected integer, got boolean")
        if not isinstance(payload, expected):
            raise EventSchemaError(
                f"{path}: expected {t}, got {type(payload).__name__}"
            )

    if t == "object" and isinstance(payload, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in payload:
                raise EventSchemaError(f"{path}: missing required property {key!r}")

        props = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in payload.items():
            if key in props:
                _stdlib_validate(value, props[key], f"{path}.{key}")
            elif additional is False:
                raise EventSchemaError(
                    f"{path}: unexpected property {key!r} (additionalProperties=false)"
                )
            elif isinstance(additional, dict):
                _stdlib_validate(value, additional, f"{path}.{key}")

    if t == "array" and isinstance(payload, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, el in enumerate(payload):
                _stdlib_validate(el, items, f"{path}[{i}]")


def validate_event(event_type: str, payload: dict[str, Any]) -> None:
    """Validate ``payload`` against the registered schema for ``event_type``.

    Raises ``EventSchemaError`` on unknown type or violation.
    """
    schema = _REGISTRY.get(event_type)
    if schema is None:
        raise EventSchemaError(f"unknown event_type: {event_type!r}")

    if _HAVE_JSONSCHEMA:
        try:
            _jsonschema.validate(instance=payload, schema=schema)  # type: ignore[attr-defined]
        except _jsonschema.ValidationError as e:  # type: ignore[attr-defined]
            raise EventSchemaError(
                f"{event_type}: payload failed schema: {e.message}"
            ) from e
    else:
        _stdlib_validate(payload, schema)


def list_event_types() -> list[dict[str, Any]]:
    """Return [{"event_type": str, "schema": dict}, ...] sorted by event_type."""
    return [
        {"event_type": et, "schema": _REGISTRY[et]}
        for et in sorted(_REGISTRY.keys())
    ]


def is_registered(event_type: str) -> bool:
    return event_type in _REGISTRY


def _reset_for_tests() -> None:
    """Clear registry — tests only."""
    _REGISTRY.clear()


# ── auto-discover on import ─────────────────────────────────────────────────

_discover_schemas()
