"""High-signal UI workflow completeness checks.

These tests assert that each major frontend page is not only rendering,
but also exposes clickable controls for core user workflows.
"""
from __future__ import annotations

import uuid

import pytest
from playwright.sync_api import Page, expect

from gdx_dispatch.tests.e2e.conftest import E2E_PASSWORD

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not E2E_PASSWORD, reason="GDX_E2E_PASSWORD not set"),
]


@pytest.fixture(scope="module")
def seeded_entities(api):
    """Seed minimal records used for row-click and action assertions."""
    token = uuid.uuid4().hex[:8]

    customer_resp = api.post(
        "/api/customers",
        json_data={
            "name": f"UI Workflow Customer {token}",
            "phone": f"555-{token[:4]}",
            "email": f"ui_workflow_{token}@test.local",
            "address": "101 Workflow Ln",
        },
    )
    assert customer_resp.status_code in (200, 201), customer_resp.text[:300]
    customer = customer_resp.json()

    job_resp = api.post(
        "/api/jobs",
        json_data={
            "title": f"UI Workflow Job {token}",
            "customer_id": customer["id"],
            "status": "Scheduled",
        },
    )
    assert job_resp.status_code in (200, 201), job_resp.text[:300]
    job = job_resp.json()

    invoice_resp = api.post(
        "/api/invoices",
        json_data={"job_id": job["id"]},
    )
    assert invoice_resp.status_code in (200, 201), invoice_resp.text[:300]
    invoice = invoice_resp.json()

    estimate_resp = api.post(
        "/api/estimates",
        json_data={"customer_id": customer["id"]},
    )
    assert estimate_resp.status_code in (200, 201), estimate_resp.text[:300]
    estimate = estimate_resp.json()

    return {
        "customer": customer,
        "job": job,
        "invoice": invoice,
        "estimate": estimate,
        "token": token,
    }


def _assert_clickable(locator):
    expect(locator).to_be_visible(timeout=10000)
    expect(locator).to_be_enabled(timeout=10000)


def _open_dialog_from_button(page: Page, button_locator):
    _assert_clickable(button_locator)
    button_locator.click(timeout=10000)
    dialog = page.locator(
        "[role='dialog'], .p-dialog, .modal, .dialog, [aria-modal='true']"
    ).first
    expect(dialog).to_be_visible(timeout=10000)
    return dialog


def _assert_filter_button_clicks(page: Page, labels: list[str]):
    for label in labels:
        button = page.locator(
            f"button:has-text('{label}'), [role='tab']:has-text('{label}')"
        ).first
        _assert_clickable(button)
        button.click(timeout=10000)
        page.wait_for_timeout(300)


