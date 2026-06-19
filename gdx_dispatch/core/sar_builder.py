"""SS-35 slice C — Subject Access Request (SAR) export builder.

Walks :mod:`gdx_dispatch.core.pii_registry` for every PII field that references
an identity and formats the result into a GDPR §15-compliant JSON
document. The output is the payload the requesting identity will
download from the signed URL issued by :mod:`gdx_dispatch.routers.sar`.

Rules enforced here
-------------------

1. Every registered category MUST appear in the output (even if empty).
   Missing categories = incomplete SAR and fails a contract test.
2. Every field MUST carry provenance: ``table``, ``column``,
   ``retention_days``, ``pii_category``. A SAR export without provenance
   is not actionable.
3. The top-level envelope carries the ``generated_at`` timestamp, the
   identity id, the registry version hash, and a link back to the
   privacy policy (caller-supplied).

This module does NOT issue signed URLs — that's the router's job. It
does NOT write anywhere — the output is pure data, and the caller
decides where to persist.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from gdx_dispatch.core.pii_registry import VALID_CATEGORIES, get_pii_for_identity, list_pii_fields


GDPR_SECTION = "GDPR Art. 15"
SCHEMA_VERSION = "gdx_dispatch.sar.v1"


def _registry_fingerprint() -> str:
    """Stable hash of the registry's current declarations.

    Included in every SAR export so an auditor can tell whether the
    registry changed between two SARs on the same identity.
    """
    recs = list_pii_fields()
    payload = json.dumps(
        [
            {
                "t": r.table,
                "c": r.column,
                "cat": r.pii_category,
                "ret": r.retention_days,
            }
            for r in recs
        ],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_sar_export(
    db: Any,
    identity_id: str,
    *,
    privacy_policy_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a GDPR §15 SAR export for ``identity_id``.

    Returns a dict with the following shape::

        {
          "schema": "gdx_dispatch.sar.v1",
          "gdpr_basis": "GDPR Art. 15",
          "identity_id": "...",
          "generated_at": "2026-...",
          "registry_fingerprint": "abcd...",
          "privacy_policy_url": "https://.../privacy",
          "categories": {
              "contact":    [ { table, column, value, retention_days, row_id }, ... ],
              "identity":   [ ... ],
              "financial":  [ ... ],
              ...
          },
          "field_count": 42,
          "category_count": 7
        }

    Every registered ``VALID_CATEGORIES`` is present as a key, even if
    the list is empty — missing categories means the export is
    incomplete.
    """
    raw = get_pii_for_identity(db, identity_id)

    # Seed every category with an empty list so the envelope always
    # reflects the full registered surface.
    categories: Dict[str, List[Dict[str, Any]]] = {c: [] for c in VALID_CATEGORIES}
    for rec in raw:
        cat = rec["pii_category"]
        # Defensive — registry layer already validates category, but if
        # someone registers a new category and forgets VALID_CATEGORIES
        # we'd rather surface the data than silently drop it.
        categories.setdefault(cat, []).append({
            "table": rec["table"],
            "column": rec["column"],
            "value": rec["value"],
            "retention_days": rec["retention_days"],
            "row_id": rec.get("row_id"),
            "provenance": {
                "source_table": rec["table"],
                "source_column": rec["column"],
                "pii_category": cat,
                "retention_days": rec["retention_days"],
            },
        })

    return {
        "schema": SCHEMA_VERSION,
        "gdpr_basis": GDPR_SECTION,
        "identity_id": str(identity_id),
        "generated_at": _utcnow_iso(),
        "registry_fingerprint": _registry_fingerprint(),
        "privacy_policy_url": privacy_policy_url,
        "categories": categories,
        "field_count": len(raw),
        "category_count": len(VALID_CATEGORIES),
    }
