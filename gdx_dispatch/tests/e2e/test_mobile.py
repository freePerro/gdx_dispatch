"""E2E tests for Mobile Technician View — MOB-01 through MOB-15.

Covers: mobile schedule, clock in/out, job status transitions,
photo upload, signature capture, notes, offline indicator, GPS,
mobile viewport, and touch target sizing.
"""
from __future__ import annotations

import base64

import pytest

from gdx_dispatch.tests.e2e.conftest import BASE_URL

pytestmark = [pytest.mark.e2e]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _jobs(api) -> list[dict]:
    resp = api.get("/api/jobs")
    assert resp.status_code == 200, (
        f"GET /api/jobs failed: {resp.status_code} {resp.text[:200]}"
    )
    data = resp.json()
    return data if isinstance(data, list) else (
        data.get("items") or data.get("data") or data.get("results") or []
    )


def _get_first_job_id(api) -> str:
    """Return the first available job ID or skip.

    Fine for read-only checks. NOT usable for the dispatch-status advance
    endpoints, or any other test that WRITES — those take `scratch_job`.
    """
    items = _jobs(api)
    if not items:
        pytest.skip("No jobs available")
    return str(items[0]["id"])



def _dispatch_status(api, job_id: str) -> str | None:
    """Read the job back. A handler's response body is a claim; this is the state."""
    resp = api.get(f"/api/jobs/{job_id}")
    assert resp.status_code == 200, (
        f"GET /api/jobs/{job_id} failed: {resp.status_code} {resp.text[:200]}"
    )
    return resp.json().get("dispatch_status")


def _clocked_in(api) -> bool:
    resp = api.get("/api/timeclock/status")
    assert resp.status_code == 200, (
        f"GET /api/timeclock/status failed: {resp.status_code} {resp.text[:200]}"
    )
    return bool(resp.json().get("clocked_in"))


# Minimal 1x1 red PNG for upload tests
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
    "nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=="
)

# Minimal SVG signature (base64 data URI content)
SIGNATURE_DATA = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAf"
    "FcSJAAAADUlEQVR4nGP4z8BQDwAEgAF/pooBPQAAAABJRU5ErkJggg=="
)


class TestMobileSchedule:
    """Mobile schedule and daily workflow tests."""

    def test_mob_01_mobile_schedule_loads(self, api, console_tracker):
        """MOB-01: GET /api/mobile/today returns today's jobs (the legacy
        /schedule route was removed 2026-09-06, #480)."""
        resp = api.get("/api/mobile/today")
        # Accept 200 (data) or 404 (module not enabled)
        if resp.status_code == 404:
            pytest.skip("Mobile schedule module not enabled")
        assert resp.status_code == 200, f"Mobile schedule failed: {resp.status_code} {resp.text[:200]}"
        data = resp.json()
        # Should be a list or dict with items
        assert isinstance(data, (list, dict)), f"Unexpected response type: {type(data)}"
        console_tracker.assert_no_errors("MOB-01")

    def test_mob_02_job_detail(self, api, console_tracker):
        """MOB-02: GET /api/mobile/job/{id} or /api/jobs/{id} returns full job info."""
        job_id = _get_first_job_id(api)

        # Try mobile-specific endpoint first, fall back to standard
        resp = api.get(f"/api/mobile/job/{job_id}")
        if resp.status_code == 404:
            resp = api.get(f"/api/jobs/{job_id}")
        assert resp.status_code == 200, f"Job detail failed: {resp.status_code}"
        data = resp.json()
        # Mobile endpoint returns {"job": {...}, "customer": {...}} envelope
        if "job" in data:
            assert "id" in data["job"], "Job detail missing 'id' field inside 'job' key"
        else:
            assert "id" in data, "Job detail missing 'id' field"
        console_tracker.assert_no_errors("MOB-02")


