"""SS-32 slice D — workload capability map.

A declarative mapping of SPIFFE-ID glob patterns to platform
capabilities. Loaded from :file:`workload_caps.json` at import / first
use, with an optional overlay from a DB table (``spiffe_workload_registration``
— see models stub).

Glob semantics (authoritative):

* Globs apply to the full SPIFFE URI (``spiffe://<td>/<path>``).
* ``*`` in a path segment matches the remainder of that single segment
  (standard fnmatch within-segment behavior, no ``/`` crossing).
* ``**`` as a whole segment matches one or more path segments (cannot
  match zero — ``/agent/**`` requires at least one segment after
  ``/agent``).
* The trust-domain portion is compared literally after lowercase
  normalisation; globs there are not supported (would defeat
  trust-domain-scoped CA selection).
* Deny by default: unmatched SPIFFE IDs get an empty capability set.
* If multiple entries match, capabilities are UNION-ed; ``tenant_scope``
  of "global" wins over "per-tenant" when composing (global is the
  stronger grant).

The caller is responsible for enforcing capabilities downstream — this
module only resolves.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from gdx_dispatch.core.spiffe.spiffe_id import (
    SpiffeID,
    SpiffeIdError,
    parse_spiffe_id,
)

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(__file__).parent / "workload_caps.json"


@dataclass(frozen=True)
class CapabilityGrant:
    """A single capability map entry."""

    spiffe_id_glob: str
    capabilities: tuple
    tenant_scope: str  # "global" | "per-tenant"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedCapabilities:
    """Resolution result for a given SPIFFE ID.

    ``capabilities`` is de-duplicated and sorted; ``matched_globs`` is
    the ordered list of globs that contributed — useful for the admin
    UI and for audit events.
    """

    spiffe_id: str
    capabilities: tuple
    tenant_scope: str
    matched_globs: tuple


def _glob_to_regex(glob: str) -> re.Pattern:
    """Translate a SPIFFE-ID glob to a regex.

    ``**`` matches ``.+`` (one-or-more chars, crosses ``/``); ``*``
    matches ``[^/]*`` (within a single path segment). Other characters
    are treated literally. The regex is anchored at both ends.
    """
    parts: List[str] = []
    i = 0
    while i < len(glob):
        if glob[i : i + 2] == "**":
            parts.append(".+")
            i += 2
        elif glob[i] == "*":
            parts.append("[^/]*")
            i += 1
        else:
            parts.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


class WorkloadCapabilityMap:
    """In-memory map of SPIFFE-ID globs → capability grants.

    Thread-safe; cheap to construct; intended to live on an app-level
    singleton but instances can also be built per-test. Overlays from
    DB-registered workloads are merged via :meth:`overlay`.
    """

    def __init__(self, grants: Sequence[CapabilityGrant] = ()):
        self._lock = threading.Lock()
        self._grants: List[CapabilityGrant] = list(grants)
        self._compiled: List[re.Pattern] = [
            _glob_to_regex(g.spiffe_id_glob) for g in self._grants
        ]

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkloadCapabilityMap":
        entries = data.get("entries") or []
        grants: List[CapabilityGrant] = []
        for raw in entries:
            if not isinstance(raw, dict):
                raise ValueError(f"capability entry is not an object: {raw!r}")
            glob = raw.get("spiffe_id_glob")
            if not isinstance(glob, str) or not glob:
                raise ValueError("entry missing 'spiffe_id_glob'")
            caps = raw.get("capabilities") or []
            if not isinstance(caps, list) or not all(
                isinstance(c, str) for c in caps
            ):
                raise ValueError(
                    f"entry '{glob}' capabilities must be list of str"
                )
            scope = raw.get("tenant_scope", "per-tenant")
            if scope not in ("global", "per-tenant"):
                raise ValueError(
                    f"entry '{glob}' tenant_scope must be 'global' or 'per-tenant'"
                )
            md = raw.get("metadata") or {}
            if not isinstance(md, dict):
                raise ValueError(f"entry '{glob}' metadata must be an object")
            grants.append(
                CapabilityGrant(
                    spiffe_id_glob=glob,
                    capabilities=tuple(caps),
                    tenant_scope=scope,
                    metadata=md,
                )
            )
        return cls(grants)

    @classmethod
    def from_json_file(
        cls, path: Optional[Path] = None
    ) -> "WorkloadCapabilityMap":
        target = Path(path) if path else _DEFAULT_PATH
        with target.open("r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    # ------------------------------------------------------------------
    # Query / mutate
    # ------------------------------------------------------------------

    def list_grants(self) -> List[CapabilityGrant]:
        with self._lock:
            return list(self._grants)

    def overlay(self, extra: Iterable[CapabilityGrant]) -> None:
        """Append additional grants (e.g. from DB) to the map.

        Earlier entries keep their position — this lets platform-wide
        defaults load first and tenant registrations layer on top.
        """
        with self._lock:
            for g in extra:
                self._grants.append(g)
                self._compiled.append(_glob_to_regex(g.spiffe_id_glob))

    def resolve(self, spiffe_id: str) -> ResolvedCapabilities:
        """Return the capabilities granted to ``spiffe_id``.

        Deny-by-default: unmatched IDs get ``capabilities=()`` and
        ``tenant_scope="per-tenant"`` (the safer of the two defaults).
        """
        try:
            parsed: SpiffeID = parse_spiffe_id(spiffe_id)
        except SpiffeIdError:
            return ResolvedCapabilities(
                spiffe_id=str(spiffe_id),
                capabilities=(),
                tenant_scope="per-tenant",
                matched_globs=(),
            )
        target = parsed.uri
        matched: List[str] = []
        caps: set[str] = set()
        scope = "per-tenant"
        with self._lock:
            for g, pat in zip(self._grants, self._compiled):
                if pat.match(target):
                    matched.append(g.spiffe_id_glob)
                    caps.update(g.capabilities)
                    if g.tenant_scope == "global":
                        scope = "global"
        return ResolvedCapabilities(
            spiffe_id=target,
            capabilities=tuple(sorted(caps)),
            tenant_scope=scope,
            matched_globs=tuple(matched),
        )


def resolve_capabilities(
    spiffe_id: str, *, map_: Optional[WorkloadCapabilityMap] = None
) -> ResolvedCapabilities:
    """Module-level convenience wrapper.

    Uses the default JSON-backed map when ``map_`` is not supplied.
    """
    target = map_ or _default_map()
    return target.resolve(spiffe_id)


_DEFAULT_MAP: Optional[WorkloadCapabilityMap] = None
_DEFAULT_MAP_LOCK = threading.Lock()


def _default_map() -> WorkloadCapabilityMap:
    global _DEFAULT_MAP
    with _DEFAULT_MAP_LOCK:
        if _DEFAULT_MAP is None:
            try:
                _DEFAULT_MAP = WorkloadCapabilityMap.from_json_file()
            except Exception as exc:
                logger.warning(
                    "failed to load default workload capability map: %s — "
                    "using empty deny-by-default map",
                    exc,
                )
                _DEFAULT_MAP = WorkloadCapabilityMap()
        return _DEFAULT_MAP


def reset_default_map_for_tests() -> None:
    """Test-only hook to clear the cached default map."""
    global _DEFAULT_MAP
    with _DEFAULT_MAP_LOCK:
        _DEFAULT_MAP = None
