"""Canonical job_type vocabulary + the pricing-lane resolver — plan §9.

`Job.job_type` is free-text String(100) with no constraint, and independent
code paths invented their own vocabulary against it. Prod 2026-07-29 held
FOUR spellings: `QB Import` (159), `Service Call` (34), `Service` (12),
`Installation` (10) — because two frontend dropdowns disagreed (JobsView
offered "Service Call", CustomerDetailView offered "Service") and backend
writers each picked their own (`service_calls.py` "Service Call", `jobs.py`
default "Service", `instant_estimate.py` lowercase, `seed_demo.py`
"Install"). Already broken before any billing work: `service_calls.py`
filtered `== "Service Call"` so the 12 `Service` jobs were invisible to the
service-call queue, and the AI quote-template exact-match missed on the
wrong spelling. (The JobsView count tiles are STATUS-based, not job_type —
an earlier draft of this note blamed them wrongly.)

Two rules, decided by Doug 2026-07-29:

* Canonical spellings live HERE and every writer/dropdown imports them.
  Canonical service spelling is **"Service Call"** (the 34-row majority, the
  dedicated reader's literal, and the term the business uses).
* The PRICING LANE is a function of job_type, never a rewrite of it:
  `Repair` and `Maintenance` stay stored as themselves (they carry meaning
  the office uses) and resolve to the service lane. Collapsing descriptors
  into lanes at write time destroys information unrecoverably.

Lane map (plan §9/§15 — the §8 invoice-from-closeout lanes):
    Service Call / Repair / Maintenance → "service"  (hourly)
    Installation                        → "install"  (flat from the matrix;
                                          an accepted estimate outranks both)
    New Construction / Inspection / Other / QB Import / NULL / unknown
                                        → "office"   (never auto-priced)
"""
from __future__ import annotations

from typing import Final

SERVICE_CALL: Final = "Service Call"
REPAIR: Final = "Repair"
MAINTENANCE: Final = "Maintenance"
INSTALLATION: Final = "Installation"
NEW_CONSTRUCTION: Final = "New Construction"
INSPECTION: Final = "Inspection"
OTHER: Final = "Other"
QB_IMPORT: Final = "QB Import"  # provenance-in-a-work-kind-field; legacy, kept

#: What the job-type dropdowns offer, in display order. QB Import is
#: deliberately absent — it is import provenance, not an operator choice.
JOB_TYPE_OPTIONS: Final[tuple[str, ...]] = (
    SERVICE_CALL,
    INSTALLATION,
    REPAIR,
    MAINTENANCE,
    NEW_CONSTRUCTION,
    INSPECTION,
    OTHER,
)

#: Legacy alias → canonical. Case-insensitive lookup via canonical_job_type.
_ALIASES: Final[dict[str, str]] = {
    "service": SERVICE_CALL,
    "service call": SERVICE_CALL,
    "servicecall": SERVICE_CALL,
    "repair": REPAIR,
    "maintenance": MAINTENANCE,
    "install": INSTALLATION,
    "installation": INSTALLATION,
    "new construction": NEW_CONSTRUCTION,
    "inspection": INSPECTION,
    "other": OTHER,
    "qb import": QB_IMPORT,
    "quickbooks import": QB_IMPORT,
}

_SERVICE_LANE: Final = frozenset({SERVICE_CALL, REPAIR, MAINTENANCE})

#: The service lane as a tuple for SQL IN filters (the repair-dispatch queue).
SERVICE_LANE_TYPES: Final[tuple[str, ...]] = (SERVICE_CALL, REPAIR, MAINTENANCE)


def canonical_job_type(value: str | None) -> str | None:
    """Fold any legacy spelling to its canonical form.

    Unknown free text comes back UNCHANGED (never guess a work kind — the
    lane resolver sends unknowns to the office); None stays None so callers
    can apply their own default.
    """
    if value is None:
        return None
    key = " ".join(str(value).split()).lower()
    if not key:
        return None
    return _ALIASES.get(key, str(value).strip())


def pricing_lane(job_type: str | None) -> str:
    """The §8 pricing lane for a job_type: 'service' | 'install' | 'office'.

    NEVER key this on lifecycle_stage — that is a progress axis, and its
    'service_call' value is the repair-dispatch lane; conflating the two
    prices every sold install as a repair (the standing taxonomy rule).
    'office' is the safe floor: it can never bill a customer wrong.
    """
    canon = canonical_job_type(job_type)
    if canon in _SERVICE_LANE:
        return "service"
    if canon == INSTALLATION:
        return "install"
    return "office"