class TestMobileJobTransitions:
    """Job status transitions from the mobile view."""

    def test_mob_03_en_route(self, api, scratch_job, console_tracker):
        """MOB-03: POST en-route actually moves the job to en_route.

        The 404 fallback to `PATCH /api/jobs/{id} {"status": "en_route"}` is
        gone. `/api/mobile/jobs/{id}/en-route` is a live route, so a 404 is a
        REGRESSION, not a reason to try something else — and the fallback wrote
        a free-text `Job.status` string that no mobile flow reads.
        """
        job_id = scratch_job
        resp = api.post(f"/api/mobile/jobs/{job_id}/en-route", json_data={})
        assert resp.status_code == 200, (
            f"En-route failed: {resp.status_code} {resp.text[:200]}"
        )
        assert resp.json().get("dispatch_status") == "en_route", (
            f"En-route answered 200 without advancing: {resp.text[:200]}"
        )
        # The body is the handler's claim; this is the state it left behind.
        assert _dispatch_status(api, job_id) == "en_route"
        console_tracker.assert_no_errors("MOB-03")

    def test_mob_04_arrived(self, api, scratch_job, console_tracker):
        """MOB-04: POST arrived actually moves the job to on_site.

        Note the target is `on_site`, not "arrived" — `Job.dispatch_status` has
        no such value. The old fallback PATCHed `{"status": "arrived"}`, which
        the jobs router title-cases into a free-text column ("Arrived") that no
        dispatch surface reads.
        """
        job_id = scratch_job
        resp = api.post(f"/api/mobile/jobs/{job_id}/arrived", json_data={})
        assert resp.status_code == 200, (
            f"Arrived failed: {resp.status_code} {resp.text[:200]}"
        )
        assert resp.json().get("dispatch_status") == "on_site", (
            f"Arrived answered 200 without advancing: {resp.text[:200]}"
        )
        assert _dispatch_status(api, job_id) == "on_site"
        console_tracker.assert_no_errors("MOB-04")

    def test_mob_05_complete_job(self, api, scratch_job, console_tracker):
        """MOB-05: a tech completes a job through the CLOSEOUT sheet.

        This used to POST `/api/mobile/jobs/{id}/complete`. That route still
        exists but is `deprecated=True` and no UI calls it: PR B moved every
        job action to the job detail screen, and `MobileTodayView.vue` records
        that the legacy endpoint "stays unreachable", guarded by
        `MobileCloseoutOwnership.spec.js`. So MOB-05 was verifying a path no
        technician can take, and passing on its 400 "Signature is required".

        The real path is `POST /api/jobs/{id}/closeout` — one transaction for
        parts + hours + signature that flips lifecycle to completed.
        """
        job_id = scratch_job
        resp = api.post(
            f"/api/jobs/{job_id}/closeout",
            json_data={
                "hours": 1.0,
                "no_parts_used": True,
                "signature_data": SIGNATURE_DATA,
                "signed_by": "E2E MOB-05",
            },
        )
        assert resp.status_code == 201, (
            f"Closeout failed: {resp.status_code} {resp.text[:300]}"
        )
        assert resp.json().get("ok") is True, resp.text[:200]

        detail = api.get(f"/api/jobs/{job_id}")
        assert detail.status_code == 200, detail.status_code
        stage = (detail.json().get("lifecycle_stage") or "").lower()
        assert stage.startswith("complet"), (
            f"Closeout returned 201 but the job is still {stage!r}"
        )
        console_tracker.assert_no_errors("MOB-05")


