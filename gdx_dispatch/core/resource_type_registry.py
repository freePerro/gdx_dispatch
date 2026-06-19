"""SS-33 Slice A: Resource Type Extensibility — in-process registry.

Declares NEW tenant/platform resource types at runtime without touching
core code. A resource type is a named JSON Schema + capability list that
tenants' data instances are validated against before persistence.

Shape model (mirrors SS-18 ToolDescriptor / SS-23 event_catalog):
    * ``name``              — ``gdx.<slug>.v<N>`` (platform)
                              or ``t_<tenant_prefix>.<slug>.v<N>`` (tenant-private)
    * ``json_schema``       — JSON Schema dict for instance payload
    * ``capabilities``      — list of (action, resource_type) tuples
                              permitted against instances of this type
    * ``owner_tenant_id``   — ``None`` → platform-wide; else tenant-private
    * ``description``       — human-facing blurb

Public surface:
    - ``register_type(name, json_schema, capabilities, owner_tenant_id=None, ...)``
    - ``get_type(name)`` → descriptor dict or None
    - ``list_types(owner_tenant_id=None, public_only=False)`` → list
    - ``validate_instance(type_name, payload)`` — raises ResourceSchemaError
    - ``unregister_type(name, *, super_admin=False)`` — platform types
      require ``super_admin=True``; they're immutable otherwise.

Integration TODO:
    * router + loader wire-in happens in ``gdx_dispatch/main.py`` — NOT wired here.
    * DB-backed tenant types loaded at app-start by
      ``gdx_dispatch.core.resource_type_loader``.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)

try:  # preferred
    import jsonschema as _jsonschema  # type: ignore

    _HAVE_JSONSCHEMA = True
except Exception:  # pragma: no cover
    _jsonschema = None  # type: ignore
    _HAVE_JSONSCHEMA = False


class ResourceTypeError(ValueError):
    """Raised for registration / lookup failures."""


class ResourceSchemaError(ValueError):
    """Raised when an instance payload fails schema validation.

    ``path`` is the JSON pointer-style location inside the payload that
    failed (``"$.field.sub"``). ``code`` is always ``schema_violation``
    for easy router-side mapping to 400-detail.
    """

    def __init__(self, message: str, *, path: str = "$", code: str = "schema_violation"):
        super().__init__(message)
        self.path = path
        self.code = code


# ── name validation ────────────────────────────────────────────────────────

_PLATFORM_NAME_RE = re.compile(r"^gdx_dispatch\.[a-z][a-z0-9_]*\.v\d+$")
# t_<alnum prefix>.<slug>.v<N> — tenant-private namespace
_TENANT_NAME_RE = re.compile(r"^t_[a-z0-9]+\.[a-z][a-z0-9_]*\.v\d+$")


def _is_platform_name(name: str) -> bool:
    return bool(_PLATFORM_NAME_RE.match(name))


def _is_tenant_name(name: str) -> bool:
    return bool(_TENANT_NAME_RE.match(name))


def _validate_name(name: str, owner_tenant_id: str | None) -> None:
    if not isinstance(name, str) or not name:
        raise ResourceTypeError("name must be a non-empty string")
    if owner_tenant_id is None:
        if not _is_platform_name(name):
            raise ResourceTypeError(
                f"platform-wide type name {name!r} must match "
                f"gdx_dispatch.<slug>.v<N> (lowercase slug + numeric version)"
            )
    else:
        if not _is_tenant_name(name):
            raise ResourceTypeError(
                f"tenant-private type name {name!r} must match "
                f"t_<tenant_prefix>.<slug>.v<N> to avoid collision with "
                f"platform 'gdx_dispatch.*' namespace"
            )


def _validate_schema_shape(schema: dict[str, Any]) -> None:
    if not isinstance(schema, dict):
        raise ResourceTypeError("json_schema must be a dict")
    if not schema:
        raise ResourceTypeError("json_schema must declare a top-level type")
    if (
        "type" not in schema
        and "$ref" not in schema
        and "oneOf" not in schema
        and "anyOf" not in schema
    ):
        raise ResourceTypeError(
            "json_schema must declare 'type', '$ref', 'oneOf', or 'anyOf' at top level"
        )


def _normalise_capabilities(caps: Iterable[Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in caps or []:
        if isinstance(item, str):
            raise ResourceTypeError(
                f"capabilities must be (action, resource_type) tuples, "
                f"not colon-strings: got {item!r}"
            )
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            raise ResourceTypeError(
                f"capability entry must be (action, resource_type), got {item!r}"
            )
        action, rtype = item
        if not isinstance(action, str) or not action:
            raise ResourceTypeError(f"capability action must be non-empty str: {item!r}")
        if not isinstance(rtype, str) or not rtype:
            raise ResourceTypeError(f"capability resource_type must be non-empty str: {item!r}")
        out.append((action, rtype))
    return out


# ── registry ───────────────────────────────────────────────────────────────

_REGISTRY: dict[str, dict[str, Any]] = {}


def register_type(
    name: str,
    json_schema: dict[str, Any],
    capabilities: Iterable[Any] | None = None,
    owner_tenant_id: str | None = None,
    *,
    description: str = "",
    index_hints: list[str] | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Register a resource type.

    Platform-wide types (``owner_tenant_id is None``) are immutable after
    registration; re-registration requires ``replace=True`` (only the
    in-process loader uses that during app start).
    """
    _validate_name(name, owner_tenant_id)
    _validate_schema_shape(json_schema)
    caps = _normalise_capabilities(capabilities or [])

    existing = _REGISTRY.get(name)
    if existing is not None and not replace:
        # Same descriptor → idempotent no-op
        if (
            existing["json_schema"] == json_schema
            and existing["capabilities"] == caps
            and existing["owner_tenant_id"] == owner_tenant_id
        ):
            return existing
        raise ResourceTypeError(
            f"resource type {name!r} already registered with a different descriptor"
        )

    descriptor: dict[str, Any] = {
        "name": name,
        "json_schema": json_schema,
        "capabilities": caps,
        "owner_tenant_id": owner_tenant_id,
        "description": description or "",
        "index_hints": list(index_hints or []),
        "is_platform": owner_tenant_id is None,
    }
    _REGISTRY[name] = descriptor
    logger.debug("resource_type_registry.register name=%s owner=%s", name, owner_tenant_id)
    return descriptor