class TestUIWorkflowCompleteness:
    def test_jobs_page_workflow_completeness(
        self,
        api,
        navigate,
        authenticated_page: Page,
        console_tracker,
        seeded_entities,
    ):
        page = navigate("/jobs", wait_for="networkidle")

        # New Job opens modal/dialog
        new_job = page.locator(
            "button:has-text('New Job'), a:has-text('New Job'), [data-testid*='new-job']"
        ).first
        _open_dialog_from_button(page, new_job)
        page.keyboard.press("Escape")

        # Status filters are clickable
        _assert_filter_button_clicks(page, ["Scheduled", "In Progress", "Completed"])

        # Search accepts text and filters
        search = page.locator(
            "input[type='search'], input[placeholder*='search' i], [data-testid*='search']"
        ).first
        _assert_clickable(search)
        search.fill(seeded_entities["token"], timeout=10000)
        expect(search).to_have_value(seeded_entities["token"], timeout=5000)

        target_row = page.locator("table tbody tr, .p-datatable-tbody tr").filter(
            has_text=seeded_entities["job"].get("title", "")
        ).first
        expect(target_row).to_be_visible(timeout=10000)

        # Row click navigates to detail
        target_row.click(timeout=10000)
        page.wait_for_timeout(800)
        assert "/jobs/" in page.url, f"Expected job detail navigation, got {page.url}"

        # Back to list for row actions
        page = navigate("/jobs", wait_for="networkidle")
        search = page.locator(
            "input[type='search'], input[placeholder*='search' i], [data-testid*='search']"
        ).first
        search.fill(seeded_entities["token"], timeout=10000)

        row_for_actions = page.locator("table tbody tr, .p-datatable-tbody tr").filter(
            has_text=seeded_entities["job"].get("title", "")
        ).first
        expect(row_for_actions).to_be_visible(timeout=10000)

        edit_action = row_for_actions.locator(
            "button:has-text('Edit'), a:has-text('Edit'), [aria-label*='edit' i], [class*='edit']"
        ).first
        _assert_clickable(edit_action)

        delete_action = row_for_actions.locator(
            "button:has-text('Delete'), a:has-text('Delete'), [aria-label*='delete' i], [class*='delete']"
        ).first
        _assert_clickable(delete_action)
        delete_action.click(timeout=10000)

        confirmation = page.locator(
            "[role='dialog']:has-text('Confirm'), [role='dialog']:has-text('Delete'), "
            ".p-confirm-dialog, .p-dialog:has-text('Are you sure'), .modal:has-text('Delete')"
        ).first
        expect(confirmation).to_be_visible(timeout=10000)

        cancel = confirmation.locator(
            "button:has-text('Cancel'), button:has-text('No'), button:has-text('Close')"
        ).first
        if cancel.count() > 0:
            cancel.click(timeout=5000)

        console_tracker.assert_no_errors("ui workflow completeness: jobs")

    def test_customers_page_workflow_completeness(
        self,
        navigate,
        authenticated_page: Page,
        console_tracker,
        seeded_entities,
    ):
        page = navigate("/customers", wait_for="networkidle")

        new_customer = page.locator(
            "button:has-text('New Customer'), a:has-text('New Customer'), button:has-text('New')"
        ).first
        _open_dialog_from_button(page, new_customer)
        page.keyboard.press("Escape")

        search = page.locator(
            "input[type='search'], input[placeholder*='search' i], [data-testid*='search']"
        ).first
        _assert_clickable(search)
        search.fill(seeded_entities["token"], timeout=10000)
        expect(search).to_have_value(seeded_entities["token"], timeout=5000)

        row = page.locator("table tbody tr, .p-datatable-tbody tr").filter(
            has_text=seeded_entities["customer"].get("name", "")
        ).first
        expect(row).to_be_visible(timeout=10000)

        # Row click navigates to detail
        row.click(timeout=10000)
        page.wait_for_timeout(800)
        assert "/customers/" in page.url, f"Expected customer detail navigation, got {page.url}"

        page = navigate("/customers", wait_for="networkidle")
        search = page.locator(
            "input[type='search'], input[placeholder*='search' i], [data-testid*='search']"
        ).first
        search.fill(seeded_entities["token"], timeout=10000)

        row = page.locator("table tbody tr, .p-datatable-tbody tr").filter(
            has_text=seeded_entities["customer"].get("name", "")
        ).first
        expect(row).to_be_visible(timeout=10000)

        edit_action = row.locator(
            "button:has-text('Edit'), a:has-text('Edit'), [aria-label*='edit' i], [class*='edit']"
        ).first
        _assert_clickable(edit_action)

        delete_action = row.locator(
            "button:has-text('Delete'), a:has-text('Delete'), [aria-label*='delete' i], [class*='delete']"
        ).first
        _assert_clickable(delete_action)

        console_tracker.assert_no_errors("ui workflow completeness: customers")

    def test_billing_page_workflow_completeness(
        self,
        navigate,
        authenticated_page: Page,
        console_tracker,
        seeded_entities,
    ):
        page = navigate("/billing", wait_for="networkidle")

        create_invoice = page.locator(
            "button:has-text('Create Invoice'), a:has-text('Create Invoice'), button:has-text('New Invoice')"
        ).first
        _open_dialog_from_button(page, create_invoice)
        page.keyboard.press("Escape")

        _assert_filter_button_clicks(page, ["Draft", "Sent", "Paid", "Overdue"])

        row = page.locator("table tbody tr, .p-datatable-tbody tr").filter(
            has_text=seeded_entities["invoice"].get("invoice_number", "")
        ).first
        expect(row).to_be_visible(timeout=10000)
        row.click(timeout=10000)
        page.wait_for_timeout(800)
        assert "/billing/" in page.url or "/invoices/" in page.url, (
            f"Expected invoice detail navigation, got {page.url}"
        )

        payment_action = page.locator(
            "button:has-text('Record Payment'), a:has-text('Record Payment'), "
            "button:has-text('Add Payment'), [aria-label*='payment' i]"
        ).first
        _assert_clickable(payment_action)

        console_tracker.assert_no_errors("ui workflow completeness: billing")

    def test_estimates_page_workflow_completeness(
        self,
        navigate,
        authenticated_page: Page,
        console_tracker,
        seeded_entities,
    ):
        page = navigate("/estimates", wait_for="networkidle")

        new_estimate = page.locator(
            "button:has-text('New Estimate'), a:has-text('New Estimate'), button:has-text('Create Estimate')"
        ).first
        _open_dialog_from_button(page, new_estimate)
        page.keyboard.press("Escape")

        _assert_filter_button_clicks(page, ["Draft", "Sent", "Accepted", "Declined"])

        row = page.locator("table tbody tr, .p-datatable-tbody tr").filter(
            has_text=seeded_entities["estimate"].get("estimate_number", "")
        ).first
        expect(row).to_be_visible(timeout=10000)
        row.click(timeout=10000)
        page.wait_for_timeout(800)
        assert "/estimates/" in page.url, f"Expected estimate detail navigation, got {page.url}"

        convert_to_job = page.locator(
            "button:has-text('Convert to Job'), a:has-text('Convert to Job'), "
            "button:has-text('Convert'), [aria-label*='convert' i]"
        ).first
        _assert_clickable(convert_to_job)

        console_tracker.assert_no_errors("ui workflow completeness: estimates")

    def test_settings_page_workflow_completeness(
        self,
        navigate,
        authenticated_page: Page,
        console_tracker,
    ):
        page = navigate("/settings", wait_for="networkidle")

        branding_tab = page.locator(
            "[role='tab']:has-text('Branding'), button:has-text('Branding'), a:has-text('Branding')"
        ).first
        _assert_clickable(branding_tab)
        branding_tab.click(timeout=10000)

        color_picker = page.locator(
            "input[type='color'], [data-testid*='color'], [aria-label*='color' i]"
        ).first
        expect(color_picker).to_be_visible(timeout=10000)

        modules_tab = page.locator(
            "[role='tab']:has-text('Module'), button:has-text('Module'), a:has-text('Module')"
        ).first
        _assert_clickable(modules_tab)
        modules_tab.click(timeout=10000)

        module_toggle = page.locator(
            "[role='switch'], input[type='checkbox'], .p-inputswitch, .p-toggleswitch"
        ).first
        _assert_clickable(module_toggle)

        users_tab = page.locator(
            "[role='tab']:has-text('User'), button:has-text('User'), a:has-text('User')"
        ).first
        _assert_clickable(users_tab)
        users_tab.click(timeout=10000)

        user_list = page.locator(
            "table tbody tr, [data-testid*='user-row'], .user-list [class*='row']"
        ).first
        expect(user_list).to_be_visible(timeout=10000)

        invite_button = page.locator(
            "button:has-text('Invite'), button:has-text('Invite User'), button:has-text('Add User'), "
            "a:has-text('Invite'), [data-testid*='invite']"
        ).first
        _assert_clickable(invite_button)

        console_tracker.assert_no_errors("ui workflow completeness: settings")

    def test_timeclock_page_workflow_completeness(
        self,
        api,
        navigate,
        authenticated_page: Page,
        console_tracker,
    ):
        # Normalize to clocked-out first so Clock In is testable.
        status = api.get("/api/timeclock/status")
        if status.status_code == 200:
            data = status.json()
            clocked_in = bool(
                data.get("clocked_in")
                or data.get("is_clocked_in")
                or str(data.get("status", "")).lower() in {"clocked_in", "in", "active"}
            )
            if clocked_in:
                api.post("/api/timeclock/clock-out", json_data={"gps_lat": 33.4484, "gps_lng": -112.0740})

        page = navigate("/timeclock", wait_for="networkidle")

        clock_in = page.locator(
            "button:has-text('Clock In'), [data-testid='clock-in-btn'], [class*='clock-in']"
        ).first
        _assert_clickable(clock_in)
        clock_in.click(timeout=10000)

        status_indicator = page.locator(
            "[data-testid='clock-status'], .clock-status, [class*='status'], [class*='clocked']"
        ).first
        expect(status_indicator).to_be_visible(timeout=10000)

        clock_out = page.locator(
            "button:has-text('Clock Out'), [data-testid='clock-out-btn'], [class*='clock-out']"
        ).first
        _assert_clickable(clock_out)

        # Cleanup to avoid leaking test session state.
        clock_out.click(timeout=10000)

        console_tracker.assert_no_errors("ui workflow completeness: timeclock")

    def test_dispatch_page_workflow_completeness(
        self,
        navigate,
        authenticated_page: Page,
        console_tracker,
    ):
        page = navigate("/dispatch", wait_for="networkidle")
        page.wait_for_timeout(1500)

        tech_columns = page.locator(
            "[data-testid*='tech-column'], .tech-column, .dispatch-column, [data-column*='tech'], [data-testid='unassigned']"
        )
        expect(tech_columns.first).to_be_visible(timeout=10000)

        job_cards = page.locator(
            "[data-testid='job-card'], .job-card, .dispatch-job, [draggable='true']"
        )
        assign_controls = page.locator(
            "button:has-text('Assign'), [aria-label*='assign' i], select[name*='tech'], .assign-job"
        )

        if job_cards.count() > 0:
            expect(job_cards.first).to_be_visible(timeout=10000)
            draggable = job_cards.first.get_attribute("draggable") == "true"
            inline_assign = job_cards.first.locator(
                "button:has-text('Assign'), [aria-label*='assign' i], select"
            ).count() > 0
            assert draggable or inline_assign or assign_controls.count() > 0, (
                "Dispatch job cards are present but neither draggable nor assignable"
            )
        else:
            expect(assign_controls.first).to_be_visible(timeout=10000)
            _assert_clickable(assign_controls.first)

        console_tracker.assert_no_errors("ui workflow completeness: dispatch")
