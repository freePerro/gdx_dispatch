"""SS-18 slice B — MCP tool registry.

In-process catalog of MCP tools. Separated from transport (D-40): SS-19
ships HTTP/SSE; here we only care about registration, lookup, and
capability-gated visibility.

INTEGRATION TODO
----------------
* register the router from ``gdx_dispatch/routers/mcp_registry.py`` in
  ``gdx_dispatch/main.py`` at sprint integration time. Until then the router is
  exercised via its own FastAPI test harness.
* at import time, ``gdx_dispatch/core/mcp_tools/__init__.py`` triggers
  registration of the initial tool set. When integration lands, ensure
  that import happens before request handling begins (or move the
  import into the router module).

Public API
----------
* :func:`register_tool(descriptor, handler)` — idempotent on name only
  when the call is identical; re-registration with a different
  descriptor / handler raises :class:`ToolAlreadyRegistered`.
* :func:`get_tool(name)` — returns ``(descriptor, handler) | None``
* :func:`describe_tool(name)` — returns descriptor (or ``None``)
* :func:`list_tools()` — all descriptors
* :func:`list_tools_for_principal(principal)` — capability-gated subset
* :func:`check_capability(principal, descriptor)` — returns bool
* :func:`clear_registry()` — test-only helper

Capability model
----------------
``descriptor.capabilities_required`` is a list of ``(action, resource_type)``
tuples. A principal passes the gate iff, for **every** required tuple:

1. They hold the exact ``(action, resource_type)`` capability, OR
2. They hold ``("*", "*")`` (wildcard).

For ``sensitivity_class == "restricted"`` descriptors the matching
capability must additionally carry ``restricted=True`` (v3 patch P36
behaviour from the SS-18 source plan).
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from gdx_dispatch.core.mcp_tool_descriptor import ToolDescriptor

logger = logging.getLogger(__name__)


ToolHandler = Callable[..., Awaitable[Any]]


class ToolAlreadyRegistered(RuntimeError):
    """Raised when two different descriptors/handlers register under the same name."""


class ToolNotFound(KeyError):
    """Raised when a handler is requested for an unregistered tool."""


_DESCRIPTORS: dict[str, ToolDescriptor] = {}
_HANDLERS: dict[str, ToolHandler] = {}


def register_tool(descriptor: ToolDescriptor, handler: ToolHandler) -> None:
    """Register a tool. Idempotent only for identical re-registration.

    Re-registering the same name with a different descriptor or handler
    is a loud error: silent overwrite was rejected in design review as
    a footgun for tests and module reload.
    """
    if not isinstance(descriptor, ToolDescriptor):
        raise TypeError(f"descriptor must be a ToolDescriptor, got {type(descriptor).__name__}")
    if not callable(handler):
        raise TypeError("handler must be callable")

    existing = _DESCRIPTORS.get(descriptor.name)
    if existing is not None:
        if existing == descriptor and _HANDLERS.get(descriptor.name) is handler:
            return  # idempotent no-op
        raise ToolAlreadyRegistered(
            f"tool {descriptor.name!r} is already registered with a different descriptor/handler"
        )
    _DESCRIPTORS[descriptor.name] = descriptor
    _HANDLERS[descriptor.name] = handler
    logger.debug("mcp.register_tool name=%s", descriptor.name)


def get_tool(name: str) -> tuple[ToolDescriptor, ToolHandler] | None:
    d = _DESCRIPTORS.get(name)
    h = _HANDLERS.get(name)
    if d is None or h is None:
        return None
    return d, h


def describe_tool(name: str) -> ToolDescriptor | None:
    return _DESCRIPTORS.get(name)


def list_tools(
    *, offset: int = 0, limit: int | None = None
) -> list[ToolDescriptor]:
    """Return registered tools, sorted by name for deterministic order.

    Red-team Pattern 6 (mcp_registry.py:116): the in-process registry
    could grow unbounded as more tools register, and any HTTP surface
    layered on top would emit the full list per call. ``offset`` /
    ``limit`` keep callers honest without forcing a behaviour change
    on existing call sites (both default to "return everything").

    The sort on ``descriptor.name`` is the "ORDER BY" equivalent here:
    Python dict iteration is insertion-order, which is stable within
    one process but not across restarts. Sorting gives us a
    deterministic order any caller can rely on.
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0 or None")
    ordered = sorted(_DESCRIPTORS.values(), key=lambda d: d.name)
    if offset:
        ordered = ordered[offset:]
    if limit is not None:
        ordered = ordered[:limit]
    return ordered


def clear_registry() -> None:
    """Test-only: drop all registrations."""
    _DESCRIPTORS.clear()
    _HANDLERS.clear()


