from __future__ import annotations

import logging

from gdx_dispatch.core.websocket import manager

logger = logging.getLogger(__name__)


async def emit_job_update(
    tenant_id: str,
    job_id: str,
    status: str,
    tech_id: str | None = None,
) -> None:
    """
    Broadcast a job status update to all connected clients for a tenant.

    Called from job update endpoints after a successful DB commit.
    """
    try:
        await manager.broadcast_to_tenant(
            tenant_id,
            {
                "type": "job_update",
                "job_id": job_id,
                "status": status,
                "tech_id": tech_id,
            },
        )
    except Exception as exc:
        logger.error("emit_job_update failed for tenant=%s job=%s: %s", tenant_id, job_id, exc)


async def emit_notification(
    tenant_id: str,
    user_id: str,
    notification: dict,
) -> None:
    """
    Push a notification to a specific user within a tenant.

    Called from notification creation helpers/endpoints.
    """
    try:
        await manager.send_to_user(
            tenant_id,
            user_id,
            {"type": "notification", **notification},
        )
    except Exception as exc:
        logger.error(
            "emit_notification failed for tenant=%s user=%s: %s",
            tenant_id,
            user_id,
            exc,
        )