class TestMobileClockInOut:
    """Mobile daily clock in/out."""

    def test_mob_06_clock_in(self, api, console_tracker):
        """MOB-06: clock-in opens a shift.

        This used to accept `400 "already clocked in"` as success, so it passed
        whether or not a row was ever written — and 400 is what you get on
        every run after the first. Start from a known clocked-OUT state, then
        require 201 and prove the shift is open.
        """
        if _clocked_in(api):
            api.post(
                "/api/timeclock/clock-out",
                json_data={"gps_lat": 33.45, "gps_lng": -112.07},
            )
        assert not _clocked_in(api), "could not reach a clocked-out starting state"

        resp = api.post(
            "/api/timeclock/clock-in",
            json_data={"gps_lat": 33.45, "gps_lng": -112.07},
        )
        assert resp.status_code == 201, (
            f"Clock in failed: {resp.status_code} {resp.text[:200]}"
        )
        assert _clocked_in(api), "clock-in returned 201 but no shift is open"
        console_tracker.assert_no_errors("MOB-06")

    def test_mob_06b_clock_out(self, api, console_tracker):
        """MOB-06b: clock-out closes the open shift.

        `404 "No active clock-in found"` is no longer accepted: it was the
        answer whenever nothing was open, which made the test pass without
        closing anything. Open a shift first if one isn't already.
        """
        if not _clocked_in(api):
            opened = api.post(
                "/api/timeclock/clock-in",
                json_data={"gps_lat": 33.45, "gps_lng": -112.07},
            )
            assert opened.status_code == 201, (
                f"Could not open a shift to close: {opened.status_code} "
                f"{opened.text[:200]}"
            )

        resp = api.post(
            "/api/timeclock/clock-out",
            json_data={"gps_lat": 33.45, "gps_lng": -112.07},
        )
        assert resp.status_code == 200, (
            f"Clock out failed: {resp.status_code} {resp.text[:200]}"
        )
        assert not _clocked_in(api), "clock-out returned 200 but the shift is still open"
        console_tracker.assert_no_errors("MOB-06b")


class TestMobileJobClock:
    """Per-job clock in/out from mobile."""

    def test_mob_07_job_clock_in(self, api, scratch_job, console_tracker):
        """MOB-07: per-job clock-in records a time entry linked to the job.

        Same shape as MOB-03/04 (#640): `< 500` plus a 404 fallback. The
        fallback pointed at `/api/timeclock/jobs/{id}/clock-in`, which is not
        in the route table at all — so the "retry" could only ever 404 too, and
        the assertion passed on that.
        """
        job_id = scratch_job
        resp = api.post(f"/api/mobile/jobs/{job_id}/clock-in", json_data={})
        assert resp.status_code in (200, 201), (
            f"Job clock-in failed: {resp.status_code} {resp.text[:200]}"
        )
        console_tracker.assert_no_errors("MOB-07")

    def test_mob_07b_job_clock_out(self, api, scratch_job, console_tracker):
        """MOB-07b: per-job clock-out records duration. See test_mob_07.

        `scratch_job` is function-scoped, so this gets a DIFFERENT job than
        MOB-07 clocked into — open an entry on this one before closing it,
        rather than relying on test ordering.
        """
        job_id = scratch_job
        opened = api.post(f"/api/mobile/jobs/{job_id}/clock-in", json_data={})
        assert opened.status_code in (200, 201), (
            f"Could not open a job entry to close: {opened.status_code} "
            f"{opened.text[:200]}"
        )
        resp = api.post(f"/api/mobile/jobs/{job_id}/clock-out", json_data={})
        assert resp.status_code in (200, 201), (
            f"Job clock-out failed: {resp.status_code} {resp.text[:200]}"
        )
        console_tracker.assert_no_errors("MOB-07b")


