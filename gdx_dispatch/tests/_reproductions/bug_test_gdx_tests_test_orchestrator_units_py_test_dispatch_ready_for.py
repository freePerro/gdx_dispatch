import pytest
from gdx_dispatch.orchestrator.units import Orchestrator


@pytest.mark.xfail(strict=False, reason="reproduction — bug not yet fixed")
def test_dispatch_ready_for_codex_flips_to_in_progress_and_invokes():
    # Setup: Mock the state and the codex client
    orchestrator = Orchestrator()
    task_id = "test-task-123"

    # Set initial state to READY
    orchestrator.set_task_status(task_id, "READY")

    # Mock the codex service to track if it gets called
    orchestrator.codex_client.dispatch = lambda tid: True

    # Action: Trigger the dispatch logic
    orchestrator.dispatch_ready_for_codex(task_id)

    # Assertions:
    # 1. The status should transition from READY to IN_PROGRESS
    assert orchestrator.get_task_status(task_id) == "IN_PROGRESS"

    # 2. The codex client should have been invoked
    orchestrator.codex_client.dispatch.assert_called_once_with(task_id)
