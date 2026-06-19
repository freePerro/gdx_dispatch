from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

# Canonical model lives in the router — single source of truth
from gdx_dispatch.routers.custom_fields import CustomFieldDefinition  # noqa: F401

ALLOWED_VALUE_TYPES = {"string", "number", "boolean", "date"}

# Map router field_type values to the legacy type system used by _type_ok
_FIELD_TYPE_TO_LEGACY = {
    "text": "string",
    "number": "number",
    "boolean": "boolean",
    "date": "date",
    "select": "string",
}


def _type_ok(v: object, t: str) -> bool:
    if v is None: return True  # noqa: E701,E702
    if t == "string": return isinstance(v, str)  # noqa: E701,E702
    if t == "number": return isinstance(v, (int, float)) and not isinstance(v, bool)  # noqa: E701,E702
    if t == "boolean": return isinstance(v, bool)  # noqa: E701,E702
    if t == "date":
        if isinstance(v, date): return True  # noqa: E701,E702
        if isinstance(v, str):
            try:
                date.fromisoformat(v)
                return True
            except ValueError:  # Validation failure is expected and handled by returning False.
                logging.getLogger(__name__).exception("date validator caught ValueError")
                return False
    return False


def validate_custom_fields(fields: dict, entity_type: str, db: Session) -> dict:
    defs = db.execute(select(CustomFieldDefinition).where(CustomFieldDefinition.entity_type == entity_type)).scalars().all()
    by_key = {d.field_key: d for d in defs}
    for key in fields:
        if not str(key).startswith("cx_"): raise ValueError(f"Invalid custom field key '{key}'")  # noqa: E701,E702
    for d in defs:
        if d.required and d.field_key not in fields: raise ValueError(f"Missing required custom field '{d.field_key}'")  # noqa: E701,E702
    for key, value in fields.items():
        d = by_key.get(key)
        if d is None: raise ValueError(f"Unknown custom field '{key}' for entity '{entity_type}'")  # noqa: E701,E702
        legacy_type = _FIELD_TYPE_TO_LEGACY.get(d.field_type, "string")
        if not _type_ok(value, legacy_type): raise ValueError(f"Invalid value type for '{key}', expected {d.field_type}")  # noqa: E701,E702
    return fields
