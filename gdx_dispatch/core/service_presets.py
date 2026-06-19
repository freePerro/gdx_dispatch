"""Service-preset catalog — Good / Better / Best per common service type.

Sprint tech_mobile Phase 2.1 (S2-A1 + S2-A2). The mobile quote-builder
shows a tech a list of services ("Spring replacement", "Opener install",
etc.) and for each one offers three pre-priced tiers (good / better /
best) the tech can hand the customer.

Storage strategy: defaults live here as a Python dict so the catalog is
greppable and the first ship doesn't need a new table. Tenants that
want their own catalog override via the
``tech_mobile.service_presets`` setting (a JSON list); the resolver
prefers the tenant override when present and falls back to the
platform default otherwise. Same defaults-then-overrides pattern as
``feature_defaults.TECH_MOBILE_SETTINGS``.

Each preset row carries:
    id           — stable string slug (URL-safe), unique within service
    label        — short human label ("Standard Pair")
    description  — one-line marketing line shown on the customer card
    line_items   — list of {description, quantity, unit_price} for the
                   estimate. unit_price is a Decimal-string; the engine
                   does NOT re-price (tech mobile is read-only — see
                   S2-A6).
    warranty_months
    includes_parts
    sort_order   — 1=good, 2=better, 3=best
"""
from __future__ import annotations

from typing import Any


