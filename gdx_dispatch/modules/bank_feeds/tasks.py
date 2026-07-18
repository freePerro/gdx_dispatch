"""Bank Feeds Celery tasks — scheduled + manual sync across N institutions.

Failure isolation (audited plan S4/S5): every institution syncs
independently — a dead bank (auth failure, rate limit, open circuit)
never blocks the others. ``BannoRateLimitError`` is caught PER
INSTITUTION; the task only retries itself when NOTHING completed, so a
healthy bank's completed work is never re-burned by a broken sibling.
``record_scheduled_run`` records "partial" when any institution errored
(deliberately not cloning the QB dispatcher's always-"ok" behavior).

Per-institution circuit breakers (``bank_api:{institution_id}``) can't
join the static KNOWN_SERVICES admin registry (dynamic names) — their
state is exposed through ``GET /api/bank-feeds/status`` instead.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal

_task_log = logging.getLogger(__name__)

BREAKER_FAILURE_THRESHOLD = 5
BREAKER_RECOVERY_TIMEOUT_S = 300


def _tenant_session(tenant_id: str):
    return SessionLocal()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _tables_present(db) -> bool:
    try:
        names = set(sa_inspect(db.get_bind()).get_table_names())
    except Exception:  # noqa: BLE001
        return False
    return {"banno_institutions", "banno_connections", "bank_feed_accounts"} <= names


def _institution_breaker(institution_id: str):
    from gdx_dispatch.core.circuit_breaker import CircuitBreaker  # noqa: PLC0415

    return CircuitBreaker(
        f"bank_api:{institution_id}",
        failure_threshold=BREAKER_FAILURE_THRESHOLD,
        recovery_timeout=BREAKER_RECOVERY_TIMEOUT_S,
    )


def breaker_state(institution_id: str) -> str:
    """Best-effort breaker state for /status. 'CLOSED' when Redis is
    unavailable (dev/tests) — the breaker is then a no-op anyway."""
    try:
        return str(asyncio.run(_institution_breaker(institution_id).get_state()).value)
    except Exception:  # noqa: BLE001
        return "CLOSED"


def _breaker_open(institution_id: str) -> bool:
    try:
        from gdx_dispatch.core.circuit_breaker import CircuitState  # noqa: PLC0415

        return asyncio.run(_institution_breaker(institution_id).get_state()) == CircuitState.OPEN
    except Exception:  # noqa: BLE001
        return False


def _breaker_record(institution_id: str, *, success: bool) -> None:
    try:
        breaker = _institution_breaker(institution_id)
        if success:
            asyncio.run(breaker.record_success())
        else:
            asyncio.run(breaker.record_failure())
    except Exception:  # noqa: BLE001
        pass


def _sync_one_institution(
    db, institution, *, force_fetch: bool
) -> dict:
    """Sync every active connection of one institution. Returns a result
    dict; raises nothing except BannoRateLimitError (caller isolates)."""
    from gdx_dispatch.modules.bank_feeds import oauth, service
    from gdx_dispatch.modules.bank_feeds.client import BannoClient
    from gdx_dispatch.modules.bank_feeds.models import (
        AUTH_DISCONNECTED,
        AUTH_HEALTHY,
        BankFeedAccount,
        BannoConnection,
    )

    result: dict = {"institution_id": str(institution.id), "accounts": {}, "errors": []}
    connections = db.execute(
        select(BannoConnection).where(
            BannoConnection.institution_id == institution.id,
            BannoConnection.auth_state != AUTH_DISCONNECTED,
        )
    ).scalars().all()
    if not connections:
        result["skipped_no_connection"] = True
        return result

    schedule = service.get_or_create_schedule(db)
    backfill_days = schedule.backfill_days

    for connection in connections:
        if connection.auth_state != AUTH_HEALTHY:
            _task_log.info(
                "bank_feeds_sync_skipped_unhealthy connection=%s state=%s",
                connection.id, connection.auth_state,
            )
            result["errors"].append({"connection": str(connection.id), "skipped_unhealthy": True})
            continue

        def _token_provider(stale_token: str | None = None, _cid=connection.id) -> str:
            return oauth.get_valid_access_token(db, _cid, stale_token=stale_token)

        with BannoClient(connection.fi_host, _token_provider) as client:
            if force_fetch:
                task_id = client.trigger_fetch(connection.banno_user_id)
                if task_id:
                    client.wait_for_fetch(connection.banno_user_id, task_id)

            result["accounts_summary"] = service.sync_accounts(db, client, connection)

            accounts = db.execute(
                select(BankFeedAccount).where(
                    BankFeedAccount.connection_id == connection.id,
                    BankFeedAccount.sync_enabled.is_(True),
                    BankFeedAccount.is_inactive.is_(False),
                )
            ).scalars().all()
            for account in accounts:
                try:
                    result["accounts"][account.external_account_id] = (
                        service.sync_account_transactions(
                            db, client, connection, account, backfill_days=backfill_days
                        )
                    )
                except Exception as exc:
                    from gdx_dispatch.modules.bank_feeds.client import BannoRateLimitError  # noqa: PLC0415

                    if isinstance(exc, BannoRateLimitError):
                        raise
                    db.rollback()
                    _task_log.exception(
                        "bank_feeds_account_sync_failed account=%s", account.external_account_id
                    )
                    result["errors"].append({
                        "account": account.external_account_id,
                        "error_class": exc.__class__.__name__,
                    })

            try:
                result["documents"] = service.sync_documents(
                    db, client, connection,
                    backfill_days=backfill_days, force_probe=force_fetch,
                )
            except Exception as exc:
                from gdx_dispatch.modules.bank_feeds.client import BannoRateLimitError  # noqa: PLC0415

                if isinstance(exc, BannoRateLimitError):
                    raise
                db.rollback()
                _task_log.exception("bank_feeds_documents_sync_failed connection=%s", connection.id)
                result["errors"].append({"documents": True, "error_class": exc.__class__.__name__})

    return result


@celery_app.task(bind=True, max_retries=3, queue="priority:low")
def bank_feeds_sync_task(
    self, tenant_id: str, force_fetch: bool = False, institution_id: str | None = None
) -> dict:
    from gdx_dispatch.modules.bank_feeds import service
    from gdx_dispatch.modules.bank_feeds.client import BannoRateLimitError
    from gdx_dispatch.modules.bank_feeds.models import BannoInstitution

    with _tenant_session(tenant_id) as db:
        if not _tables_present(db):
            return {"skipped_no_tables": True}

        query = select(BannoInstitution).where(BannoInstitution.enabled.is_(True))
        if institution_id:
            query = select(BannoInstitution).where(BannoInstitution.id == institution_id)
        institutions = db.execute(query.order_by(BannoInstitution.created_at.asc())).scalars().all()
        if not institutions:
            return {"skipped_no_institutions": True}

        results: dict = {}
        completed = 0
        errored = 0
        rate_limited = 0

        for institution in institutions:
            inst_key = str(institution.id)
            if _breaker_open(inst_key):
                _task_log.info("bank_feeds_sync_skipped_circuit_open institution=%s", inst_key)
                results[inst_key] = {"skipped_circuit_open": True}
                continue
            try:
                inst_result = _sync_one_institution(db, institution, force_fetch=force_fetch)
                results[inst_key] = inst_result
                if inst_result.get("errors"):
                    errored += 1
                else:
                    completed += 1
                _breaker_record(inst_key, success=not inst_result.get("errors"))
            except BannoRateLimitError:
                db.rollback()
                rate_limited += 1
                results[inst_key] = {"rate_limited": True}
                _task_log.warning("bank_feeds_sync_rate_limited institution=%s", inst_key)
                # Rate limit is not a breaker "failure" — the API is alive.
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                errored += 1
                results[inst_key] = {"error_class": exc.__class__.__name__}
                _breaker_record(inst_key, success=False)
                _task_log.exception("bank_feeds_sync_institution_failed institution=%s", inst_key)

        if completed and not errored and not rate_limited:
            status = "ok"
        elif completed:
            status = "partial"
        elif not completed and not errored and not rate_limited:
            # Every institution was skipped (circuit open / no connection) —
            # nothing was attempted, so don't record a scary "error".
            status = "skipped"
        elif rate_limited and not completed and not errored:
            # Nothing at all completed because of rate limiting — retry the
            # whole task with backoff; leave the schedule untouched so
            # record_scheduled_run doesn't paper over the gap.
            raise self.retry(countdown=min(2 ** self.request.retries * 30, 600))
        else:
            status = "error"
        try:
            service.record_scheduled_run(
                db, status,
                None if status == "ok" else str({k: v for k, v in results.items() if v.get("errors") or v.get("error_class") or v.get("rate_limited")})[:400],
            )
        except Exception:  # noqa: BLE001
            db.rollback()
            _task_log.exception("bank_feeds_record_scheduled_run_failed")

        return {"status": status, "results": results}


@celery_app.task(queue="priority:low")
def bank_feeds_schedule_dispatcher() -> dict:
    """Beat-fired every 5 minutes. Advances next_run_at BEFORE queueing so
    a slow sync (> one tick) can't be double-dispatched; record_scheduled_run
    then rolls forward from real completion."""
    from gdx_dispatch.core.tenant import single_tenant
    from gdx_dispatch.modules.bank_feeds import service
    from gdx_dispatch.modules.bank_feeds.models import FREQ_MANUAL

    _t = single_tenant()
    queued: list[str] = []
    skipped = 0

    for tid in [str(_t["id"])]:
        try:
            with _tenant_session(tid) as db:
                if not _tables_present(db):
                    continue
                schedule = service.get_or_create_schedule(db)
                next_run_at = _aware(schedule.next_run_at)
                if schedule.frequency == FREQ_MANUAL or next_run_at is None:
                    skipped += 1
                    continue
                if next_run_at > datetime.now(timezone.utc):
                    skipped += 1
                    continue
                schedule.next_run_at = service.compute_next_run_at(schedule.frequency)
                schedule.updated_at = datetime.now(timezone.utc)
                db.commit()
            bank_feeds_sync_task.delay(tid)
            queued.append(tid)
        except Exception:  # noqa: BLE001
            _task_log.exception("bank_feeds_dispatcher_failed tenant=%s", tid)

    return {"queued": queued, "skipped": skipped}
