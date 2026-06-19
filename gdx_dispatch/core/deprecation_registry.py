"""SS-25 Slice C — JSON-backed deprecation registry.

This module owns the authoritative mapping from endpoint path to its
deprecation metadata. It is read by:

* :mod:`gdx_dispatch.core.middleware.api_versioning` — to decide whether to
  inject ``Sunset`` / ``Deprecation`` response headers on the fly.
* :mod:`gdx_dispatch.routers.api_metadata` — to expose the full registry via
  ``GET /api/meta/deprecations`` so consumers can audit/watch.

**Source of truth** is the JSON file at
``gdx_dispatch/core/deprecations.json``. We keep it JSON (not Python) so a
supervisor/operator can edit entries without shipping a Python change,
and so tests can seed ad-hoc registries via
:func:`DeprecationRegistry.from_entries`.

Schema per entry::

    {
        "endpoint": "/api/v1/customers",          # exact path match
        "deprecated_at": "2026-04-01T00:00:00Z",  # ISO 8601, UTC
        "sunset_at":     "2028-04-01T00:00:00Z",  # ISO 8601, UTC
        "replacement_endpoint": "/api/v2/customers"  # optional, nullable
    }

``deprecated_at`` must be <= ``sunset_at``. Missing timestamps are
rejected at load time — a deprecation without a sunset window is an
open-ended threat and violates the 24-month policy commitment.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_PATH = Path(__file__).with_name("deprecations.json")


@dataclass(frozen=True)
class DeprecationEntry:
    """One deprecation record."""

    endpoint: str
    deprecated_at: datetime
    sunset_at: datetime
    replacement_endpoint: str | None = None

    def __post_init__(self) -> None:
        if self.deprecated_at.tzinfo is None or self.sunset_at.tzinfo is None:
            raise ValueError(
                f"deprecation timestamps must be timezone-aware (endpoint={self.endpoint})"
            )
        if self.deprecated_at > self.sunset_at:
            raise ValueError(
                f"deprecated_at is after sunset_at for endpoint={self.endpoint}"
            )

    def to_public_dict(self) -> dict[str, str | None]:
        """Serialisable form for ``/api/meta/deprecations`` response."""
        return {
            "endpoint": self.endpoint,
            "deprecated_at": self.deprecated_at.isoformat(),
            "sunset_at": self.sunset_at.isoformat(),
            "replacement_endpoint": self.replacement_endpoint,
        }


class DeprecationRegistry:
    """In-memory index of :class:`DeprecationEntry` keyed by endpoint path.

    Thread-safe: reads take a lock but the dict copy-on-write keeps the
    hot path cheap. Intended to be instantiated once per process; the
    module-level :func:`get_registry` returns the default singleton
    loaded from :data:`DEFAULT_REGISTRY_PATH`.
    """

    def __init__(self, entries: Iterable[DeprecationEntry] = ()) -> None:
        self._lock = threading.RLock()
        self._by_path: dict[str, DeprecationEntry] = {}
        for entry in entries:
            self._by_path[entry.endpoint] = entry

    # ── lookups ────────────────────────────────────────────────────────────

    def lookup(self, path: str) -> DeprecationEntry | None:
        """Exact-match lookup. Returns ``None`` if not deprecated."""
        with self._lock:
            return self._by_path.get(path)

    def all_entries(self) -> list[DeprecationEntry]:
        """Snapshot of every registered deprecation."""
        with self._lock:
            return list(self._by_path.values())

    def __contains__(self, path: object) -> bool:
        if not isinstance(path, str):
            return False
        return self.lookup(path) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._by_path)

    # ── factories ──────────────────────────────────────────────────────────

    @classmethod
    def from_entries(cls, raw: Iterable[Mapping[str, object]]) -> "DeprecationRegistry":
        """Build from an iterable of raw dicts (e.g. loaded from JSON)."""
        parsed: list[DeprecationEntry] = []
        for row in raw:
            parsed.append(_parse_entry(row))
        return cls(parsed)

    @classmethod
    def from_json_file(cls, path: Path | str) -> "DeprecationRegistry":
        """Load from a JSON file. Missing file → empty registry (not an error).

        An empty or missing registry is the normal steady state for a
        fresh environment; absence is not a failure. A *malformed* file
        is — we let ``json.JSONDecodeError`` bubble up so operators see
        the real mistake instead of silently shipping no deprecations.
        """
        p = Path(path)
        if not p.exists():
            logger.info("deprecation_registry_missing_file", extra={"path": str(p)})
            return cls([])
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        raw_entries = data.get("deprecations", []) if isinstance(data, dict) else []
        return cls.from_entries(raw_entries)


def _parse_entry(row: Mapping[str, object]) -> DeprecationEntry:
    try:
        endpoint = str(row["endpoint"])
        deprecated_at = _parse_iso_utc(str(row["deprecated_at"]))
        sunset_at = _parse_iso_utc(str(row["sunset_at"]))
    except KeyError as exc:
        raise ValueError(f"deprecation entry missing required field: {exc}") from exc
    replacement = row.get("replacement_endpoint")
    replacement_str = str(replacement) if replacement else None
    return DeprecationEntry(
        endpoint=endpoint,
        deprecated_at=deprecated_at,
        sunset_at=sunset_at,
        replacement_endpoint=replacement_str,
    )


def _parse_iso_utc(raw: str) -> datetime:
    # Support trailing ``Z`` which fromisoformat doesn't accept <3.11 in
    # all cases. Normalise to ``+00:00``.
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        # Treat naive inputs as UTC. The JSON source is operator-edited;
        # we prefer tolerance here over a cryptic load-time crash.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ── module-level singleton ────────────────────────────────────────────────

_default_registry: DeprecationRegistry | None = None
_default_lock = threading.Lock()


def get_registry() -> DeprecationRegistry:
    """Return the default singleton registry, loading on first access."""
    global _default_registry
    if _default_registry is None:
        with _default_lock:
            if _default_registry is None:
                _default_registry = DeprecationRegistry.from_json_file(
                    DEFAULT_REGISTRY_PATH
                )
    return _default_registry


def reset_registry_for_tests() -> None:
    """Drop the cached singleton. Call this in fixtures, not production."""
    global _default_registry
    with _default_lock:
        _default_registry = None