def repopulate_from_cache() -> int:
    """Re-register tools from already-loaded mcp_tools submodule objects.

    Called by ``build_mcp_subapp`` when the registry appears empty or has
    orphan conflicts — a sign that a sibling test called ``clear_registry()``
    between mount invocations. The submodule objects (with their DESCRIPTOR
    and handler attributes) survive in ``sys.modules`` even after the
    registry is cleared, so re-registration is always possible.

    Returns the number of tools newly registered.
    """
    import sys

    count = 0
    for mod_name, mod in list(sys.modules.items()):
        if not mod_name.startswith("gdx_dispatch.core.mcp_tools."):
            continue
        descriptor = getattr(mod, "DESCRIPTOR", None)
        handler = getattr(mod, "handler", None)
        if descriptor is None or handler is None:
            continue
        if descriptor.name in _DESCRIPTORS:
            continue
        _DESCRIPTORS[descriptor.name] = descriptor
        _HANDLERS[descriptor.name] = handler
        count += 1
    return count


# ── Capability gating ──────────────────────────────────────────────────────


def _principal_caps(principal: Any) -> list[dict[str, Any]]:
    """Normalise a principal's capabilities to list[dict].

    Accepts either the dataclass-style ``RouterPrincipal`` (capabilities
    is a list of dicts) or any object with a ``.capabilities`` iterable
    of dicts. Tolerates tuple-shaped capabilities as a last resort.
    """
    caps = getattr(principal, "capabilities", None)
    if caps is None:
        return []
    out: list[dict[str, Any]] = []
    for c in caps:
        if isinstance(c, dict):
            action = c.get("action")
            resource_type = c.get("resource_type")
            if not isinstance(action, str) or not action:
                raise ValueError(
                    f"capability dict must have non-empty 'action' string, got {action!r}"
                )
            if not isinstance(resource_type, str) or not resource_type:
                raise ValueError(
                    f"capability dict must have non-empty 'resource_type' string, got {resource_type!r}"
                )
            out.append(c)
        elif isinstance(c, (tuple, list)) and len(c) == 2:
            action, resource_type = c[0], c[1]
            if not isinstance(action, str) or not action:
                raise ValueError(
                    f"capability tuple must have non-empty action string, got {action!r}"
                )
            if not isinstance(resource_type, str) or not resource_type:
                raise ValueError(
                    f"capability tuple must have non-empty resource_type string, got {resource_type!r}"
                )
            out.append({"action": action, "resource_type": resource_type})
        else:
            raise ValueError(
                f"capability must be dict or 2-tuple of (action, resource_type), got {type(c).__name__}"
            )
    return out


def check_capability(principal: Any, descriptor: ToolDescriptor) -> bool:
    """True iff ``principal`` holds every capability the descriptor requires.

    Sensitive (``restricted``) tools require the matching capability to
    also carry ``restricted=True`` (or the principal holds a restricted
    wildcard).
    """
    caps = _principal_caps(principal)
    if not caps:
        return False

    exact: set[tuple[str, str]] = {(c.get("action"), c.get("resource_type")) for c in caps}
    restricted: set[tuple[str, str]] = {
        (c.get("action"), c.get("resource_type")) for c in caps if c.get("restricted")
    }
    has_wildcard = ("*", "*") in exact
    has_restricted_wildcard = ("*", "*") in restricted

    is_restricted = descriptor.sensitivity_class == "restricted"

    for required in descriptor.capabilities_required:
        tup = tuple(required)
        if tup in exact or has_wildcard:
            if is_restricted and tup not in restricted and not has_restricted_wildcard:
                return False
            continue
        return False
    return True


def list_tools_for_principal(
    principal: Any, *, offset: int = 0, limit: int | None = None
) -> list[ToolDescriptor]:
    """Subset of :func:`list_tools` the principal may invoke.

    Pagination is applied **after** the capability filter so the page
    window reflects what the caller can actually see.
    """
    if offset < 0:
        raise ValueError("offset must be >= 0")
    if limit is not None and limit < 0:
        raise ValueError("limit must be >= 0 or None")
    filtered = [d for d in list_tools() if check_capability(principal, d)]
    if offset:
        filtered = filtered[offset:]
    if limit is not None:
        filtered = filtered[:limit]
    return filtered


class CapabilityDenied(PermissionError):
    """Raised by handler guards when a caller lacks the required capability."""


def require_capability(principal: Any, descriptor: ToolDescriptor) -> None:
    """Guard helper for handlers: raise :class:`CapabilityDenied` on failure.

    Handlers MUST call this (or the router-level equivalent) BEFORE
    executing any side effect. "Silent failure is not failure, it is
    lying" — every denial surfaces as a structured error.
    """
    if not check_capability(principal, descriptor):
        required = [list(c) for c in descriptor.capabilities_required]
        raise CapabilityDenied(
            f"tool {descriptor.name!r} requires {required!r} "
            f"(sensitivity_class={descriptor.sensitivity_class})"
        )
