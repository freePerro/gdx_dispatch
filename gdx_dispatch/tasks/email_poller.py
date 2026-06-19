from __future__ import annotations

import imaplib
import json
import logging
import os
from email import message_from_bytes
from email.message import Message
from email.utils import parseaddr
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gdx_dispatch.core.audit import log_audit_event_sync
from gdx_dispatch.core.celery_app import celery_app
from gdx_dispatch.core.database import SessionLocal
from gdx_dispatch.core.pii import HashColumn

log = logging.getLogger(__name__)


def _normalize_sender_email(raw_from: str | None) -> str:
    _, email = parseaddr(raw_from or "")
    return email.strip().lower()


def _message_text(msg: Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            content_type = str(part.get_content_type() or "").lower()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True) or b""
                return payload.decode(errors="replace").strip()
        return ""

    payload = msg.get_payload(decode=True)
    if payload is None:
        return str(msg.get_payload() or "").strip()
    return payload.decode(errors="replace").strip()


def _ensure_inbound_email_table(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS inbound_emails (
                id TEXT PRIMARY KEY,
                customer_id TEXT NULL,
                message_id TEXT NOT NULL UNIQUE,
                sender_email TEXT NOT NULL,
                subject TEXT NULL,
                body_text TEXT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
    )
    db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_inbound_emails_message_id ON inbound_emails (message_id)"))
    db.commit()


def _load_imap_config(db: Session) -> dict[str, Any] | None:
    raw = db.execute(text("SELECT integrations FROM app_settings LIMIT 1")).scalar_one_or_none()
    if raw is None:
        return None

    integrations: dict[str, Any]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            integrations = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            log.warning("email_poller: tenant integrations JSON malformed — skipping")
            return None
    elif isinstance(raw, dict):
        integrations = raw
    else:
        return None

    imap_cfg = integrations.get("imap")
    if not isinstance(imap_cfg, dict):
        return None

    required = ("host", "username", "password")
    if any(not str(imap_cfg.get(key, "")).strip() for key in required):
        return None
    return imap_cfg


def _match_customer_id(db: Session, sender_email: str) -> str | None:
    if not sender_email:
        return None
    row = db.execute(
        text("SELECT id FROM customers WHERE email_hash = :email_hash LIMIT 1"),
        {"email_hash": HashColumn.hash_for_search(sender_email)},
    ).mappings().first()
    return str(row["id"]) if row else None


def _is_duplicate_message(db: Session, message_id: str) -> bool:
    existing = db.execute(
        text("SELECT 1 FROM inbound_emails WHERE message_id = :message_id LIMIT 1"),
        {"message_id": message_id},
    ).scalar_one_or_none()
    return bool(existing)


def _mark_as_seen(imap_client: Any, msg_num: bytes) -> None:
    imap_client.store(msg_num, "+FLAGS", "\\Seen")


def _open_imap_connection(imap_cfg: dict[str, Any]) -> Any:
    host = str(imap_cfg.get("host"))
    port = int(imap_cfg.get("port") or 993)
    use_ssl = bool(imap_cfg.get("use_ssl", True))
    if use_ssl:
        return imaplib.IMAP4_SSL(host, port)
    return imaplib.IMAP4(host, port)


def _audit(db: Session, tenant_id: str, action: str, entity_id: str, details: dict[str, Any]) -> None:
    log_audit_event_sync(
        db=db,
        tenant_id=tenant_id,
        user_id="system",
        action=action,
        entity_type="email_poller",
        entity_id=entity_id,
        details=details,
    )
    db.commit()


def _process_single_email(
    db: Session,
    imap_client: Any,
    msg_num: bytes,
    tenant_id: str,
) -> dict[str, int]:
    result = {"processed": 0, "duplicates": 0, "errors": 0}
    try:
        status, payload = imap_client.fetch(msg_num, "(RFC822)")
        if status != "OK" or not payload:
            return result

        raw_email = payload[0][1]
        parsed = message_from_bytes(raw_email)
        sender_email = _normalize_sender_email(parsed.get("From"))
        message_id = str(parsed.get("Message-ID") or f"<{tenant_id}-{msg_num.decode(errors='ignore')}>").strip()
        subject = str(parsed.get("Subject") or "").strip()
        body_text = _message_text(parsed)

        if _is_duplicate_message(db, message_id):
            _mark_as_seen(imap_client, msg_num)
            _audit(
                db,
                tenant_id,
                action="inbound_email_duplicate_skipped",
                entity_id=message_id,
                details={"sender_email": sender_email},
            )
            result["duplicates"] += 1
            return result

        customer_id = _match_customer_id(db, sender_email)
        db.execute(
            text(
                """
                INSERT INTO inbound_emails (id, customer_id, message_id, sender_email, subject, body_text, created_at)
                VALUES (:id, :customer_id, :message_id, :sender_email, :subject, :body_text, CURRENT_TIMESTAMP)
                """
            ),
            {
                "id": str(uuid4()),
                "customer_id": customer_id,
                "message_id": message_id,
                "sender_email": sender_email,
                "subject": subject,
                "body_text": body_text,
            },
        )
        db.commit()

        _mark_as_seen(imap_client, msg_num)
        _audit(
            db,
            tenant_id,
            action="inbound_email_created",
            entity_id=message_id,
            details={"sender_email": sender_email, "customer_id": customer_id},
        )
        result["processed"] += 1
        return result
    except IntegrityError:
        log.exception("email_poller_duplicate_insert_race", extra={"tenant_id": tenant_id})
        db.rollback()
        try:
            _mark_as_seen(imap_client, msg_num)
        except Exception:
            log.exception("email_poller_mark_seen_failed_after_integrity", extra={"tenant_id": tenant_id})
        result["duplicates"] += 1
        return result
    except Exception:
        log.exception("email_poller_message_processing_failed", extra={"tenant_id": tenant_id})
        db.rollback()
        result["errors"] += 1
        return result


@celery_app.task(queue="priority:low")
def poll_inbound_emails() -> dict[str, int]:
    tenant_id = os.getenv("GDX_TENANT_ID") or os.getenv("GDX_DEFAULT_TENANT_ID") or "gdx"
    stats: dict[str, int] = {"processed": 0, "duplicates": 0, "skipped": 0, "errors": 0}

    db = SessionLocal()
    try:
        _ensure_inbound_email_table(db)
        imap_cfg = _load_imap_config(db)
        if not imap_cfg:
            _audit(
                db,
                tenant_id,
                action="email_poller_skipped",
                entity_id=tenant_id,
                details={"reason": "imap_config_missing"},
            )
            stats["skipped"] += 1
            return stats

        try:
            imap_client = _open_imap_connection(imap_cfg)
            imap_client.login(str(imap_cfg["username"]), str(imap_cfg["password"]))
        except Exception:
            log.exception("email_poller_imap_connection_failed", extra={"tenant_id": tenant_id})
            _audit(
                db,
                tenant_id,
                action="email_poller_connection_failed",
                entity_id=tenant_id,
                details={"host": str(imap_cfg.get("host", ""))},
            )
            stats["errors"] += 1
            return stats

        try:
            mailbox = str(imap_cfg.get("mailbox") or "INBOX")
            imap_client.select(mailbox)
            search_status, search_data = imap_client.search(None, "UNSEEN")
            if search_status == "OK" and search_data:
                message_numbers = (search_data[0] or b"").split()
                tenant_processed = tenant_duplicates = tenant_errors = 0
                for msg_num in message_numbers:
                    message_result = _process_single_email(db, imap_client, msg_num, tenant_id)
                    stats["processed"] += message_result["processed"]
                    stats["duplicates"] += message_result["duplicates"]
                    stats["errors"] += message_result["errors"]
                    tenant_processed += message_result["processed"]
                    tenant_duplicates += message_result["duplicates"]
                    tenant_errors += message_result["errors"]
                _audit(
                    db,
                    tenant_id,
                    action="email_poller_cycle_completed",
                    entity_id=tenant_id,
                    details={
                        "processed": tenant_processed,
                        "duplicates": tenant_duplicates,
                        "errors": tenant_errors,
                    },
                )
        finally:
            try:
                imap_client.logout()
            except Exception:
                log.exception("email_poller_imap_logout_failed", extra={"tenant_id": tenant_id})

    except Exception:
        log.exception("email_poller_failed", extra={"tenant_id": tenant_id})
        db.rollback()
        stats["errors"] += 1
    finally:
        db.close()

    return stats