def get_type(name: str) -> dict[str, Any] | None:
    return _REGISTRY.get(name)


def list_types(
    owner_tenant_id: str | None = None,
    public_only: bool = False,
) -> list[dict[str, Any]]:
    """List visible types.

    * ``public_only=True`` → only platform-wide types.
    * ``owner_tenant_id`` set → platform-wide + that tenant's private types.
    * Both unset → all types (admin / internal view).
    """
    rows = sorted(_REGISTRY.values(), key=lambda d: d["name"])
    if public_only:
        return [d for d in rows if d["owner_tenant_id"] is None]
    if owner_tenant_id is not None:
        return [
            d for d in rows
            if d["owner_tenant_id"] is None or d["owner_tenant_id"] == owner_tenant_id
        ]
    return rows


def unregister_type(name: str, *, super_admin: bool = False) -> None:
    """Remove a registered type.

    Tenant-private types can be removed by their owner (enforced at
    router layer). Platform-wide types require ``super_admin=True``;
    the 7-day grace period is enforced at the router layer, not here,
    because the registry is in-process and has no clock of its own.
    """
    existing = _REGISTRY.get(name)
    if existing is None:
        raise ResourceTypeError(f"unknown resource type: {name!r}")
    if existing["owner_tenant_id"] is None and not super_admin:
        raise ResourceTypeError(
            f"platform-wide type {name!r} is immutable; super-admin required"
        )
    del _REGISTRY[name]


def validate_instance(type_name: str, payload: dict[str, Any]) -> None:
    """Validate ``payload`` against the registered type's JSON Schema.

    Raises ``ResourceSchemaError`` with ``.path`` pointing at the first
    failing location. Silent failure is never acceptable.
    """
    descriptor = _REGISTRY.get(type_name)
    if descriptor is None:
        raise ResourceTypeError(f"unknown resource type: {type_name!r}")
    schema = descriptor["json_schema"]

    if _HAVE_JSONSCHEMA:
        try:
            _jsonschema.validate(instance=payload, schema=schema)  # type: ignore[attr-defined]
        except _jsonschema.ValidationError as e:  # type: ignore[attr-defined]
            path = "$"
            if e.absolute_path:
                path = "$." + ".".join(str(p) for p in e.absolute_path)
            raise ResourceSchemaError(
                f"{type_name}: schema violation at {path}: {e.message}",
                path=path,
            ) from e
    else:
        _stdlib_validate(payload, schema)


def _stdlib_validate(payload: Any, schema: dict[str, Any], path: str = "$") -> None:
    """Minimal JSON-Schema fallback mirroring event_catalog._stdlib_validate."""
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
            raise ResourceSchemaError(
                f"{path}: expected integer, got boolean", path=path
            )
        if not isinstance(payload, expected):
            raise ResourceSchemaError(
                f"{path}: expected {t}, got {type(payload).__name__}", path=path
            )

    if t == "object" and isinstance(payload, dict):
        for key in schema.get("required", []):
            if key not in payload:
                raise ResourceSchemaError(
                    f"{path}: missing required property {key!r}", path=path
                )
        props = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in payload.items():
            if key in props:
                _stdlib_validate(value, props[key], f"{path}.{key}")
            elif additional is False:
                raise ResourceSchemaError(
                    f"{path}: unexpected property {key!r}", path=f"{path}.{key}"
                )

    if t == "array" and isinstance(payload, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for i, el in enumerate(payload):
                _stdlib_validate(el, items, f"{path}[{i}]")


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def _reset_for_tests() -> None:
    """Clear registry — tests only."""
    _REGISTRY.clear()
