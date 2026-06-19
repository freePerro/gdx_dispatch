"""SS-15 platform additions — INTEGRATED 2026-04-20.

Historical marker kept as a record of what Sprint 0.9-a merged. No
runtime use; no imports from this module. The specs below describe
what are now real columns on ``AccessToken`` in
``gdx_dispatch/models/platform_extensions.py`` (``status`` + ``metadata_json``)
and the in-memory ``_PAT_STATE`` shim + ``hasattr`` guards in
``gdx_dispatch/routers/admin_pats.py`` were dropped in Sprint 0.9-f
(commit ``07da4f74``).

Safe to delete once no one needs the historical integration trail.
"""
from __future__ import annotations

# Plain tuples, deliberately not bound to a SQLAlchemy mapper so the
# presence of this module does NOT mutate the ControlBase metadata graph.
# Integration will translate these into real Column(...) declarations on
# AccessToken.

SS15_ACCESS_TOKEN_COLUMN_ADDITIONS: list[tuple[str, str]] = [
    (
        "status",
        "String(32), nullable=False, server_default=sa.text(\"'active'\")",
    ),
    (
        "metadata_json",
        "JSON, nullable=True",
    ),
]
