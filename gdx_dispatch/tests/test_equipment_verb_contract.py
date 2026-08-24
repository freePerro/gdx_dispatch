"""The verb EquipmentView sends must be a verb the backend actually serves.

2026-05-03: `routers/equipment_tracking.py` was deliberately UNWIRED to retire
the parallel `equipment_assets` table, and `modules/equipment/router.py` became
canonical. The canonical router registers **PUT** `/api/equipment/{id}`; the
retired one registers PATCH. `EquipmentView.vue` kept sending PATCH.

Result: **saving an equipment edit 405'd silently for over three months.** The
live route table says `GET/PUT/DELETE` for that path — no PATCH.

Why this test exists rather than a comment. Two separate audits looked at this
and one of them (the 2026-08-21 completion sweep) scored it FIXED, because
`grep PATCH routers/equipment_tracking.py` finds a decorator and the file gives
no hint it is unmounted. Reading a router file cannot tell you whether it
serves anything. This compares the two sources that must agree, so neither can
move alone.

Static-source by design, same rationale as
`test_jobs_create_payload_contract.py`: standing up the full app and a tenant
DB to ask "which verbs are registered" is overkill for a question the source
answers exactly.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL = (REPO_ROOT / "gdx_dispatch/modules/equipment/router.py").read_text(encoding="utf-8")
VIEW = (REPO_ROOT / "gdx_dispatch/frontend/src/views/EquipmentView.vue").read_text(encoding="utf-8")
APP = (REPO_ROOT / "gdx_dispatch/app.py").read_text(encoding="utf-8")

# @router.put("/equipment/{equipment_id}", ...) — the prefix-less decorator path
_DECORATOR = re.compile(
    r'@router\.(get|post|put|patch|delete)\(\s*"/equipment/\{[a-z_]+\}"'
)
# await api.put(`/api/equipment/${...}`  — the client call in the view
_CALL = re.compile(
    r'api\.(get|post|put|patch|del|delete)\(\s*`/api/equipment/\$\{[^`]*\}`'
)


def _served_verbs() -> set[str]:
    return {m.group(1) for m in _DECORATOR.finditer(CANONICAL)}


def _sent_verbs() -> set[str]:
    return {m.group(1) for m in _CALL.finditer(VIEW)}


def test_the_canonical_router_serves_put_for_a_single_equipment_record():
    """Anchors the other assertions. If this fails the path moved and the rest
    of this file is measuring the wrong thing."""
    assert "put" in _served_verbs(), (
        "modules/equipment/router.py no longer registers PUT /equipment/{id} — "
        f"found {sorted(_served_verbs())}"
    )


def test_equipment_view_sends_only_verbs_the_backend_serves():
    """The actual contract. PATCH here is the three-month silent failure."""
    served = _served_verbs()
    sent = _sent_verbs()

    assert sent, (
        "no /api/equipment/{id} call found in EquipmentView.vue — if the call "
        "moved, move this test with it rather than deleting it"
    )
    unserved = {v for v in sent if v not in served and not (v == "del" and "delete" in served)}
    assert not unserved, (
        f"EquipmentView sends {sorted(unserved)} to /api/equipment/{{id}}, which "
        f"the canonical router does not serve (it serves {sorted(served)}). "
        "This 405s. The PATCH route in routers/equipment_tracking.py does not "
        "count — that router is unwired; see the test below."
    )


def test_the_retired_equipment_tracking_router_stays_unwired():
    """Guards the wrong fix.

    The tempting repair for a 405 is to make the PATCH route real by mounting
    `equipment_tracking_router` again. That router is backed by the
    `equipment_assets` table the 2026-05-03 consolidation deliberately removed,
    so rewiring it resurrects a parallel equipment store — a worse outcome than
    the bug. An absence assertion, deliberately: proving it is NOT mounted is
    real evidence, unlike asserting some string is present.
    """
    mounted = re.search(
        r"include_router\(\s*equipment_tracking_router", APP
    )
    assert mounted is None, (
        "equipment_tracking_router is mounted again. It is backed by the "
        "equipment_assets table retired on 2026-05-03; mounting it re-creates "
        "the parallel equipment surface that consolidation removed. If the "
        "intent was to serve PATCH, repoint the frontend to PUT instead."
    )
