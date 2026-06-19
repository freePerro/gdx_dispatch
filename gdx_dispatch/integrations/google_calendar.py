"""Google Calendar sync integration for GDX.

Syncs job appointments with Google Calendar using the Calendar API v3.
Requires google-api-python-client and google-auth.

Usage:
    from gdx_dispatch.integrations.google_calendar import GoogleCalendarSync
    sync = GoogleCalendarSync(access_token="...", refresh_token="...", client_id="...", client_secret="...")
    event = sync.create_event(summary="Spring Replacement", start="2026-04-05T09:00:00", end="2026-04-05T11:00:00")
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)


class GoogleCalendarSync:
    """Wraps Google Calendar API v3 for job/appointment sync."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str = "",
        client_id: str = "",
        client_secret: str = "",
        calendar_id: str = "primary",
    ):
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._calendar_id = calendar_id
        self._service = None

    def _get_service(self) -> Any:
        if self._service:
            return self._service
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build

            creds = Credentials(
                token=self._access_token,
                refresh_token=self._refresh_token,
                client_id=self._client_id,
                client_secret=self._client_secret,
                token_uri="https://oauth2.googleapis.com/token",
            )
            self._service = build("calendar", "v3", credentials=creds)
            return self._service
        except ImportError:
            log.warning("google-api-python-client not installed — calendar sync disabled")
            return None

    def create_event(
        self,
        summary: str,
        start: str,
        end: str | None = None,
        description: str = "",
        location: str = "",
        attendees: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a calendar event."""
        service = self._get_service()
        if not service:
            return {"id": None, "status": "disabled", "reason": "google-api not available"}

        if not end:
            start_dt = datetime.fromisoformat(start)
            end = (start_dt + timedelta(hours=2)).isoformat()

        event_body: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "location": location,
            "start": {"dateTime": start, "timeZone": "America/Chicago"},
            "end": {"dateTime": end, "timeZone": "America/Chicago"},
        }
        if attendees:
            event_body["attendees"] = [{"email": e} for e in attendees]

        try:
            event = service.events().insert(
                calendarId=self._calendar_id, body=event_body
            ).execute()
            log.info("gcal_event_created", extra={"event_id": event.get("id"), "summary": summary})
            return {"id": event.get("id"), "status": "created", "html_link": event.get("htmlLink")}
        except Exception:
            log.exception("gcal_create_event_failed", extra={"summary": summary})
            return {"id": None, "status": "error", "reason": "API call failed"}

    def update_event(
        self,
        event_id: str,
        summary: str | None = None,
        start: str | None = None,
        end: str | None = None,
        description: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        """Update an existing calendar event."""
        service = self._get_service()
        if not service:
            return {"id": event_id, "status": "disabled"}

        try:
            existing = service.events().get(
                calendarId=self._calendar_id, eventId=event_id
            ).execute()

            if summary is not None:
                existing["summary"] = summary
            if description is not None:
                existing["description"] = description
            if location is not None:
                existing["location"] = location
            if start is not None:
                existing["start"] = {"dateTime": start, "timeZone": "America/Chicago"}
            if end is not None:
                existing["end"] = {"dateTime": end, "timeZone": "America/Chicago"}

            updated = service.events().update(
                calendarId=self._calendar_id, eventId=event_id, body=existing
            ).execute()
            return {"id": updated.get("id"), "status": "updated"}
        except Exception:
            log.exception("gcal_update_event_failed", extra={"event_id": event_id})
            return {"id": event_id, "status": "error"}

    def delete_event(self, event_id: str) -> dict[str, Any]:
        """Delete a calendar event."""
        service = self._get_service()
        if not service:
            return {"id": event_id, "status": "disabled"}

        try:
            service.events().delete(
                calendarId=self._calendar_id, eventId=event_id
            ).execute()
            return {"id": event_id, "status": "deleted"}
        except Exception:
            log.exception("gcal_delete_event_failed", extra={"event_id": event_id})
            return {"id": event_id, "status": "error"}

    def list_events(
        self,
        time_min: str | None = None,
        time_max: str | None = None,
        max_results: int = 50,
    ) -> list[dict[str, Any]]:
        """List calendar events."""
        service = self._get_service()
        if not service:
            return []

        try:
            if not time_min:
                time_min = datetime.now(timezone.utc).isoformat()
            if not time_max:
                time_max = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

            result = service.events().list(
                calendarId=self._calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            return result.get("items", [])
        except Exception:
            log.exception("gcal_list_events_failed")
            return []

    def sync_job_to_calendar(self, job: dict[str, Any]) -> dict[str, Any]:
        """Sync a GDX job to Google Calendar."""
        title = job.get("title", "Job")
        customer = job.get("customer_name", "")
        address = job.get("address", "")
        scheduled = job.get("scheduled_start") or job.get("scheduled_at", "")
        notes = job.get("notes", "")

        summary = f"{title} - {customer}" if customer else title
        description = f"Job: {title}\nCustomer: {customer}\nNotes: {notes}"

        return self.create_event(
            summary=summary,
            start=scheduled,
            description=description,
            location=address,
        )
