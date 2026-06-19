"""SS-22 platform additions stub.

Isolation rule from the SS-22 task brief: any column additions required
by the SCIM 2.0 endpoints MUST land here (NOT in ``gdx_dispatch/models/platform.py``
directly) so the integration step is a single conscious merge, not a
surprise diff on the base platform.

Status for slice F: **no new columns required**.

Rationale:
  * ``provider_subject`` already lives on ``IdentityProvider`` and is the
    load-bearing external handle exposed as SCIM ``userName``.
  * ``externalId`` (RFC 7643 optional) can be stashed in
    ``IdentityProvider.provider_metadata`` (JSON) under the ``external_id``
    key without a schema change. The SCIM router does not yet need to
    round-trip this field — added as a TODO below.
  * Soft-delete uses the existing ``Identity.status`` + ``deleted_at``
    columns; no new column is needed for the SS-22 deprovision path.

If a future sub-slice decides to denormalise ``external_id`` onto
``Identity`` (e.g. for filter-by-externalId performance), declare the
``sa.Column`` here and add a migration named ``TODO_ss22_scim_XXXX.py``
with ``down_revision = "TODO"`` — the integration merge will
re-chain it to the live revision.

TODO:
  * If added, merge the column list below into the ``Identity`` class in
    ``gdx_dispatch/models/platform.py`` via a single conscious edit.
  * Do NOT import this module from ``platform.py`` — that would couple
    the base platform to SS-22.
"""
from __future__ import annotations

# Column specs reserved for future denormalisation. Kept as a plain list
# of tuples so that nothing in this file actually registers against
# SQLAlchemy's metadata — the presence of a mapper here would violate
# the isolation rule.
SS22_IDENTITY_COLUMN_ADDITIONS: list[tuple[str, str]] = [
    # ("external_id", "String(255), nullable=True, indexed"),
    # ("provider_subject", "String(255), nullable=True, indexed"),
]
