from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from gdx_dispatch.core.celery_app import celery_app


@celery_app.task(queue="priority:high")
def send_appointment_reminder(appointment_id: str) -> dict[str, Any]:
    appointment = _get_appointment(appointment_id)
    if not appointment:
        return {"status": "not_found", "appointment_id": appointment_id}

    start_at = appointment["start_at"]
    now = _now_utc()
    hours_until = (start_at - now).total_seconds() / 3600
    if not _in_reminder_window(hours_until):
        return {
            "status": "skipped",
            "appointment_id": appointment_id,
            "hours_until": round(hours_until, 2),
        }

    phone = appointment.get("customer_phone")
    if not phone:
        return {"status": "missing_phone", "appointment_id": appointment_id}

    _send_sms(phone, _build_reminder_message(appointment, hours_until))
    return {
        "status": "sent",
        "appointment_id": appointment_id,
        "hours_until": round(hours_until, 2),
    }


@celery_app.task(queue="priority:high")
def check_upcoming_appointment_reminders() -> dict[str, int]:
    count = 0
    for appointment_id in _find_upcoming_appointment_ids():
        send_appointment_reminder.delay(appointment_id)
        count += 1
    return {"scheduled_count": count}


def _in_reminder_window(hours_until: float) -> bool:
    # Windows for 24-hour and 2-hour reminders.
    return 23.5 <= hours_until <= 24.5 or 1.5 <= hours_until <= 2.5


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _get_appointment(appointment_id: str) -> dict[str, Any] | None:
    # Stubbed for now; tests monkeypatch this.
    _ = appointment_id
    return None


def _find_upcoming_appointment_ids() -> list[str]:
    return []


def _send_sms(phone: str, message: str) -> None:
    _ = (phone, message)


def _build_reminder_message(appointment: dict[str, Any], hours_until: float) -> str:
    if hours_until <= 3:
        return f"Reminder: your appointment starts in about 2 hours (#{appointment['id']})."
    return f"Reminder: your appointment is tomorrow (#{appointment['id']})."
