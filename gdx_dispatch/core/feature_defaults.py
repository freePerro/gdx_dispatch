"""Feature setting catalogs — defaults + bounds, keyed by feature.

Every setting that varies legitimately between tenants ("policy choice"
settings) lives here as a default. Tenants override per-row in storage:
the tech-mobile catalog stores overrides in
``AppSettings.tenant_mobile_settings`` (a JSON dict).

The catalog is the source of truth for defaults. The storage column only
holds keys that have been explicitly overridden — reading a setting falls
back to the catalog default when the override is missing. This means a
fresh tenant has ``tenant_mobile_settings == {}`` and gets the platform
default everywhere.

Each catalog entry declares:
    type    — "bool" / "int" / "enum" / "string" / "list[string]"
    default — platform default value
    bounds  — for "int": (min, max). for "enum": tuple of allowed values.
              for other types: None.
    phase   — sprint phase the setting first appears in (informational —
              drives admin-UI grouping)
    label   — human-readable label for the admin UI

This module is greppable by design — adding a setting means editing
exactly one dict. The admin UI reads the catalog to render its form;
the backend helper validates writes against the same catalog.
"""

from __future__ import annotations

from typing import Any

# Tech-mobile settings catalog. Every key is namespaced "tech_mobile.*".
# Sprint refs map to the sprint plan (sprint_tech_mobile.md).
#
# Every key here must be read by something: the admin form offers each one as
# a live switch, and a switch nothing reads is a promise the app does not keep.
# tests/test_feature_defaults.py::TestEveryKeyHasAReader enforces it. Seventeen
# keys with no reader were deleted under GDXA-493 (two under #644 before them);
# their stored overrides are ignored on read (core/tenant_mobile_settings.py).
TECH_MOBILE_SETTINGS: dict[str, dict[str, Any]] = {
    # --- Phase 1.1 — Today's Route ---------------------------------------
    "tech_mobile.drive_time_provider": {
        "type": "enum",
        "default": "google",
        "bounds": ("google", "mapbox", "off"),
        "phase": "1.1",
        "label": "Drive-time provider",
        "help": "google = Distance Matrix (default); mapbox = planned; off = no real-time drive-time.",
    },
    "tech_mobile.drag_reorder_authority": {
        "type": "enum",
        "default": "live",
        "bounds": ("live", "dispatch_approval"),
        "phase": "1.1",
        "label": "Drag-reorder authority",
        "help": "live = tech reorders apply immediately; dispatch_approval = pending until dispatch confirms.",
    },
    # Phase 1.2's signature_required_completion and signature_surface were
    # read only by the deprecated POST /api/mobile/jobs/{id}/complete and went
    # with it (GDXA-1937). The closeout's signature gate is the workflow flag
    # require_signature_on_complete (Settings → Workflow).
    # --- Phase 1.3 — Parts ------------------------------------------------
    "tech_mobile.critical_part_audible": {
        "type": "bool",
        "default": True,
        "bounds": None,
        "phase": "1.3",
        "label": "Critical-part dispatch alert audible",
        "help": "Audible ping on dispatch screen when a critical part is flagged.",
    },
    # --- Phase 1.5 — Push Notifications ----------------------------------
    "tech_mobile.push_fallback_mode": {
        "type": "string",
        "default": "badge_only",
        "bounds": ("badge_only", "badge_plus_email", "hold_features"),
        "phase": "1.5",
        "label": "Push fallback mode",
        "help": (
            "How to handle techs who haven't granted push permission. "
            "badge_only = in-app badge counter only (default). "
            "badge_plus_email = also send the same notification by email. "
            "hold_features = don't show push-dependent features for techs "
            "without push (no parts notifications, no dispatch chat pings)."
        ),
    },
    # --- Phase 1.4 — Multi-Tech ------------------------------------------
    "tech_mobile.techs_see_all_jobs": {
        "type": "bool",
        "default": False,
        "bounds": None,
        "phase": "1.4",
        "label": "Techs can see all jobs",
        "help": (
            "When ON, the mobile Jobs tab gains an 'All jobs' scope so any "
            "tech can browse every job in the company (and open them "
            "read-only). When OFF (default), techs see only jobs assigned "
            "to them or that they created. Write actions (start, complete, "
            "clock-in) always require assignment regardless of this setting."
        ),
    },
    # --- Sprint 2 — Quoting ----------------------------------------------
    "tech_mobile.signature_required_quote": {
        "type": "enum",
        "default": "required",
        "bounds": ("required", "optional", "off"),
        "phase": "2.1",
        "label": "Customer signature required for quote acceptance",
        "help": "Whether a signature is required to accept a quote.",
    },
    "tech_mobile.estimate_validity_days": {
        "type": "int",
        "default": 30,
        "bounds": (1, 365),
        "phase": "2.1",
        "label": "Estimate validity period (days)",
        "help": "How many days an issued estimate stays valid before expiring.",
    },
    "tech_mobile.quote_decline_reasons": {
        "type": "list[string]",
        "default": [
            "Priced too high",
            "Not today",
            "Wants a second opinion",
            "Going to do it themselves",
            "Already booked another company",
            "Other",
        ],
        "bounds": None,
        "phase": "2.1",
        "label": "Quote decline reasons",
        "help": "Reason taxonomy presented when a customer declines a quote on the truck. Tenants edit to match their analytics needs.",
    },
    "tech_mobile.service_presets_override": {
        "type": "list[string]",
        "default": [],
        "bounds": None,
        "phase": "2.1",
        "label": "Service presets override (advanced)",
        "help": "Optional override of the platform default Good/Better/Best service preset catalog. Empty = use platform defaults. Each entry is a JSON-encoded service block.",
    },
    # --- Sprint 5 — GPS & Live Map (Phase 5.3) --------------------------
    "tech_mobile.gps_breadcrumb_enabled": {
        "type": "bool",
        "default": True,
        "bounds": None,
        "phase": "5.3",
        "label": "Background GPS breadcrumb",
        "help": "Sample tech location on a timer while clocked in. Stops at clock-out. Documented in privacy policy.",
    },
    "tech_mobile.gps_retention_days": {
        "type": "int",
        "default": 45,
        "bounds": (7, 365),
        "phase": "5.3",
        "label": "GPS breadcrumb retention (days)",
        "help": "Auto-delete breadcrumb rows older than this many days. Min 7, max 365.",
    },
    "tech_mobile.gps_arrival_distance_m": {
        "type": "int",
        "default": 100,
        "bounds": (10, 1000),
        "phase": "5.3",
        "label": "Auto-arrival distance threshold (meters)",
        "help": "Prompt 'Mark arrived?' when tech is within this many meters of the customer address for the configured dwell time.",
    },
    "tech_mobile.gps_arrival_dwell_seconds": {
        "type": "int",
        "default": 120,
        "bounds": (30, 600),
        "phase": "5.3",
        "label": "Auto-arrival dwell time (seconds)",
        "help": "How long the tech must remain within the arrival radius before the prompt fires.",
    },
    # --- Sprint 6 — Time / DOT -------------------------------------------
    "tech_mobile.vehicle_inspection": {
        "type": "enum",
        "default": "off",
        "bounds": ("off", "daily", "weekly"),
        "phase": "6.2",
        "label": "Vehicle inspection / fuel log",
        "help": "Cadence at which techs must complete vehicle inspection (DOT requirement for some accounts).",
    },
}


