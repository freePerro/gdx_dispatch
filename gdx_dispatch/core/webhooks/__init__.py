import logging

from gdx_dispatch.core.webhooks.delivery import deliver_webhook, sign_payload
from gdx_dispatch.core.webhooks.models import WebhookDelivery, WebhookEndpoint

# Celery task imports are optional — guarded so that lightweight consumers
# (tests, scripts) can import this package without a running broker or celery
# installed.
try:
    from gdx_dispatch.core.webhooks.tasks import deliver_webhook_task, emit_webhook, retry_failed_webhooks_task
except ImportError:
    logging.getLogger(__name__).exception("<module> caught exception")
    deliver_webhook_task = None  # type: ignore[assignment]
    emit_webhook = None  # type: ignore[assignment]
    retry_failed_webhooks_task = None  # type: ignore[assignment]

__all__ = [
    "WebhookEndpoint",
    "WebhookDelivery",
    "sign_payload",
    "deliver_webhook",
    "deliver_webhook_task",
    "retry_failed_webhooks_task",
    "emit_webhook",
]
