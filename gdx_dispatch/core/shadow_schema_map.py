"""SS-29 slice A — declarative old→new shadow schema map.

A small, pure-Python module that loads :file:`shadow_maps.json` (alongside
this file) and exposes helpers for looking up the target v2 table, the
column-rename dictionary, and the column-transformation pipeline for a
given v1 table.

Design rules:

* **Pure & side-effect-free.** Loading the map is idempotent and does not
  touch the database. The map is cached at module load time but
  :func:`reload_map` is available for tests.
* **No implicit surprises.** Unknown tables raise ``KeyError``; unknown
  transform names raise ``ValueError``. This mirrors SS-28's
  fail-closed philosophy — a typo in the map file must break a test,
  never silently drop data.
* **JSON over Python config.** Keeps the map editable by ops without a
  code deploy and makes it cheap to diff/audit.

The mapping schema::

    {
      "$schema_version": "1",
      "maps": {
        "<old_table>": {
          "new_table": "<new_table>",
          "column_renames": {"<old_col>": "<new_col>", ...},
          "column_transformations": [
            {"from": "<old_col>", "to": "<new_col>", "transform": "<name>"}
          ],
          "primary_key": "<old_pk_col>"
        },
        ...
      }
    }

INTEGRATION_TODO: real v2 tables do not yet exist in the main schema —
the ShadowWriter (slice B) reads the map for table/column names but its
"write" path is a no-op at the ORM level when the target table is not
present. That keeps SS-29 mergeable before the v2 model landing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

# Supported column transforms. Each callable takes a single value and
# returns the transformed value; unknown transforms raise ValueError.
_TRANSFORMS: dict[str, Callable[[Any], Any]] = {
    "identity": lambda v: v,
    "upper": lambda v: v.upper() if isinstance(v, str) else v,
    "lower": lambda v: v.lower() if isinstance(v, str) else v,
    "int_to_str": lambda v: str(v) if v is not None else None,
    "str_to_int": lambda v: int(v) if v is not None and v != "" else None,
    "json_dumps": lambda v: json.dumps(v) if v is not None else None,
    "json_loads": lambda v: json.loads(v) if isinstance(v, str) and v else v,
}


@dataclass(frozen=True)
class ShadowMap:
    """Per-table shadow mapping record."""

    old_table: str
    new_table: str
    column_renames: Mapping[str, str]
    column_transformations: tuple[dict[str, str], ...]
    primary_key: str

    def transform_row(self, row: Mapping[str, Any]) -> dict[str, Any]:
        """Return a dict shaped for INSERT into the new v2 table.

        * Applies column_renames first so downstream transforms operate
          on final-name columns.
        * Applies each transformation in declared order. If the ``to``
          column is not already present, the transform also reads from
          ``from`` (pre-rename) to support type-conversion-only cases.
        * Rows are shallow-copied — this function never mutates the caller's
          dict.
        """
        out: dict[str, Any] = {}
        for k, v in row.items():
            new_key = self.column_renames.get(k, k)
            out[new_key] = v

        for tr in self.column_transformations:
            name = tr.get("transform", "identity")
            fn = _TRANSFORMS.get(name)
            if fn is None:
                raise ValueError(
                    f"shadow_schema_map: unknown transform {name!r} "
                    f"for {self.old_table}.{tr.get('from')}"
                )
            src = tr.get("from")
            dst = tr.get("to", src)
            # Resolve source value: prefer post-rename dst, then fall back
            # to pre-rename src (from the original row).
            if dst in out:
                out[dst] = fn(out[dst])
            elif src in row:
                out[dst] = fn(row[src])
            else:
                # Missing column is not fatal — absence shadows as absence.
                continue
        return out


def _map_path() -> Path:
    return Path(__file__).with_name("shadow_maps.json")


_CACHE: dict[str, ShadowMap] | None = None


def reload_map(path: Path | None = None) -> dict[str, ShadowMap]:
    """Force-reload the map from disk. Returns the new cache dict."""
    global _CACHE
    src = path or _map_path()
    raw = json.loads(src.read_text(encoding="utf-8"))
    maps = raw.get("maps", {}) or {}

    out: dict[str, ShadowMap] = {}
    for old_table, spec in maps.items():
        if not isinstance(spec, dict):
            raise ValueError(
                f"shadow_schema_map: entry for {old_table!r} must be an object"
            )
        new_table = spec.get("new_table")
        if not new_table:
            raise ValueError(
                f"shadow_schema_map: {old_table!r} missing 'new_table'"
            )
        renames = dict(spec.get("column_renames") or {})
        transforms = tuple(dict(t) for t in (spec.get("column_transformations") or []))
        pk = spec.get("primary_key") or "id"
        out[old_table] = ShadowMap(
            old_table=old_table,
            new_table=new_table,
            column_renames=renames,
            column_transformations=transforms,
            primary_key=pk,
        )

    _CACHE = out
    return out


def get_map() -> dict[str, ShadowMap]:
    """Return the cached mapping, loading it on first call."""
    if _CACHE is None:
        return reload_map()
    return _CACHE


def shadow_for(old_table: str) -> ShadowMap:
    """Return the ShadowMap for ``old_table`` or raise KeyError."""
    m = get_map()
    if old_table not in m:
        raise KeyError(f"shadow_schema_map: no mapping for {old_table!r}")
    return m[old_table]


def is_shadowed(old_table: str) -> bool:
    """True if ``old_table`` has an entry in the map."""
    return old_table in get_map()


def supported_transforms() -> tuple[str, ...]:
    """Return the names of supported transforms (for docs/tests)."""
    return tuple(sorted(_TRANSFORMS))
