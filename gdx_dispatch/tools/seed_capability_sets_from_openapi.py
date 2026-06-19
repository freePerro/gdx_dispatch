"""Seed capability rows for default capability sets (SS-4 slice A).

Currently uses a curated map aligned to the Sprint 0.7 platform plan.
"""
from __future__ import annotations

import argparse
import json
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from gdx_dispatch.models.platform import Capability, CapabilitySet

CORE_RESOURCE_TYPES = [
    "job",
    "customer",
    "invoice",
    "estimate",
    "lead",
    "user",
    "tenant",
    "work_order",
    "appointment",
    "payment",
    "expense",
    "vendor",
    "document",
]
CORE_ACTIONS = ["read", "write", "delete", "admin"]

ROLE_CAPABILITIES: dict[str, dict[str, list[str] | bool]] = {
    "role:owner": {"_wildcard": True},
    "role:admin": {"_wildcard": True},
    "role:tech": {"read": ["job", "customer", "estimate"], "write": ["job"]},
    "role:contractor": {"read": ["job"], "write": ["job"]},
    "role:viewer": {"read": ["job", "customer", "invoice", "estimate"]},
    "platform:internal": {"read": ["*"], "admin": ["platform-jobs"]},
    "platform:service_account_full": {"_wildcard": True},
}


def seed_capabilities_for_set(db: Session, capability_set: CapabilitySet) -> int:
    db.execute(delete(Capability).where(Capability.capability_set_id == capability_set.id))
    inserted = 0
    role_name = capability_set.name
    spec = ROLE_CAPABILITIES.get(role_name, {})

    if spec.get("_wildcard"):
        for resource_type in CORE_RESOURCE_TYPES:
            for action in CORE_ACTIONS:
                db.add(
                    Capability(
                        id=uuid4(),
                        capability_set_id=capability_set.id,
                        action=action,
                        resource_type=resource_type,
                        instance_pattern="*",
                        conditions={},
                    )
                )
                inserted += 1
        return inserted

    for action, resources in spec.items():
        if action.startswith("_"):
            continue
        assert isinstance(resources, list)
        for resource_type in resources:
            db.add(
                Capability(
                    id=uuid4(),
                    capability_set_id=capability_set.id,
                    action=action,
                    resource_type=resource_type,
                    instance_pattern="*",
                    conditions={},
                )
            )
            inserted += 1
    return inserted


def seed_capabilities(db: Session, dry_run: bool = False) -> dict:
    stats = {"sets_seen": 0, "sets_seeded": 0, "capabilities_inserted": 0}
    sets = db.execute(select(CapabilitySet)).scalars().all()
    stats["sets_seen"] = len(sets)
    for cap_set in sets:
        inserted = seed_capabilities_for_set(db, cap_set)
        stats["sets_seeded"] += 1
        stats["capabilities_inserted"] += inserted
    if dry_run:
        db.rollback()
    else:
        db.commit()
    return stats


def _main() -> int:
    parser = argparse.ArgumentParser(description="Seed capabilities for platform capability sets.")
    parser.add_argument("--apply", action="store_true", help="Persist changes (default is dry-run).")
    args = parser.parse_args()

    from gdx_dispatch.core.database import SessionLocal

    with SessionLocal() as db:
        result = seed_capabilities(db, dry_run=not args.apply)
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"{mode} seed_capability_sets_from_openapi result:")
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
