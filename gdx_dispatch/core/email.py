from __future__ import annotations

import importlib
import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Any

logger = logging.getLogger(__name__)


def _import_boto3() -> Any:
    return importlib.import_module("boto3")


def _smtp_configured() -> bool:
    return all(
        [
            os.getenv("MAIL_SERVER"),
            os.getenv("MAIL_PORT"),
            os.getenv("MAIL_USERNAME"),
            os.getenv("MAIL_PASSWORD"),
        ]
    )


def _ses_configured() -> bool:
    return bool(os.getenv("AWS_REGION") and os.getenv("AWS_ACCESS_KEY"))


def send_email(
    to: str,
    subject: str,
    body_html: str,
    from_address: str,
    tenant_branding: dict[str, Any],
) -> dict[str, Any]:
    _ = tenant_branding

    provider = os.getenv("EMAIL_PROVIDER", "").strip().lower()
    if provider == "smtp":
        if not _smtp_configured():
            logger.warning("email not configured for provider=smtp")
            return {"sent": False, "reason": "not configured"}

        server = str(os.getenv("MAIL_SERVER"))
        port = int(str(os.getenv("MAIL_PORT")))
        username = str(os.getenv("MAIL_USERNAME"))
        password = str(os.getenv("MAIL_PASSWORD"))

        message = EmailMessage()
        message["From"] = from_address
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body_html, subtype="html")

        with smtplib.SMTP(server, port) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)

        return {"sent": True, "provider": "smtp"}

    if provider == "ses":
        if not _ses_configured():
            logger.warning("email not configured for provider=ses")
            return {"sent": False, "reason": "not configured"}

        boto3 = _import_boto3()
        ses = boto3.client(
            "ses",
            region_name=os.getenv("AWS_REGION"),
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY"),
        )
        response = ses.send_email(
            Source=from_address,
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Html": {"Data": body_html, "Charset": "UTF-8"}},
            },
        )
        return {
            "sent": True,
            "provider": "ses",
            "message_id": response.get("MessageId"),
        }

    logger.warning("email not configured: unsupported or missing EMAIL_PROVIDER")
    return {"sent": False, "reason": "not configured"}
