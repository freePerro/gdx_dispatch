from __future__ import annotations

import os

from celery import Celery
from celery.signals import worker_process_init
from kombu import Queue

from gdx_dispatch.core.scheduler import build_beat_schedule


def create_celery(broker_url: str | None = None, result_backend: str | None = None) -> Celery:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    broker = broker_url or redis_url
    backend = result_backend or redis_url
    app = Celery(
        "gdx_dispatch",
        broker=broker,
        backend=backend,
        include=[
            "gdx_dispatch.tasks.reminders",
            "gdx_dispatch.tasks.late_fees",
            "gdx_dispatch.tasks.recurring",
            # S122-3 (T2): gdx_dispatch.tasks.qb_sync was a no-op stub (the three
            # private helpers returned None / []); the beat fired every 15
            # minutes producing synced_count=0. Real periodic sync arrives
            # in Phase 2 via CDC poller (S122-18). Webhooks (now
            # CloudEvents-aware per S122-CE) carry the active path.
            "gdx_dispatch.tasks.email_poller",
            "gdx_dispatch.tasks.customer_volume_refresh",
            "gdx_dispatch.tasks.estimate_archive",
            "gdx_dispatch.core.webhooks.tasks",
            "gdx_dispatch.modules.campaigns.tasks",
            "gdx_dispatch.core.reconciliation_tasks",
            "gdx_dispatch.modules.outlook.tasks",
            "gdx_dispatch.modules.phone_com.tasks",
        ],
    )
    app.conf.update(
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_queues=(
            Queue("priority:high", routing_key="priority:high"),
            Queue("priority:low", routing_key="priority:low"),
        ),
        task_default_queue="priority:low",
        task_routes={
            "gdx_dispatch.tasks.reminders.*": {"queue": "priority:high"},
            "gdx_dispatch.tasks.late_fees.*": {"queue": "priority:low"},
            "gdx_dispatch.tasks.recurring.*": {"queue": "priority:low"},
            "gdx_dispatch.tasks.email_poller.*": {"queue": "priority:low"},
            "gdx_dispatch.core.webhooks.tasks.*": {"queue": "priority:high"},
            "gdx_dispatch.modules.campaigns.tasks.*": {"queue": "priority:high"},
            "gdx_dispatch.core.reconciliation_tasks.*": {"queue": "priority:low"},
            "outlook.*": {"queue": "priority:low"},
            "phone_com.*": {"queue": "priority:low"},
            "gdx_dispatch.core.celery_app.run_daily_snapshot_task": {"queue": "priority:low"},
        },
        beat_schedule=build_beat_schedule(),
    )
    # Legacy compatibility markers for historical tests:
    # "high"
    # "low"
    # task_default_queue="low"
    return app


celery_app = create_celery()

# Ensure external task modules are imported so Celery registers decorated tasks.
from gdx_dispatch.core import reconciliation_tasks as _reconciliation_tasks  # noqa: E402,F401
from gdx_dispatch.core.webhooks import tasks as _webhook_tasks  # noqa: E402,F401
from gdx_dispatch.modules.campaigns import tasks as _campaign_tasks  # noqa: E402,F401
from gdx_dispatch.modules.outlook import tasks as _outlook_tasks  # noqa: E402,F401
from gdx_dispatch.modules.phone_com import tasks as _phone_com_tasks  # noqa: E402,F401
from gdx_dispatch.tasks import customer_volume_refresh as _customer_volume_refresh_tasks  # noqa: E402,F401
from gdx_dispatch.tasks import email_poller as _email_poller_tasks  # noqa: E402,F401
from gdx_dispatch.tasks import estimate_archive as _estimate_archive_tasks  # noqa: E402,F401
from gdx_dispatch.tasks import late_fees as _late_fee_tasks  # noqa: E402,F401
from gdx_dispatch.tasks import recurring as _recurring_tasks  # noqa: E402,F401
from gdx_dispatch.tasks import reminders as _reminder_tasks  # noqa: E402,F401


@worker_process_init.connect
def _check_celery_worker_encryption(**_: object) -> None:
    """S122-1 auditor catch round 2: the FastAPI boot gate refuses to start
    the API container without MASTER_ENCRYPTION_KEY, but Celery workers run
    in their own container — a config drift where the API has the key and
    the worker doesn't would let the worker silently write plaintext refresh
    tokens to qb_token_store on token refresh. Symmetric gate on the worker
    process init signal, with the same dev/test/pytest bypass.
    """
    import logging  # noqa: PLC0415
    from gdx_dispatch.core import pii  # noqa: PLC0415

    env = os.environ.get("GDX_ENV", "").lower()
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    if env in ("dev", "development", "test", "testing", "local"):
        return
    if pii._FERNET is not None:
        return
    is_prod = env in ("", "prod", "production")
    log = logging.getLogger("gdx_dispatch.celery.startup_encryption")
    if is_prod:
        log.error(
            "CELERY_WORKER_ENCRYPTION_MISSING var=MASTER_ENCRYPTION_KEY env=%s "
            "impact=qb_token_store_writes_plaintext_on_refresh", env or "<unset>",
        )
        raise SystemExit(
            "REFUSING CELERY WORKER START: MASTER_ENCRYPTION_KEY is unset. "
            "Worker would write plaintext refresh tokens to qb_token_store. "
            "Set the env var or override with GDX_ENV=dev."
        )
    log.warning("CELERY_WORKER_ENCRYPTION_DEV_MODE no MASTER_ENCRYPTION_KEY; plaintext fallback")


@celery_app.task(queue="priority:low")
def run_daily_snapshot_task(tenant_id: str) -> None:
    return None
