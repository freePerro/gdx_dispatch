from __future__ import annotations

from gdx_dispatch.core.celery_app import celery_app


# No queue= kwarg: "low" isn't a consumed queue (2026-07-07 audit) and a
# decorator queue overrides task_routes, which sends campaigns.* to
# priority:high.
@celery_app.task
def send_campaign_task(send_id: str) -> None:
    return None
