"""SS-15 slice C — custom-field sensitivity classifier.

Closes the D-52.1 round-9 PII vector: tenant admins classify each
custom field as ``public``, ``internal``, or ``restricted``. The
classification is read from ``ResourceFieldDescriptor.sensitivity_classification``.

This module is the pure logic surface — no router code lives here.
The router at ``gdx_dispatch/routers/custom_field_sensitivity.py`` composes
these helpers with FastAPI plumbing.

Sensitivity taxonomy (P7 / P11, matching existing model default):

- ``public``    — safe to return to any authenticated caller
- ``internal``  — default; tenant members only (the model default)
- ``restricted``— only callers holding ``read:{resource_type}:restricted``

Rule: newly-created fields default to ``restricted`` per Risk #2 in the
SS-15 plan ("misclassification defaults must fail closed"). When creating
a descriptor through this module, callers get ``restricted`` unless they
explicitly pass a lower level. The existing model-level default remains
``internal`` for backward compatibility with pre-SS-15 data; new writes
via ``classify_new_field`` fail closed.
"""
from __future__ import annotations

from typing import Any, Iterable

VALID_CLASSIFICATIONS: frozenset[str] = frozenset({"public", "internal", "restricted"})
DEFAULT_FOR_NEW_FIELD: str = "restricted"


class InvalidClassification(ValueError):
    """Raised when a classification value is outside the taxonomy."""


def validate_classification(value: str) -> str:
    """Return the classification if valid, else raise InvalidClassification.

    Accepts string input; normalises to lowercase. Empty / None is rejected.
    """
    if value is None:
        raise InvalidClassification("classification is required")
    if not isinstance(value, str):
        raise InvalidClassification(f"classification must be a string, got {type(value).__name__}")
    normalised = value.strip().lower()
    if normalised not in VALID_CLASSIFICATIONS:
        raise InvalidClassification(
            f"classification must be one of {sorted(VALID_CLASSIFICATIONS)}, got {value!r}"
        )
    return normalised


def classify_new_field(requested: str | None = None) -> str:
    """Return the classification to use when creating a new field.

    If the admin doesn't specify, default to ``restricted`` (fail-closed).
    Otherwise validate and use the requested value.
    """
    if requested is None:
        return DEFAULT_FOR_NEW_FIELD
    return validate_classification(requested)


def is_downgrade(old: str, new: str) -> bool:
    """Return True if ``new`` is a less-restrictive classification than ``old``.

    Order: restricted > internal > public.
    """
    rank = {"restricted": 2, "internal": 1, "public": 0}
    if old not in rank or new not in rank:
        raise InvalidClassification(f"unknown classification in downgrade check: {old!r}, {new!r}")
    return rank[new] < rank[old]


def caller_can_read_restricted(principal_capabilities: Iterable[dict[str, Any]], resource_type: str) -> bool:
    """Return True iff the caller holds ``read:{resource_type}:restricted``.

    The policy engine (SS-7) is the authoritative enforcer; this helper lets
    the router / views short-circuit response filtering without a full policy
    call for simple per-field visibility decisions.

    A ``("*", "*")`` wildcard capability grants access. A capability whose
    ``conditions`` dict contains ``{"sensitivity": "restricted"}`` on a matching
    ``read`` + ``resource_type`` tuple also qualifies.
    """
    for cap in principal_capabilities:
        action = cap.get("action")
        rtype = cap.get("resource_type")
        if action == "*" and rtype == "*":
            return True
        if action != "read":
            continue
        if rtype not in (resource_type, "*"):
            continue
        conditions = cap.get("conditions") or {}
        if isinstance(conditions, dict) and conditions.get("sensitivity") == "restricted":
            return True
    return False


def filter_fields_by_sensitivity(
    fields: Iterable[dict[str, Any]],
    principal_capabilities: Iterable[dict[str, Any]],
    resource_type: str,
) -> list[dict[str, Any]]:
    """Drop restricted fields the caller isn't authorised to see.

    ``fields`` is an iterable of dicts each with a
    ``sensitivity_classification`` key (missing defaults to ``internal``).
    Returns a filtered list. Fields marked ``restricted`` are omitted
    unless ``caller_can_read_restricted`` returns True.
    """
    caps = list(principal_capabilities)
    can_read_restricted = caller_can_read_restricted(caps, resource_type)
    out: list[dict[str, Any]] = []
    for f in fields:
        classification = f.get("sensitivity_classification", "internal")
        if classification == "restricted" and not can_read_restricted:
            continue
        out.append(f)
    return out
