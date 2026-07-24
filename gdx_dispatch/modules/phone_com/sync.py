"""Phone.com full-resync — pull historical call-logs + messages from Phone.com.

Used by:
- ``POST /api/settings/integrations/phone-com/sync-now`` (user-initiated
  catch-up after they paste a token).
- The token-health periodic task (Sprint pc-s15) for incremental refresh.

Strategy:
- Open ``PhoneComClient`` for the tenant via the stored token.
- Walk paginated ``list_calls`` (newest-first per Phone.com default sort)
  with ``paginate(method)`` from pc-s3 — caps at 10000 items by default.
- Same for ``list_messages``.
- Each item routes through ``upserts.upsert_call`` /
  ``upserts.upsert_message`` — keyed on Phone.com object id, so re-running
  is idempotent.
- Voicemail data is inline on call payloads when present (call_log row
  carries ``voicemail_url`` + ``voicemail_transcript``); upsert call first,
  then synthesize a voicemail upsert for any call whose payload has a
  ``voicemail_url``.

Returns a count summary the UI can show ("synced 23 calls, 8 messages").
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.modules.phone_com import key_storage, upserts
from gdx_dispatch.modules.phone_com.client import PhoneComAPIError, PhoneComClient
from gdx_dispatch.models.tenant_models import AppSettings

log = logging.getLogger("gdx_dispatch.modules.phone_com.sync")


def _open_tenant_session(control_db: Session, tenant_id: UUID) -> Session | None:
    return SessionLocal()


def _known_extension_ids(tenant_db: Session) -> list[str]:
    from gdx_dispatch.modules.phone_com.models import PhoneComExtension

    rows = tenant_db.query(PhoneComExtension.phone_com_extension_id).all()
    return [r[0] for r in rows]


def _sync_messages_for_extensions(
    tenant_db: Session,
    client: PhoneComClient,
    extension_ids: list[str],
    *,
    cap: int,
    from_epoch: int | None = None,
) -> int:
    """Walk ``/extensions/{id}/messages`` for each extension and upsert.

    Per-extension failures are non-fatal — one broken extension must not
    hide the others' messages. Message ids are account-unique, so the
    upsert key dedupes any overlap.
    """
    if not extension_ids:
        extension_ids = _known_extension_ids(tenant_db)
    synced = 0
    for ext_id in extension_ids:
        try:
            for item in client.paginate(
                client.list_messages,
                limit=50,
                extension_id=ext_id,
                from_epoch=from_epoch,
            ):
                if synced >= cap:
                    log.warning(
                        "phone_com_sync: message cap %d hit at extension=%s — "
                        "remaining extensions skipped this run",
                        cap, ext_id,
                    )
                    return synced
                upserts.upsert_message(tenant_db, item)
                synced += 1
        except PhoneComAPIError:
            log.exception(
                "phone_com_sync: list_messages failed for extension=%s (non-fatal)",
                ext_id,
            )
        except IntegrityError:
            # The 10-min poll overlapping the nightly resync can race the
            # same new message id through query-then-insert. Roll back and
            # move on — the loser's rows arrive on its next run. Broader
            # SQLAlchemyError (e.g. a dead DB) must NOT be swallowed here,
            # or a hard outage would report ok=True with 0 synced forever.
            log.exception(
                "phone_com_sync: upsert race for extension=%s (non-fatal)",
                ext_id,
            )
            tenant_db.rollback()
    return synced


def _build_client(control_db: Session, tenant_id: UUID, tenant_db: Session) -> PhoneComClient | None:
    token = key_storage.get_token(control_db, tenant_id)
    if token is None:
        return None
    app = tenant_db.query(AppSettings).first()
    voip_id_raw = app.phone_com_voip_id if app else None
    if voip_id_raw is None:
        return None
    try:
        voip_id = int(voip_id_raw)
    except (TypeError, ValueError):
        log.debug(
            "phone_com.sync_voip_id_unparseable tenant_id=%s raw=%r",
            tenant_id,
            voip_id_raw,
        )
        return None
    return PhoneComClient(token=token, voip_id=voip_id)


def run_full_resync(
    control_db: Session,
    tenant_id: UUID,
    *,
    cap: int = 5000,
) -> dict[str, Any]:
    """Pull full history from Phone.com into this tenant's DB.

    Returns ``{ok, calls_synced, messages_synced, voicemails_synced, error?}``.
    Idempotent — safe to re-run.
    """
    tenant_db = _open_tenant_session(control_db, tenant_id)
    if tenant_db is None:
        return {
            "ok": False,
            "error": "tenant db unavailable",
            "calls_synced": 0,
            "messages_synced": 0,
            "voicemails_synced": 0,
        }

    try:
        client = _build_client(control_db, tenant_id, tenant_db)
        if client is None:
            return {
                "ok": False,
                "error": "phone_com integration not configured",
                "calls_synced": 0,
                "messages_synced": 0,
                "voicemails_synced": 0,
            }

        calls_synced = 0
        messages_synced = 0
        voicemails_synced = 0

        try:
            with client:
                # 1. Calls (also harvests inline voicemail rows).
                # Phone.com defaults to a ~30-day window when no
                # filters[start_time] is set. Use a 2000-01-01 anchor to
                # pull effectively-everything (Phone.com 422s on gt:0).
                for item in client.paginate(client.list_calls, limit=50, from_epoch=946684800):
                    if calls_synced >= cap:
                        break
                    upserts.upsert_call(tenant_db, item)
                    calls_synced += 1

                    # Inline voicemail on the call payload — synthesize a
                    # voicemail row keyed on the call's id when a
                    # voicemail_url or transcript is present. Carry the
                    # cp_url through raw_payload so the audio proxy can
                    # use the presigned (unauthed) variant.
                    if (
                        item.get("voicemail_url")
                        or item.get("voicemail_cp_url")
                        or item.get("voicemail_transcript")
                    ):
                        vm_payload = {
                            "id": f"vm-from-call-{item.get('id')}",
                            "call_id": item.get("id"),
                            "audio_url": (
                                item.get("voicemail_url")
                                or item.get("voicemail_cp_url")
                            ),
                            "voicemail_cp_url": item.get("voicemail_cp_url"),
                            "transcript": item.get("voicemail_transcript"),
                            "duration": item.get("voicemail_duration"),
                        }
                        upserts.upsert_voicemail(tenant_db, vm_payload)
                        voicemails_synced += 1

                # 2. Extensions (Wave C / S4) — synced BEFORE messages since
                # messages are pulled per-extension. Small list; `paginate`
                # handles edge cases. Also surfaces the set of dialable
                # extensions for the Settings card's default_extension pick.
                extension_ids: list[str] = []
                try:
                    for item in client.paginate(client.list_extensions, limit=50):
                        ext_row = upserts.upsert_extension(tenant_db, item)
                        if ext_row is not None:
                            extension_ids.append(ext_row.phone_com_extension_id)
                except PhoneComAPIError:
                    log.exception("phone_com_sync: list_extensions failed (non-fatal)")

                # 3. SMS / messages — per-extension. Phone.com's account-level
                # /messages endpoint returns total=0 even when extensions hold
                # messages (verified against prod 2026-07-23: 0 at account
                # scope, 128 under /extensions/{id}/messages), so walking
                # extensions is the only way to see the SMS inbox.
                messages_synced = _sync_messages_for_extensions(
                    tenant_db, client, extension_ids, cap=cap,
                )

                # 4. Phone numbers (Wave C / S3) — populates the dropdown that
                # replaces free-text default_caller_id on the Settings card.
                try:
                    for item in client.paginate(client.list_phone_numbers, limit=50):
                        upserts.upsert_phone_number(tenant_db, item)
                except PhoneComAPIError:
                    log.exception("phone_com_sync: list_phone_numbers failed (non-fatal)")

                # 5. Faxes (P2.7) — direction in only by default; the call
                # path here mirrors calls/messages and is idempotent.
                try:
                    for item in client.paginate(client.list_faxes, limit=50):
                        upserts.upsert_fax(tenant_db, item)
                except PhoneComAPIError:
                    log.exception("phone_com_sync: list_faxes failed (non-fatal)")
        except PhoneComAPIError as exc:
            log.exception("phone_com_sync upstream error tenant=%s", tenant_id)
            return {
                "ok": False,
                "error": str(exc),
                "calls_synced": calls_synced,
                "messages_synced": messages_synced,
                "voicemails_synced": voicemails_synced,
            }

        # Stamp app_settings.phone_com_last_synced_at for UI freshness
        # display (Wave B / S17). Tolerate AppSettings rows from tenants
        # paved before the column was added.
        try:
            app = tenant_db.query(AppSettings).first()
            if app is not None and hasattr(app, "phone_com_last_synced_at"):
                app.phone_com_last_synced_at = datetime.now(timezone.utc)
                tenant_db.commit()
        except Exception:  # noqa: BLE001
            log.exception("phone_com_sync: stamp last_synced_at failed tenant=%s", tenant_id)
            tenant_db.rollback()

        # Refresh recent stats roll-up (Wave D / S6). Last 7 days only —
        # historical backfill happens via the one-shot roll_up_all_history.
        try:
            from gdx_dispatch.modules.phone_com.stats import roll_up_recent

            roll_up_recent(tenant_db, days=7)
        except Exception:  # noqa: BLE001
            log.exception("phone_com_sync: stats roll-up failed tenant=%s (non-fatal)", tenant_id)
            tenant_db.rollback()

        # Wave E / S10: a successful sync round-trip is the strongest possible
        # signal that the token is still valid — stamp last_validated_at so the
        # UI self-heals even if Re-test never gets clicked.
        try:
            from gdx_dispatch.modules.phone_com import key_storage as _ks

            _ks.mark_validated(control_db, tenant_id)
        except Exception:  # noqa: BLE001
            log.exception("phone_com_sync: mark_validated failed (non-fatal)")

        log.info(
            "phone_com_sync ok tenant=%s calls=%d messages=%d voicemails=%d",
            tenant_id, calls_synced, messages_synced, voicemails_synced,
        )
        return {
            "ok": True,
            "calls_synced": calls_synced,
            "messages_synced": messages_synced,
            "voicemails_synced": voicemails_synced,
        }
    finally:
        tenant_db.close()


def sync_recent_messages(
    control_db: Session,
    tenant_id: UUID,
    *,
    window_hours: int = 48,
    cap: int = 1000,
) -> dict[str, Any]:
    """Messages-only frequent poll — the de-facto live path for the SMS inbox.

    Phone.com's account-level listeners have never delivered a webhook to us
    (verified against prod nginx 2026-07-23), so without this the inbox only
    updates at the 03:45 UTC nightly resync. Windowed to the last
    ``window_hours`` so each run is a handful of small requests; the nightly
    full resync remains the completeness backstop.
    """
    tenant_db = _open_tenant_session(control_db, tenant_id)
    if tenant_db is None:
        return {"ok": False, "error": "tenant db unavailable", "messages_synced": 0}
    try:
        client = _build_client(control_db, tenant_id, tenant_db)
        if client is None:
            return {
                "ok": False,
                "error": "phone_com integration not configured",
                "messages_synced": 0,
            }
        from_epoch = int(
            (datetime.now(timezone.utc) - timedelta(hours=window_hours)).timestamp()
        )
        with client:
            synced = _sync_messages_for_extensions(
                tenant_db, client, [], cap=cap, from_epoch=from_epoch,
            )
        log.info(
            "phone_com_sync_recent_messages ok tenant=%s messages=%d window_h=%d",
            tenant_id, synced, window_hours,
        )
        return {"ok": True, "messages_synced": synced}
    except PhoneComAPIError as exc:
        log.exception("phone_com_sync_recent_messages upstream error tenant=%s", tenant_id)
        return {"ok": False, "error": str(exc), "messages_synced": 0}
    finally:
        tenant_db.close()