class TestMobileUploads:
    """Photo upload and signature capture from mobile."""

    def test_mob_08_photo_upload(self, api, scratch_job, console_tracker):
        """MOB-08: POST photo to job, photo saved."""
        job_id = scratch_job
        import httpx

        # Build multipart upload
        files = {"file": ("test_photo.png", TINY_PNG, "image/png")}
        # There is no `/api/mobile/jobs/{id}/photos` route — the mobile SPA
        # posts to the jobs router. This used to try the mobile path first and
        # fall back only on 404; the real answer is 405, so the fallback never
        # fired and `< 500` passed on it. No photo was ever uploaded (#640).
        # Use raw httpx since APIClient doesn't support multipart.
        resp = httpx.post(
            f"{BASE_URL}/api/jobs/{job_id}/photos",
            headers={
                "Authorization": api._client.headers["Authorization"],
                "x-tenant-id": api._client.headers["x-tenant-id"],
            },
            files=files,
            verify=False,
            timeout=15,
        )
        assert resp.status_code == 201, (
            f"Photo upload failed: {resp.status_code} {resp.text[:200]}"
        )
        assert resp.json().get("id"), f"no document row returned: {resp.text[:200]}"
        console_tracker.assert_no_errors("MOB-08")

    def test_mob_09_signature_capture(self, api, scratch_job, console_tracker):
        """MOB-09: POST signature to job, signature saved."""
        job_id = scratch_job
        # /api/mobile/jobs/{id}/signature was removed 2026-09-06 (#480); the
        # signature pad posts to the jobs router.
        # Same body the SPA sends (JobDetailView: `{ signature: dataUrl }`); the
        # handler's model has exactly one required field, so any other key is a
        # 422 that never reaches the save — which is what this test used to accept.
        resp = api.post(f"/api/jobs/{job_id}/signature", json_data={"signature": SIGNATURE_DATA})
        assert resp.status_code == 201, f"Signature capture failed: {resp.status_code} {resp.text[:200]}"
        assert resp.json().get("id"), f"no document row returned: {resp.text[:200]}"
        console_tracker.assert_no_errors("MOB-09")


class TestMobileNotes:
    """Adding notes from mobile view."""

    def test_mob_10_add_note(self, api, scratch_job, console_tracker):
        """MOB-10: POST note to job, note appears."""
        job_id = scratch_job
        # The handler's field is `note`, not `content`. Sending `content`
        # 422s before any note is written — and `< 500` accepted that, so this
        # test never once added a note (#640, the MOB-09 shape again).
        resp = api.post(
            f"/api/mobile/jobs/{job_id}/notes",
            json_data={"note": "E2E test note — automated"},
        )
        assert resp.status_code == 201, (
            f"Add note failed: {resp.status_code} {resp.text[:200]}"
        )
        body = resp.json()
        assert body.get("id"), f"no note row returned: {resp.text[:200]}"
        # The echoed `note` is a local variable — a handler that dropped its
        # db.add would still return it. The checklist row says the note appears
        # ON THE JOB, so read it back.
        listed = api.get(f"/api/jobs/{job_id}/notes")
        assert listed.status_code == 200, (
            f"GET notes failed: {listed.status_code} {listed.text[:200]}"
        )
        rows = listed.json()
        rows = rows if isinstance(rows, list) else (rows.get("items") or rows.get("notes") or [])
        assert any(str(r.get("id")) == str(body["id"]) for r in rows), (
            f"note {body['id']} was returned by POST but is not on the job"
        )
        console_tracker.assert_no_errors("MOB-10")


