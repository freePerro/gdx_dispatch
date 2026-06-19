"""SS-31 platform additions stub — federation tables.

Isolation rule (matches SS-15 / SS-20 / SS-21 / SS-22 precedent): any
column / table additions required by SS-31 land HERE, not in
``gdx_dispatch/models/platform.py`` directly. Integration merges this into the
base platform in one conscious edit.

Tables declared at the ``column_specs`` level (NOT registered against
SQLAlchemy's metadata yet — the migration creates them, and the
integration step either promotes them to real mappers in
``platform.py`` OR the router keeps using raw SQL against them).

Tables this sprint introduces:

  1. ``federation_provider``
      - id (UUID pk)
      - tenant_id (string, indexed)
      - kind (enum: 'oidc' | 'saml')
      - display_name (string)
      - metadata_url (string)
      - client_id (string, nullable)
      - client_secret_encrypted (text, nullable)
          INTEGRATION_TODO: must flow through gdx_dispatch.core.pii.EncryptedString
          or a dedicated secret helper. See gdx_dispatch/routers/federation.py
          set_secret_encoder().
      - trust_bundle_ref (string, nullable)
      - redirect_uri / sp_entity_id / acs_url / scope (strings, nullable)
      - created_at (timestamp)
      - deleted_at (timestamp, nullable)

  2. ``federation_link``
      - identity_id (UUID fk → identities.id)
      - provider_id (UUID fk → federation_provider.id)
      - external_subject (string, indexed)
      - linked_at (timestamp)
      - last_login_at (timestamp, nullable)
      - revoked_at (timestamp, nullable)
      - UNIQUE (provider_id, external_subject)

  3. ``federation_trust_bundle_cache``
      - provider_id (UUID pk)
      - bundle_json (text)      — serialized TrustBundle.raw + resolved
                                    endpoints / jwks / certs
      - fetched_at (timestamp)
      - ttl_seconds (integer)
      - last_refresh_error (text, nullable)

The in-memory router stores (gdx_dispatch/routers/federation.py) cover exactly
these fields 1:1 so the DB-backed swap is a store-class replacement,
not a schema-shape change.
"""
from __future__ import annotations

# Column specs — intentionally plain dicts so nothing here touches
# SQLAlchemy metadata. The integration step is a conscious copy into
# platform.py + a rename of the migration into the main chain.

SS31_FEDERATION_PROVIDER_COLUMNS: list[tuple[str, str]] = [
    ("id", "UUID, pk, default=uuid4"),
    ("tenant_id", "String(64), NOT NULL, indexed"),
    ("kind", "String(16), NOT NULL"),
    ("display_name", "String(255), NOT NULL"),
    ("metadata_url", "String(1024), NOT NULL"),
    ("client_id", "String(255), nullable"),
    ("client_secret_encrypted", "Text, nullable"),
    ("trust_bundle_ref", "String(255), nullable"),
    ("redirect_uri", "String(1024), nullable"),
    ("sp_entity_id", "String(255), nullable"),
    ("acs_url", "String(1024), nullable"),
    ("scope", "String(255), nullable, default='openid email profile'"),
    ("created_at", "TIMESTAMPTZ, NOT NULL, default=now"),
    ("deleted_at", "TIMESTAMPTZ, nullable"),
]

SS31_FEDERATION_LINK_COLUMNS: list[tuple[str, str]] = [
    ("id", "UUID, pk, default=uuid4"),
    ("identity_id", "UUID, NOT NULL, FK->identities.id, indexed"),
    ("provider_id", "UUID, NOT NULL, FK->federation_provider.id, indexed"),
    ("external_subject", "String(255), NOT NULL, indexed"),
    ("linked_at", "TIMESTAMPTZ, NOT NULL, default=now"),
    ("last_login_at", "TIMESTAMPTZ, nullable"),
    ("revoked_at", "TIMESTAMPTZ, nullable"),
    # UNIQUE(provider_id, external_subject)
]

SS31_FEDERATION_TRUST_BUNDLE_CACHE_COLUMNS: list[tuple[str, str]] = [
    ("provider_id", "UUID, pk"),
    ("bundle_json", "Text, NOT NULL"),
    ("fetched_at", "TIMESTAMPTZ, NOT NULL"),
    ("ttl_seconds", "Integer, NOT NULL, default=3600"),
    ("last_refresh_error", "Text, nullable"),
]

# INTEGRATION TODO:
#   * Merge the three tables above into platform.py as real mappers.
#   * Promote client_secret_encrypted to EncryptedString.
#   * Add CASCADE rules on (identity_id, provider_id) consistent with
#     the existing IdentityProvider pattern.
#   * Re-chain migration TODO_ss31_federation_XXXX.py after the live
#     alembic head (currently 068) once SS-30 cutover lands.
