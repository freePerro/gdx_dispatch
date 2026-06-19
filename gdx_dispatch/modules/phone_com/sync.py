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
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.modules.phone_com import key_storage, upserts
from gdx_dispatch.modules.phone_com.client import PhoneComAPIError, PhoneComClient
from gdx_dispatch.models.tenant_models import AppSettings

log = logging.getLogger("gdx_dispatch.modules.phone_com.sync")


def _open_tenant_session(control_db: Session, tenant_id: UUID) -> Session | None:
    return SessionLocal()


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

                # 2. SMS / messages
                for item in client.paginate(client.list_messages, limit=50):
                    if messages_synced >= cap:
                        break
                    upserts.upsert_message(tenant_db, item)
                    messages_synced += 1

                # 3. Extensions (Wave C / S4) — small list, single page suffices
                # in practice, but `paginate` handles edge cases. Surfaces the
                # set of dialable extensions so the Settings card can pick a
                # real default_extension instead of a free-text guess.
                try:
                    for item in client.paginate(client.list_extensions, limit=50):
                        upserts.upsert_extension(tenant_db, item)
                except PhoneComAPIError:
                    log.exception("phone_com_sync: list_extensions failed (non-fatal)")

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