# Default service-preset catalog. Industry-standard garage-door services.
# Prices are illustrative; tenants override via tech_mobile.service_presets.
DEFAULT_SERVICE_PRESETS: list[dict[str, Any]] = [
    {
        "service": "spring_replacement",
        "label": "Spring Replacement",
        "description": "Replace broken or worn torsion / extension springs.",
        "tiers": [
            {
                "id": "good",
                "label": "Standard Pair",
                "description": "10,000-cycle springs, 1-year warranty. Solid value for the average homeowner.",
                "line_items": [
                    {"description": "Standard 10k-cycle torsion spring (pair)", "quantity": 1, "unit_price": "189.00"},
                    {"description": "Labor — spring replacement", "quantity": 1, "unit_price": "150.00"},
                ],
                "warranty_months": 12,
                "includes_parts": True,
                "sort_order": 1,
            },
            {
                "id": "better",
                "label": "High-Cycle Pair",
                "description": "20,000-cycle springs, 3-year warranty. Best for daily heavy use.",
                "line_items": [
                    {"description": "High-cycle 20k torsion spring (pair)", "quantity": 1, "unit_price": "289.00"},
                    {"description": "Labor — spring replacement", "quantity": 1, "unit_price": "150.00"},
                ],
                "warranty_months": 36,
                "includes_parts": True,
                "sort_order": 2,
            },
            {
                "id": "best",
                "label": "Premium Lifetime",
                "description": "50,000-cycle springs + new bearings + cable inspection. Lifetime warranty.",
                "line_items": [
                    {"description": "Premium 50k torsion spring (pair)", "quantity": 1, "unit_price": "419.00"},
                    {"description": "End-bearing plates (pair)", "quantity": 1, "unit_price": "45.00"},
                    {"description": "Labor — premium spring service", "quantity": 1, "unit_price": "175.00"},
                ],
                "warranty_months": 999,
                "includes_parts": True,
                "sort_order": 3,
            },
        ],
    },
    {
        "service": "opener_replacement",
        "label": "Opener Replacement",
        "description": "Remove failed opener and install replacement.",
        "tiers": [
            {
                "id": "good",
                "label": "Chain-Drive 1/2 HP",
                "description": "Reliable chain-drive opener, single remote, 5-year motor warranty.",
                "line_items": [
                    {"description": "Chain-drive 1/2 HP opener", "quantity": 1, "unit_price": "329.00"},
                    {"description": "Remote (1)", "quantity": 1, "unit_price": "29.00"},
                    {"description": "Labor — installation", "quantity": 1, "unit_price": "200.00"},
                ],
                "warranty_months": 60,
                "includes_parts": True,
                "sort_order": 1,
            },
            {
                "id": "better",
                "label": "Belt-Drive 3/4 HP",
                "description": "Quiet belt-drive, two remotes, keypad, smartphone-ready.",
                "line_items": [
                    {"description": "Belt-drive 3/4 HP opener", "quantity": 1, "unit_price": "489.00"},
                    {"description": "Remotes (2)", "quantity": 2, "unit_price": "29.00"},
                    {"description": "Wireless keypad", "quantity": 1, "unit_price": "59.00"},
                    {"description": "Labor — installation", "quantity": 1, "unit_price": "225.00"},
                ],
                "warranty_months": 120,
                "includes_parts": True,
                "sort_order": 2,
            },
            {
                "id": "best",
                "label": "Smart 1.25 HP w/ Battery Backup",
                "description": "Top-tier smart opener, battery backup, camera, full smartphone control.",
                "line_items": [
                    {"description": "Smart 1.25 HP belt-drive w/ battery backup", "quantity": 1, "unit_price": "729.00"},
                    {"description": "Built-in camera module", "quantity": 1, "unit_price": "99.00"},
                    {"description": "Remotes (2)", "quantity": 2, "unit_price": "29.00"},
                    {"description": "Wireless keypad", "quantity": 1, "unit_price": "59.00"},
                    {"description": "Labor — premium install + smartphone setup", "quantity": 1, "unit_price": "275.00"},
                ],
                "warranty_months": 180,
                "includes_parts": True,
                "sort_order": 3,
            },
        ],
    },
    {
        "service": "tune_up",
        "label": "Door Tune-Up",
        "description": "Inspection + lubrication + minor adjustments.",
        "tiers": [
            {
                "id": "good",
                "label": "Standard Tune-Up",
                "description": "Visual inspection, lubrication, hardware tightening.",
                "line_items": [
                    {"description": "Standard tune-up service", "quantity": 1, "unit_price": "129.00"},
                ],
                "warranty_months": 6,
                "includes_parts": False,
                "sort_order": 1,
            },
            {
                "id": "better",
                "label": "Tune-Up Plus",
                "description": "Standard + spring tension check + cable inspection + roller replacement (10).",
                "line_items": [
                    {"description": "Tune-up plus service", "quantity": 1, "unit_price": "189.00"},
                    {"description": "Nylon rollers (10)", "quantity": 10, "unit_price": "5.00"},
                ],
                "warranty_months": 12,
                "includes_parts": True,
                "sort_order": 2,
            },
            {
                "id": "best",
                "label": "Full Service",
                "description": "Plus + safety reverse test + all hinges replaced + bottom seal + 1-yr warranty on labor.",
                "line_items": [
                    {"description": "Full-service tune-up", "quantity": 1, "unit_price": "289.00"},
                    {"description": "Bottom weather seal", "quantity": 1, "unit_price": "39.00"},
                    {"description": "Hinges (replacement set)", "quantity": 1, "unit_price": "45.00"},
                ],
                "warranty_months": 12,
                "includes_parts": True,
                "sort_order": 3,
            },
        ],
    },
    {
        "service": "cable_repair",
        "label": "Cable Repair",
        "description": "Replace broken or frayed lift cables.",
        "tiers": [
            {
                "id": "good",
                "label": "Standard Cable Set",
                "description": "1/8\" galvanized cables (pair), labor.",
                "line_items": [
                    {"description": "Galvanized cable (pair)", "quantity": 1, "unit_price": "39.00"},
                    {"description": "Labor — cable replacement", "quantity": 1, "unit_price": "150.00"},
                ],
                "warranty_months": 12,
                "includes_parts": True,
                "sort_order": 1,
            },
            {
                "id": "better",
                "label": "Heavy-Duty Cable Set",
                "description": "Aircraft-grade cables, drum inspection, 3-yr warranty.",
                "line_items": [
                    {"description": "Aircraft-grade cable (pair)", "quantity": 1, "unit_price": "69.00"},
                    {"description": "Drum inspection / re-tension", "quantity": 1, "unit_price": "45.00"},
                    {"description": "Labor — cable replacement", "quantity": 1, "unit_price": "150.00"},
                ],
                "warranty_months": 36,
                "includes_parts": True,
                "sort_order": 2,
            },
            {
                "id": "best",
                "label": "Cable + Drum Service",
                "description": "Cables + new drums + bearing inspection. Lifetime cable warranty.",
                "line_items": [
                    {"description": "Aircraft-grade cable (pair)", "quantity": 1, "unit_price": "69.00"},
                    {"description": "New cable drums (pair)", "quantity": 1, "unit_price": "89.00"},
                    {"description": "Center / end bearings", "quantity": 1, "unit_price": "55.00"},
                    {"description": "Labor — full cable + drum service", "quantity": 1, "unit_price": "200.00"},
                ],
                "warranty_months": 999,
                "includes_parts": True,
                "sort_order": 3,
            },
        ],
    },
    {
        "service": "section_replacement",
        "label": "Section Replacement",
        "description": "Replace damaged door panel/section.",
        "tiers": [
            {
                "id": "good",
                "label": "OEM Section Match",
                "description": "Direct OEM match, factory finish.",
                "line_items": [
                    {"description": "OEM section (single panel)", "quantity": 1, "unit_price": "289.00"},
                    {"description": "Labor — section replacement", "quantity": 1, "unit_price": "175.00"},
                ],
                "warranty_months": 12,
                "includes_parts": True,
                "sort_order": 1,
            },
            {
                "id": "better",
                "label": "Insulated OEM Section",
                "description": "OEM with R-9 insulation upgrade.",
                "line_items": [
                    {"description": "Insulated OEM section", "quantity": 1, "unit_price": "389.00"},
                    {"description": "Labor — section replacement", "quantity": 1, "unit_price": "175.00"},
                ],
                "warranty_months": 24,
                "includes_parts": True,
                "sort_order": 2,
            },
            {
                "id": "best",
                "label": "Full Door Recommended",
                "description": "Door is past worth-repairing. Quote includes full replacement consultation.",
                "line_items": [
                    {"description": "Full door replacement consult — credit applies if full door ordered", "quantity": 1, "unit_price": "0.00"},
                ],
                "warranty_months": 0,
                "includes_parts": False,
                "sort_order": 3,
            },
        ],
    },
    {
        "service": "door_replacement",
        "label": "Full Door Replacement",
        "description": "Remove and install a complete new garage door.",
        "tiers": [
            {
                "id": "good",
                "label": "Standard Steel 16x7",
                "description": "Non-insulated 25-gauge steel, white. Hardware + standard install.",
                "line_items": [
                    {"description": "16x7 steel door (non-insulated)", "quantity": 1, "unit_price": "899.00"},
                    {"description": "Track + hardware", "quantity": 1, "unit_price": "199.00"},
                    {"description": "Labor — door install", "quantity": 1, "unit_price": "450.00"},
                ],
                "warranty_months": 24,
                "includes_parts": True,
                "sort_order": 1,
            },
            {
                "id": "better",
                "label": "Insulated Steel 16x7",
                "description": "R-12 polyurethane core. Quieter, energy-saving.",
                "line_items": [
                    {"description": "16x7 insulated steel door (R-12)", "quantity": 1, "unit_price": "1389.00"},
                    {"description": "Track + heavy-duty hardware", "quantity": 1, "unit_price": "229.00"},
                    {"description": "Labor — door install", "quantity": 1, "unit_price": "450.00"},
                ],
                "warranty_months": 60,
                "includes_parts": True,
                "sort_order": 2,
            },
            {
                "id": "best",
                "label": "Carriage House Premium",
                "description": "Decorative carriage style with windows. Lifetime warranty on door body.",
                "line_items": [
                    {"description": "16x7 carriage-house premium door", "quantity": 1, "unit_price": "2289.00"},
                    {"description": "Decorative windows (4)", "quantity": 1, "unit_price": "189.00"},
                    {"description": "Track + premium hardware", "quantity": 1, "unit_price": "279.00"},
                    {"description": "Labor — premium install + alignment", "quantity": 1, "unit_price": "550.00"},
                ],
                "warranty_months": 999,
                "includes_parts": True,
                "sort_order": 3,
            },
        ],
    },
]


# Default decline-reason taxonomy. Used by S2-A5; surfaced in
# /api/mobile/quotes/decline-reasons. Tenants override via
# tech_mobile.quote_decline_reasons.
DEFAULT_DECLINE_REASONS: list[str] = [
    "Priced too high",
    "Not today",
    "Wants a second opinion",
    "Going to do it themselves",
    "Already booked another company",
    "Other",
]


def list_default_services() -> list[dict[str, Any]]:
    """Return a shallow copy of the default service preset catalog."""
    return [dict(s, tiers=[dict(t) for t in s["tiers"]]) for s in DEFAULT_SERVICE_PRESETS]


def find_default_preset(service: str, tier_id: str) -> dict[str, Any] | None:
    """Return the (service, tier_id) preset row from the default catalog, or None."""
    for s in DEFAULT_SERVICE_PRESETS:
        if s["service"] == service:
            for t in s["tiers"]:
                if t["id"] == tier_id:
                    return {"service": s["service"], "label": s["label"], **t}
    return None
