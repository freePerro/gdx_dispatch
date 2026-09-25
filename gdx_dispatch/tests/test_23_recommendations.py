"""Tests for gdx_dispatch/core/next_action.py (the NextActionQueue)."""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gdx_dispatch.core.next_action import NextActionQueue  # noqa: E402

# ===========================================================================
# NextActionQueue tests
# ===========================================================================

# ---------------------------------------------------------------------------
# NextActionQueue — create and retrieve persisted action
# ---------------------------------------------------------------------------

def test_next_action_queue_create_and_retrieve(tenant_db, control_db):
    """Created actions appear in the queue."""
    q = NextActionQueue()
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    result = q.create_action(
        tenant_id=tenant_id,
        user_id=user_id,
        action_type="follow_up_estimate",
        title="Follow Up on Estimate #123",
        description="Call the customer back.",
        priority="high",
        action_url="/jobs/123",
        estimated_value=250.0,
        reference_id="job-123",
        tenant_db=tenant_db,
    )

    assert "error" not in result
    assert result["action_type"] == "follow_up_estimate"
    assert result["priority"] == "high"
    assert result["status"] == "pending"

    # Queue should include this action
    queue_items = q.get_queue(tenant_id, user_id, tenant_db)
    ids = [item["id"] for item in queue_items]
    assert result["id"] in ids


# ---------------------------------------------------------------------------
# NextActionQueue — complete and snooze actions
# ---------------------------------------------------------------------------

def test_next_action_queue_complete_and_snooze(tenant_db, control_db):
    """complete_action and snooze_action update status correctly."""
    q = NextActionQueue()
    tenant_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())

    # Create two actions
    action_a = q.create_action(
        tenant_id=tenant_id,
        user_id=user_id,
        action_type="call_overdue_invoice",
        title="Call Customer A",
        description=None,
        priority="high",
        action_url="/invoices/1",
        estimated_value=500.0,
        reference_id="inv-1",
        tenant_db=tenant_db,
    )
    action_b = q.create_action(
        tenant_id=tenant_id,
        user_id=user_id,
        action_type="request_review",
        title="Request Review",
        description=None,
        priority="low",
        action_url="/customers/1",
        estimated_value=0.0,
        reference_id="cust-1",
        tenant_db=tenant_db,
    )

    # Complete action A
    done = q.complete_action(tenant_id, action_a["id"], tenant_db)
    assert done["status"] == "completed"

    # Snooze action B until tomorrow
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    snoozed = q.snooze_action(tenant_id, action_b["id"], tomorrow, tenant_db)
    assert snoozed["status"] == "snoozed"
    assert "snoozed_until" in snoozed

    # Queue should now exclude both
    queue_items = q.get_queue(tenant_id, user_id, tenant_db)
    persisted_ids = [
        item["id"]
        for item in queue_items
        if not str(item["id"]).startswith("auto:")
    ]
    assert action_a["id"] not in persisted_ids
    assert action_b["id"] not in persisted_ids
