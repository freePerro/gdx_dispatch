"""Default customer-alert tag taxonomy + seeding helper.

Sprint tech_mobile S1-A8.

The tech-mobile "today's route" view surfaces customer alerts (dog
warning, gate code, etc.) on each job card. The data lives in the
existing ``Tag`` + ``TagAssignment`` tables (entity_type='customer'),
which had no platform-default seed before this sprint.

This module owns:

- ``DEFAULT_CUSTOMER_ALERT_TAGS`` — the list of seed entries that ship
  with every new tenant on first provisioning.
- ``seed_default_customer_alert_tags(db, tenant_id)`` — idempotent
  bootstrapper. Inserts any tag from the default list whose ``name`` is
  not already present for the tenant. Safe to re-run; the existing
  ``UNIQUE(company_id, name)`` constraint plus an in-app pre-check
  prevents duplicates either way.

After seed, tenants are free to add, edit, rename, or remove any tag
via /admin/feature-settings/tech-mobile → Tags. The defaults are not
"locked in" — they're just a reasonable starting taxonomy so a new
tenant can use the alerts feature on day one.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from gdx_dispatch.models.tenant_models import Tag


log = logging.getLogger(__name__)


# Each entry is (name, color, description). Names are short codes — the
# mobile UI renders them as alert chips (dog warning, gate code, …) by
# replacing underscores with spaces and title-casing. Tenants can rename
# freely; the front-end never hard-codes any specific name.
DEFAULT_CUSTOMER_ALERT_TAGS: list[tuple[str, str, str]] = [
    ("dog_warning", "#ef4444", "Customer has a dog on the property — beware."),
    ("gate_code", "#3b82f6", "Customer has provided a gate or building code."),
    ("cod_only", "#f59e0b", "Cash on delivery — collect payment before leaving."),
    ("call_first", "#3b82f6", "Customer has asked to be called before arrival."),
    ("wife_signs", "#3b82f6", "Wife is the authorized signer."),
    ("husband_signs", "#3b82f6", "Husband is the authorized signer."),
    ("side_gate", "#3b82f6", "Use the side gate, not the main entrance."),
    ("back_door", "#3b82f6", "Access via the back door."),
    ("tools_outside", "#3b82f6", "Customer prefers tools/staging stay outside."),
    ("parking_difficult", "#f59e0b", "Parking is tight — plan accordingly."),
    ("noise_sensitive", "#f59e0b", "Customer is sensitive to noise (kids/pets/work-from-home)."),
    ("customer_picky", "#f59e0b", "Customer historically picky — extra care + photos."),
    ("repeat_customer", "#10b981", "Has used us before — review prior visit."),
    ("vip", "#8b5cf6", "VIP customer — escalate any issues immediately."),
]


def seed_default_customer_alert_tags(db: Session, tenant_id: str) -> int:
    """Idempotently seed the default customer-alert tag taxonomy.

    Returns the number of newly-inserted tags. Existing rows are left
    alone — including any color/description edits the tenant has made.
    """
    existing_names = {
        n
        for (n,) in db.query(Tag.name)
        .filter(Tag.company_id == tenant_id)
        .all()
    }
    inserted = 0
    for name, color, description in DEFAULT_CUSTOMER_ALERT_TAGS:
        if name in existing_names:
            continue
        db.add(
            Tag(
                id=uuid4(),
                company_id=tenant_id,
                name=name,
                color=color,
                description=description,
            )
        )
        inserted += 1
    if inserted:
        db.commit()
        log.info("customer_alert_tags_seeded tenant=%s inserted=%d", tenant_id, inserted)
    return inserted
