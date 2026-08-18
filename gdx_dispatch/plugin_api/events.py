"""Plugin event platform — shared types + matching (stdlib only).

Imported by both the core fan-out (to pick recipients) and the plugin-host (to
route a dispatched event to the right handler). Kept dependency-light like the
rest of plugin_api so it unit-tests on bare python.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PluginEvent:
    """What a plugin's event_handler receives. At-least-once, unordered — the
    handler must be idempotent on ``delivery_id``."""

    name: str
    data: dict[str, Any]
    tenant_id: str
    occurred_at: str
    delivery_id: str

    @classmethod
    def from_wire(cls, body: dict[str, Any]) -> PluginEvent:
        return cls(
            name=str(body.get("event") or body.get("name") or ""),
            data=dict(body.get("data") or {}),
            tenant_id=str(body.get("tenant_id") or ""),
            occurred_at=str(body.get("occurred_at") or ""),
            delivery_id=str(body.get("delivery_id") or ""),
        )


def event_matches(name: str, patterns) -> bool:
    """Does ``name`` (e.g. "invoice.paid") match any subscription pattern?

    Patterns: an exact name, a one-level prefix wildcard ("invoice.*"), or "*"
    (everything). Deliberately simple — no multi-segment globbing.
    """
    for p in patterns or ():
        if p == "*" or p == name:
            return True
        if isinstance(p, str) and p.endswith(".*") and name.startswith(p[:-1]):
            return True
    return False


def capability_fingerprint(events=(), schedule_names=(), services=()) -> str:
    """Stable hash of a plugin's declared automatic-execution surface.

    Consent is recorded against this fingerprint; if a plugin upgrade changes
    its declared events/schedules/services, the fingerprint changes and dispatch
    fail-closes until the owner re-consents. Hashes serialized NAMES only —
    never the callable objects (no stable identity across boots), and NOT the
    cron strings (a retiming is not a new capability). Both consent-time and
    dispatch-time compute this from the /api/plugins catalog, which exposes the
    same name lists — so the two fingerprints are directly comparable.
    """
    payload = {
        "events": sorted(str(e) for e in (events or ())),
        "schedules": sorted(str(s) for s in (schedule_names or ())),
        "services": sorted(str(s) for s in (services or ())),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()
