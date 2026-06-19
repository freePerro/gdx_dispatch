"""Sprint 1.x slice 1.x-s10 — capability sets per role + AI ceiling logic.

Defines the canonical mapping of roles to capability sets and the logic
for deriving restricted capabilities for AI workers.
"""
from __future__ import annotations

from collections.abc import Iterable

__all__ = [
    "CAPABILITY_SETS_BY_ROLE",
    "caps_for_role",
    "derive_ai_worker_caps",
]

# Canonical mapping of roles to their default capability sets.
# Note: "ai_worker" is empty because its capabilities are derived per-request.
CAPABILITY_SETS_BY_ROLE: dict[str, tuple[tuple[str, str], ...]] = {
    "admin": (("*", "*"),),
    "owner": (("*", "*"),),
    "technician": (
        ("read", "customer"),
        ("read", "job"),
        ("read", "schedule"),
        ("read", "parts"),
        ("write", "job.own"),
    ),
    "customer": (
        ("read", "customer.own"),
        ("read", "invoice.own"),
        ("read", "job.own"),
    ),
    "ai_worker": (),
}


def caps_for_role(role: str) -> tuple[tuple[str, str], ...]:
    """Return the default capability tuple for a coarse role name.

    Lookup is case-insensitive. Returns an empty tuple for unknown roles.
    """
    return CAPABILITY_SETS_BY_ROLE.get(role.lower(), ())


def derive_ai_worker_caps(
    delegating_admin_caps: Iterable[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Intersect an admin's capabilities with the AI ceiling.

    This implements the v1 hard-coded ceiling for AI workers.
    Specifically:
    1. Drops any cap where action is "admin".
    2. Drops the superuser wildcard ("*", "*").
    3. Drops red-blast-radius capabilities (v1 hard-coded list):
       - ("write", "invoice")
       - ("delete", "*")
       - ("void", "*")
    4. Narrows all write capabilities to the S2 whitelist:
       - ("write", "*") -> ("write", "customer.contact")
       - ("write", "<resource>") -> ("write", "customer.contact") if <resource> != "customer.contact"
       (Actually, the rule is: if it's a write, it MUST be "customer.contact" to pass).
    5. Keeps all read capabilities as-is.

    Returns a deduped, sorted tuple.
    """
    # Red-tier ceiling (v1 hard-coded)
    RED_TIER_CEILING = {("write", "invoice"), ("delete", "*"), ("void", "*")}
    # Whitelist of resource strings the AI worker is allowed to write.
    # Adding a new write tool? Add its `capabilities_required` resource
    # string here — otherwise admin-derived AI workers can't reach it.
    WRITE_WHITELIST = frozenset(
        {
            "customer.contact",
            "email",
            "email.draft",
            "document",
            "document.folder",
        }
    )
    # Resources the AI worker is allowed to read when delegated by a
    # superuser-wildcard principal. Keyed on what tools in
    # ``gdx_dispatch/core/mcp_tools/`` declare as ``capabilities_required``. If a new
    # read tool is added with a new resource type, add it here too — or it
    # will be invisible to admin-derived AI workers.
    SUPERUSER_READ_FAN_OUT = (
        ("read", "customer"),
        ("read", "job"),
        ("read", "invoice"),
        ("read", "schedule"),
        ("read", "technician"),
        ("read", "email"),
        ("read", "document"),
    )

    out: set[tuple[str, str]] = set()

    for action, resource in delegating_admin_caps:
        # 1. Drop admin action
        if action == "admin":
            continue

        # 2. Superuser wildcard expands to the AI-read fan-out PLUS every
        #    write resource on the whitelist. Without this, an admin
        #    (canonical caps ``(("*", "*"),)``) would derive an empty
        #    AI-worker cap set and have zero tools available.
        if action == "*" and resource == "*":
            out.update(SUPERUSER_READ_FAN_OUT)
            for resource_name in WRITE_WHITELIST:
                out.add(("write", resource_name))
            continue

        # 3. Drop red-tier
        if (action, resource) in RED_TIER_CEILING:
            continue

        # 4. Handle writes
        if action == "write":
            if resource == "*":
                # Broad write cap fans out to the whitelist.
                for resource_name in WRITE_WHITELIST:
                    out.add(("write", resource_name))
            elif resource in WRITE_WHITELIST:
                out.add(("write", resource))
            else:
                # Write to a resource outside the whitelist is dropped.
                continue
        else:
            # 5. Keep everything else (reads, etc.)
            out.add((action, resource))

    return tuple(sorted(list(out)))
