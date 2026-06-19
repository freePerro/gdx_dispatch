from __future__ import annotations

from gdx_dispatch.core.celery_app import celery_app


@celery_app.task(queue="low")
def send_campaign_task(send_id: str) -> None:
    return None
