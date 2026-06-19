"""SS-35 slice B — declarative PII field catalog.

Registers every known PII column across the GDX schema. This module is
*the* canonical list — when you add a column that holds personal data,
you add it here. Model files stay clean; the privacy surface is auditable
in one place.

Usage
-----

Call :func:`register_all` once at app startup (e.g. from ``main.py`` —
tracked in TODO) to populate :mod:`gdx_dispatch.core.pii_registry`.
In tests, call :func:`register_all` explicitly in fixtures so the
registry is deterministic.

TODO: not yet wired into ``gdx_dispatch/main.py`` app startup.

Categories reference
--------------------

- ``contact`` — email, phone, messaging IDs
- ``identity`` — legal name, DOB, SSN (never stored), display_name
- ``financial`` — payment method masks, tax IDs, invoice recipient
- ``health`` — none currently; placeholder for regulated-industry phase
- ``location`` — addresses, GPS, service locations
- ``behavioral`` — activity logs, preferences, scoring signals
- ``technical`` — IP, user-agent, device IDs

Retention defaults: ``None`` = "forever unless erased". Financial rows
use retention ``None`` + ``scrub_strategy="skip"`` because ledgers are
immutable (GDPR Art. 17(3)(e) — legal-obligation exception).

Every entry in this file must have a reason to live. If you remove or
change one, ensure the corresponding SS-28 audit event fires for the
schema change (``gdx.custom_field_schema.changed.v1`` is a reasonable
kin but we emit our own :mod:`erasure`/``sar`` events).
"""
from __future__ import annotations

from gdx_dispatch.core.pii_registry import register_pii_field


def register_all() -> None:
    """Register every known PII field. Idempotent — later calls
    overwrite earlier ones with identical values, so calling twice is a
    no-op from an observable-behaviour standpoint.
    """
    # ─── identities (the identity-root table) ──────────────────
    # On this table the PK ``id`` IS the identity reference — the
    # registry's default for ``identity_fk_column=None`` on
    # ``table=="identities"`` is ``"id"``.
    register_pii_field(
        "identities", "email", "contact",
        notes="login identifier; unique per tenant",
    )
    register_pii_field(
        "identities", "phone", "contact",
        notes="SMS channel; nullable",
    )
    register_pii_field(
        "identities", "legal_name", "identity",
        notes="KYC / invoice name",
    )

    # ─── memberships (one identity × tenant) ───────────────────
    register_pii_field(
        "memberships", "display_name", "identity",
        identity_fk_column="identity_id",
        notes="per-tenant chosen display name",
    )
    register_pii_field(
        "memberships", "phone_override", "contact",
        identity_fk_column="identity_id",
        notes="override of identity phone for this tenant",
    )

    # ─── addresses (N per identity) ────────────────────────────
    for col in ("street1", "street2", "city", "region", "postal_code", "country"):
        register_pii_field(
            "addresses", col, "location",
            identity_fk_column="identity_id",
            notes="service/billing address component",
        )

    # ─── payment_methods (financial — ledger exception) ────────
    for col in ("card_last4_masked", "card_brand", "cardholder_name_masked",
                "bank_last4_masked"):
        register_pii_field(
            "payment_methods", col, "financial",
            identity_fk_column="identity_id",
            scrub_strategy="skip",
            notes="masked already; ledger immutable",
        )

    # ─── device_bindings (technical) ───────────────────────────
    register_pii_field(
        "device_bindings", "device_id", "technical",
        identity_fk_column="identity_id",
        retention_days=365,
        notes="hashed device fingerprint",
    )
    register_pii_field(
        "device_bindings", "user_agent", "technical",
        identity_fk_column="identity_id",
        retention_days=365,
    )
    register_pii_field(
        "device_bindings", "last_ip", "technical",
        identity_fk_column="identity_id",
        retention_days=90,
        notes="short retention — network location",
    )

    # ─── user_preferences (behavioral) ─────────────────────────
    register_pii_field(
        "user_preferences", "preferences_blob", "behavioral",
        identity_fk_column="identity_id",
        notes="opt-ins, UI prefs, language",
    )

    # ─── activity_log (behavioral) ─────────────────────────────
    register_pii_field(
        "activity_log", "action", "behavioral",
        identity_fk_column="actor_identity_id",
        retention_days=730,
        notes="user-triggered action stream",
    )