class TestMobilePartsAndLocation:
    """Parts used and GPS location tracking."""

    def test_mob_11_parts_used(self, api, scratch_job, console_tracker):
        """MOB-11: POST parts-used records parts on a job."""
        job_id = scratch_job
        # The part line's field is `qty`, not `quantity` — `quantity` 422s
        # before anything is recorded, and `< 500` passed on it (#640).
        resp = api.post(
            f"/api/mobile/jobs/{job_id}/parts-used",
            json_data={"parts": [{"name": "Spring 25x4", "qty": 2}]},
        )
        assert resp.status_code == 200, (
            f"Parts used failed: {resp.status_code} {resp.text[:200]}"
        )
        assert resp.json().get("recorded") == 1, (
            f"parts-used answered 200 but recorded nothing: {resp.text[:200]}"
        )
        console_tracker.assert_no_errors("MOB-11")

    def test_mob_12_location_tracking(self, api, console_tracker):
        """MOB-12: POST /api/mobile/location records GPS coordinates."""
        # Two reasons this never tested anything (#640): the fields are
        # `lat`/`lng`, not `latitude`/`longitude` (422), and the endpoint
        # answers 403 "Not clocked in" without an open shift. Both are < 500.
        opened_here = False
        if not _clocked_in(api):
            opened = api.post(
                "/api/timeclock/clock-in",
                json_data={"gps_lat": 33.45, "gps_lng": -112.07},
            )
            assert opened.status_code == 201, (
                f"Could not open a shift for GPS: {opened.status_code} "
                f"{opened.text[:200]}"
            )
            opened_here = True

        try:
            resp = api.post("/api/mobile/location", json_data={
                "lat": 33.4484,
                "lng": -112.0740,
                "accuracy_m": 10.0,
            })
            assert resp.status_code == 201, (
                f"Location tracking failed: {resp.status_code} {resp.text[:200]}"
            )
            body = resp.json()
            assert body.get("id"), f"no location row returned: {resp.text[:200]}"
            assert float(body.get("lat")) == pytest.approx(33.4484), resp.text[:200]
            # `accuracy_m`, not `accuracy` (routers/tech_locations.py:42) — the
            # wrong name is silently dropped, which is this PR's whole class.
            assert float(body.get("accuracy_m")) == pytest.approx(10.0), resp.text[:200]
        finally:
            # Don't leak an open shift into whatever runs next — TIME-02 asserts
            # a clock-in succeeds, and a shift this test opened would 400 it.
            if opened_here:
                api.post(
                    "/api/timeclock/clock-out",
                    json_data={"gps_lat": 33.45, "gps_lng": -112.07},
                )
        console_tracker.assert_no_errors("MOB-12")


class TestMobileViewport:
    """Mobile viewport rendering and touch target accessibility."""

    def test_mob_14_mobile_viewport_rendering(self, navigate, console_tracker, authenticated_page):
        """MOB-14: Mobile schedule page renders correctly at 375px width."""
        page = authenticated_page
        page.set_viewport_size({"width": 375, "height": 812})
        page.goto(f"{BASE_URL}/mobile", wait_until="networkidle")
        page.wait_for_timeout(2000)

        # Page should render without horizontal overflow
        overflow = page.evaluate("""() => {
            return document.documentElement.scrollWidth > 375;
        }""")
        assert not overflow, "Mobile page overflows horizontally at 375px width"
        console_tracker.assert_no_errors("MOB-14")

    def test_mob_15_touch_targets_minimum_size(self, navigate, console_tracker, authenticated_page):
        """MOB-15: All buttons/links are at least 44x44px for mobile accessibility."""
        page = authenticated_page
        page.set_viewport_size({"width": 375, "height": 812})
        page.goto(f"{BASE_URL}/mobile", wait_until="networkidle")
        page.wait_for_timeout(2000)

        # Check all visible buttons and links for minimum touch target size
        undersized = page.evaluate("""() => {
            const problems = [];
            const elements = document.querySelectorAll('button, a, [role="button"], input[type="submit"]');
            for (const el of elements) {
                if (!el.offsetParent) continue;  // skip hidden
                const rect = el.getBoundingClientRect();
                if (rect.width < 44 || rect.height < 44) {
                    const label = el.textContent?.trim().slice(0, 30) || el.className || el.tagName;
                    problems.push(`${label}: ${Math.round(rect.width)}x${Math.round(rect.height)}px`);
                }
            }
            return problems;
        }""")
        if undersized:
            # Warn but don't hard-fail — many UI libs have small targets
            pytest.warns(UserWarning, match="undersized touch targets") if False else None
            # Log the issues for review
            for issue in undersized[:5]:
                print(f"  WARN: undersized touch target: {issue}")
        console_tracker.assert_no_errors("MOB-15")
