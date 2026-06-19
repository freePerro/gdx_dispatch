"""SS-33 Slice B: resource type loader.

Discovers types from two sources at app start:

1. **Filesystem** — every ``gdx_dispatch/core/resource_types/*.json`` file is a
   platform-wide (``owner_tenant_id=None``) JSON Schema. The schema's
   ``title`` wins; filename stem is the fallback. Schema extensions:

       * ``x-gdx-capabilities`` → capabilities list (tuple-shaped)
       * ``x-gdx-index-hints``  → list[str] of hinted indexable fields

2. **Database** — if a SQLAlchemy ``Session`` is supplied, rows from the
   ``resource_type`` table (SS-33 models stub) are loaded as
   tenant-private types. DB failures log + skip; they never block import.

Public API:
    * :func:`load_builtin_types()` — filesystem pass, idempotent
    * :func:`load_tenant_types_from_db(session)` — DB pass, idempotent
    * :func:`bootstrap(session=None)` — both passes in order

Integration TODO:
    * ``gdx_dispatch/main.py`` startup hook should call ``bootstrap(session)``
      after DB init. Not wired here; router mount + bootstrap are
      SS-33 integration concerns.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from gdx_dispatch.core import resource_type_registry as rtr

logger = logging.getLogger(__name__)

BUILTIN_DIR = Path(__file__).parent / "resource_types"


def _extract_capabilities(schema: dict[str, Any]) -> list[tuple[str, str]]:
    raw = schema.get("x-gdx-capabilities", [])
    out: list[tuple[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                out.append((str(item[0]), str(item[1])))
    return out


def _extract_index_hints(schema: dict[str, Any]) -> list[str]:
    raw = schema.get("x-gdx-index-hints", [])
    if isinstance(raw, list):
        return [str(x) for x in raw]
    return []


def load_builtin_types() -> list[str]:
    """Discover every ``resource_types/*.json`` and register it.

    Returns list of registered names. Idempotent: re-registering an
    identical descriptor is a no-op; mutated descriptors overwrite
    via ``replace=True`` so the filesystem is always the source of
    truth for platform-wide types at app start.
    """
    if not BUILTIN_DIR.is_dir():
        logger.debug("resource_type_loader: no builtin dir at %s", BUILTIN_DIR)
        return []
    names: list[str] = []
    for path in sorted(BUILTIN_DIR.glob("*.json")):
        # Narrow to the specific shape of failures that mean "this ONE
        # file is bad" so one malformed file doesn't block startup for
        # all builtin types. Anything broader (MemoryError, SystemExit,
        # etc.) should propagate — it is not a per-file problem.
        try:
            schema = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.error(
                "resource_type_loader.builtin_read_failed",
                extra={
                    "op": "read_builtin",
                    "path": str(path),
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )
            continue
        name = schema.get("title") or path.stem
        caps = _extract_capabilities(schema)
        hints = _extract_index_hints(schema)
        description = schema.get("description", "")
        try:
            rtr.register_type(
                name,
                schema,
                caps,
                owner_tenant_id=None,
                description=description,
                index_hints=hints,
                replace=True,
            )
            names.append(name)
        except rtr.ResourceTypeError as exc:
            logger.error(
                "resource_type_loader.builtin_register_failed",
                extra={
                    "op": "register_builtin",
                    "path": str(path),
                    "name": name,
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )
    return names


def load_tenant_types_from_db(session: Any) -> list[str]:
    """Load tenant-private types from the ``resource_type`` DB table.

    Best-effort: missing table / driver errors log + return []. Never
    raises. Rows must carry ``name``, ``json_schema`` (JSON),
    ``capabilities`` (JSON list of [action, resource_type]),
    ``owner_tenant_id``, ``description``.
    """
    from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError

    names: list[str] = []
    # Narrow to SQLAlchemyError family — the "table doesn't exist yet"
    # case is the point of this try. Anything outside that family
    # (ImportError on sqlalchemy itself, KeyboardInterrupt, etc.) must
    # propagate — it's not a DB-schema problem.
    try:
        from sqlalchemy import text

        result = session.execute(
            text(
                "SELECT name, json_schema, capabilities, owner_tenant_id, "
                "description, index_hints FROM resource_type"
            )
        )
        rows = list(result)
    except (OperationalError, ProgrammingError) as exc:
        # Expected pre-migration shape: table not yet created. Debug
        # level — this is normal on fresh tenants.
        logger.debug(
            "resource_type_loader: resource_type table unavailable — %s",
            type(exc).__name__,
        )
        return []
    except SQLAlchemyError as exc:
        # Any other SQLA error is a real DB problem — log loudly.
        logger.error(
            "resource_type_loader.db_query_failed",
            extra={
                "op": "load_tenant_types",
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
        return []

    for row in rows:
        # Per-row: narrow to the shapes that mean "this ONE row is
        # malformed" — we want to skip it but keep loading the rest.
        try:
            name = row[0]
            schema = row[1] if isinstance(row[1], dict) else json.loads(row[1] or "{}")
            caps_raw = row[2] if isinstance(row[2], list) else json.loads(row[2] or "[]")
            owner = row[3]
            description = row[4] or ""
            hints_raw = row[5] if isinstance(row[5], list) else json.loads(row[5] or "[]")
            caps = [
                (str(c[0]), str(c[1]))
                for c in caps_raw
                if isinstance(c, (list, tuple)) and len(c) == 2
            ]
            rtr.register_type(
                name,
                schema,
                caps,
                owner_tenant_id=owner,
                description=description,
                index_hints=[str(h) for h in hints_raw],
                replace=True,
            )
            names.append(name)
        except (
            json.JSONDecodeError,
            TypeError,
            IndexError,
            rtr.ResourceTypeError,
        ) as exc:
            logger.error(
                "resource_type_loader.db_row_invalid",
                extra={
                    "op": "register_tenant_type",
                    "row": repr(row),
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )
    return names


def bootstrap(session: Any | None = None) -> dict[str, list[str]]:
    """Run both passes. Returns ``{"builtin": [...], "tenant": [...]}``."""
    builtin = load_builtin_types()
    tenant: list[str] = []
    if session is not None:
        tenant = load_tenant_types_from_db(session)
    return {"builtin": builtin, "tenant": tenant}