def list_tech_mobile_settings() -> list[dict[str, Any]]:
    """Return the catalog as a list ordered for stable admin UI rendering."""
    return [
        {"key": key, **meta}
        for key, meta in sorted(
            TECH_MOBILE_SETTINGS.items(),
            key=lambda kv: (kv[1].get("phase", "9"), kv[0]),
        )
    ]


def tech_mobile_default(key: str) -> Any:
    """Return the platform default for a tech-mobile setting key.

    Raises KeyError if the key isn't in the catalog — keeps callers honest;
    a typo'd key is a bug, not a silent fallback.
    """
    return TECH_MOBILE_SETTINGS[key]["default"]


def validate_tech_mobile_value(key: str, value: Any) -> Any:
    """Validate + coerce a value against the catalog spec.

    Returns the coerced value on success. Raises ``ValueError`` with a
    human-readable message on any spec violation. The admin write-path
    funnels every mutation through this so the column never holds a value
    that wouldn't survive a future read.
    """
    spec = TECH_MOBILE_SETTINGS.get(key)
    if spec is None:
        raise ValueError(f"unknown setting: {key!r}")

    typ = spec["type"]
    bounds = spec.get("bounds")

    if typ == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{key}: expected bool, got {type(value).__name__}")
        return value

    if typ == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key}: expected int, got {type(value).__name__}")
        if bounds is not None:
            lo, hi = bounds
            if not lo <= value <= hi:
                raise ValueError(f"{key}: {value} out of bounds [{lo}, {hi}]")
        return value

    if typ == "enum":
        if bounds is None or value not in bounds:
            raise ValueError(f"{key}: {value!r} not in {bounds!r}")
        return value

    if typ == "string":
        if not isinstance(value, str):
            raise ValueError(f"{key}: expected string, got {type(value).__name__}")
        return value

    if typ == "list[string]":
        if not isinstance(value, list):
            raise ValueError(f"{key}: expected list, got {type(value).__name__}")
        for i, item in enumerate(value):
            if not isinstance(item, str):
                raise ValueError(f"{key}[{i}]: expected string, got {type(item).__name__}")
        return value

    raise ValueError(f"{key}: unsupported type {typ!r} in catalog")
