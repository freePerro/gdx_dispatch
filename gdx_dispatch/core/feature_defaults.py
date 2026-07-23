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
    "tech_mobile.on_my_way_auto_fire": {
        "type": "enum",
        "default": "manual",
        "bounds": ("auto", "manual"),
        "phase": "1.1",
        "label": '"On my way" trigger',
        "help": "auto fires when state advances to en_route; manual requires the tech to tap.",
    },
    "tech_mobile.drag_reorder_authority": {
        "type": "enum",
        "default": "live",
        "bounds": ("live", "dispatch_approval"),
        "phase": "1.1",
        "label": "Drag-reorder authority",
        "help": "live = tech reorders apply immediately; dispatch_approval = pending until dispatch confirms.",
    },
    # --- Phase 1.2 — Arrival & On-Site -----------------------------------
    "tech_mobile.photo_slot_tagging": {
        "type": "enum",
        "default": "optional",
        "bounds": ("optional", "required"),
        "phase": "1.2",
        "label": "Photo slot tagging (before/during/after)",
        "help": "required = every photo must be tagged before/during/after; optional = free-form.",
    },
    "tech_mobile.signature_required_completion": {
        "type": "enum",
        "default": "required",
        "bounds": ("required", "optional", "off"),
        "phase": "1.2",
        "label": "Customer signature required for completion",
        "help": "Whether a customer signature is required to mark a job complete.",
    },
    "tech_mobile.signature_surface": {
        "type": "enum",
        "default": "phone_handoff",
        "bounds": ("phone_handoff", "customer_link"),
        "phase": "1.2",
        "label": "Signature capture surface",
        "help": "phone_handoff = customer signs on tech's phone; customer_link = SMS/email link to sign on their own device.",
    },
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
    "tech_mobile.completion_lead_tech_only": {
        "type": "bool",
        "default": False,
        "bounds": None,
        "phase": "1.4",
        "label": "Restrict job completion to lead tech",
        "help": (
            "When ON, only the designated lead tech can flip a multi-tech "
            "job to 'done'. When OFF (default), any assigned tech can. "
            "Falls back to permissive when no lead is set, so a tenant "
            "that flips this on without designating leads doesn't lock "
            "every job from completing."
        ),
    },
    "tech_mobile.multi_tech_jobs": {
        "type": "bool",
        "default": True,
        "bounds": None,
        "phase": "1.4",
        "label": "Multi-tech jobs allowed",
        "help": "Whether a single job can have multiple assigned technicians.",
    },
    "tech_mobile.lead_tech_only_completion": {
        "type": "bool",
        "default": False,
        "bounds": None,
        "phase": "1.4",
        "label": "Restrict completion to lead tech",
        "help": "When ON, only the designated lead tech can mark a job complete.",
    },
    # --- Phase 1.5 — Push -------------------------------------------------
    "tech_mobile.push_fallback_policy": {
        "type": "enum",
        "default": "badge_only",
        "bounds": ("badge_only", "badge_plus_email", "hold_features"),
        "phase": "1.5",
        "label": "Push fallback (when permission denied/unsupported)",
        "help": "badge_only = in-app badge; badge_plus_email = badge + email; hold_features = disable push-dependent features for this tenant.",
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
    "tech_mobile.quote_tax_shape": {
        "type": "enum",
        "default": "line_by_line",
        "bounds": ("line_by_line", "single_total"),
        "phase": "2.1",
        "label": "Quote tax shape",
        "help": "line_by_line = tax displayed per line; single_total = single tax line at bottom.",
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
    # --- Sprint 3 — Offline ----------------------------------------------
    "tech_mobile.offline_mode_enabled": {
        "type": "bool",
        "default": True,
        "bounds": None,
        "phase": "3.1",
        "label": "Offline mode enabled",
        "help": "When ON, mobile clients queue mutations locally and sync when connectivity returns.",
    },
    # --- Sprint 5 — GPS / History ----------------------------------------
    "tech_mobile.gps_tracking_enabled": {
        "type": "bool",
        "default": True,
        "bounds": None,
        "phase": "5.3",
        "label": "GPS tracking enabled",
        "help": "Master switch for GPS breadcrumb capture. OFF disables location capture entirely.",
    },
    "tech_mobile.gps_breadcrumb_interval_sec": {
        "type": "int",
        "default": 30,
        "bounds": (15, 120),
        "phase": "5.3",
        "label": "GPS breadcrumb interval (seconds)",
        "help": "How often the tech's device reports its position while clocked in.",
    },
    "tech_mobile.gps_retention_days": {
        "type": "int",
        "default": 45,
        "bounds": (7, 365),
        "phase": "5.3",
        "label": "GPS retention (days)",
        "help": "Auto-delete breadcrumbs older than this many days.",
    },
    "tech_mobile.auto_arrival_radius_m": {
        "type": "int",
        "default": 100,
        "bounds": (25, 500),
        "phase": "5.3",
        "label": "Auto-arrival detection radius (meters)",
        "help": "Distance from customer address that counts as 'on-site' for the auto-prompt.",
    },
    "tech_mobile.auto_arrival_threshold_min": {
        "type": "int",
        "default": 2,
        "bounds": (1, 10),
        "phase": "5.3",
        "label": "Auto-arrival prompt threshold (minutes)",
        "help": "Minutes inside the auto-arrival radius before prompting 'Mark arrived?'",
    },
    "tech_mobile.callback_window_days": {
        "type": "int",
        "default": 90,
        "bounds": (14, 365),
        "phase": "5.1",
        "label": "Callback window (days)",
        "help": "Jobs within this window of a prior visit to the same equipment count as a callback.",
    },
    "tech_mobile.callback_compensation": {
        "type": "enum",
        "default": "none",
        "bounds": ("none", "no_commission", "bonus_structure"),
        "phase": "5.1",
        "label": "Callback compensation impact",
        "help": "How callback jobs affect tech payroll/commission.",
    },
    # --- Sprint 5 — Diagnosis & Hazards ----------------------------------
    "tech_mobile.diagnosis_required": {
        "type": "enum",
        "default": "optional",
        "bounds": ("required", "optional"),
        "phase": "5.2",
        "label": "Diagnosis form required before completion",
        "help": "When required, techs must complete the diagnosis form before marking a job complete.",
    },
    "tech_mobile.hazard_photo_required": {
        "type": "enum",
        "default": "optional",
        "bounds": ("required", "optional"),
        "phase": "5.2",
        "label": "Hazard / safety photo required",
        "help": "When required, techs must capture a hazard/safety photo for jobs flagged with hazards.",
    },
    "tech_mobile.receipt_photo_required": {
        "type": "enum",
        "default": "optional",
        "bounds": ("required", "optional"),
        "phase": "5.2",
        "label": "Receipt photo required for road purchases",
        "help": "When required, techs must attach a receipt photo for any road-purchased parts.",
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
    "tech_mobile.gps_breadcrumb_interval_seconds": {
        "type": "int",
        "default": 30,
        "bounds": (10, 600),
        "phase": "5.3",
        "label": "GPS sampling interval (seconds)",
        "help": "How often the tech app samples location while clocked in.",
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
    "tech_mobile.break_tracking": {
        "type": "enum",
        "default": "optional",
        "bounds": ("required", "optional", "off"),
        "phase": "6.1",
        "label": "Break / lunch tracking",
        "help": "off = no break tracking; optional = tech may log breaks; required = tech must log breaks each shift.",
    },
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
