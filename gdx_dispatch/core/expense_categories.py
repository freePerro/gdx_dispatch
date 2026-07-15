"""Expense category vocabulary — ONE source of truth (GL S8 audit round 4).

The backend validated one list while the shipped frontend hardcoded another
(materials/travel/meals/…) and never fetched /api/expense-categories — zero
overlap, so strict validation would have bricked the Expenses page and every
historical row would post to 6900 on backfill. This module owns the
canonical list, the legacy synonym map, and the normalizer used by BOTH the
API boundary and the posting rules (so legacy prod data resolves to real
accounts at posting/backfill time without a data rewrite).
"""
from __future__ import annotations

EXPENSE_CATEGORIES: list[str] = [
    "Fuel",
    "Parts/Supplies",
    "Tools/Equipment",
    "Advertising",
    "Insurance",
    "Vehicle Maintenance",
    "Subcontractor",
    "Other",
]

_CANONICAL_BY_LOWER = {c.lower(): c for c in EXPENSE_CATEGORIES}

# Legacy vocabulary (the pre-S8 frontend + historical rows) → canonical.
LEGACY_EXPENSE_CATEGORY_MAP: dict[str, str] = {
    "materials": "Parts/Supplies",
    "supplies": "Parts/Supplies",
    "parts": "Parts/Supplies",
    "equipment": "Tools/Equipment",
    "tools": "Tools/Equipment",
    "travel": "Other",
    "meals": "Other",
    "fuel": "Fuel",
    "gas": "Fuel",
    "vehicle": "Vehicle Maintenance",
    "sub": "Subcontractor",
    "subcontractors": "Subcontractor",
}


def canonicalize_expense_category(raw: str | None) -> str | None:
    """Canonical category for ``raw`` (case-insensitive, legacy synonyms
    mapped), or None when it can't be resolved — the caller decides whether
    that's a 422 (API input) or an EXPENSE_FALLBACK post (historical data).
    """
    key = (raw or "").strip().lower()
    if not key:
        return None
    if key in _CANONICAL_BY_LOWER:
        return _CANONICAL_BY_LOWER[key]
    return LEGACY_EXPENSE_CATEGORY_MAP.get(key)
