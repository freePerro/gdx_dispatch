from __future__ import annotations

from celery.schedules import crontab


def build_beat_schedule() -> dict[str, dict[str, object]]:
    return {
        "check-upcoming-appointment-reminders-hourly": {
            "task": "gdx_dispatch.tasks.reminders.check_upcoming_appointment_reminders",
            "schedule": crontab(minute=0),
            "options": {"queue": "priority:high"},
        },
        "generate-recurring-jobs-daily-6am": {
            # 2026-07-07 prod audit: this entry pointed at a task name
            # ("…generate_recurring_jobs_for_all_tenants") that never
            # existed — the real task is generate_recurring_jobs, which
            # walks the single tenant itself. Every 06:00 firing died as
            # "unregistered task".
            "task": "gdx_dispatch.tasks.recurring.generate_recurring_jobs",
            "schedule": crontab(hour=6, minute=0),
            "options": {"queue": "priority:low"},
        },
        # S122-3 (T2): "sync-qb-every-15-minutes" was wired to a no-op stub
        # since pre-2026-04 — fired every 15 minutes producing
        # synced_count=0. Removed 2026-05-12. CDC poller (S122-18) is the
        # real replacement; webhooks (S122-CE) carry the active path until it ships.
        # "apply-late-fees-daily-midnight" was the same pattern — wired to the
        # no-op gdx_dispatch.tasks.late_fees stub (helpers returned []). Removed
        # 2026-06-22 along with the module; re-add with the task when late-fee
        # logic actually exists.
        # trial-reminders-daily-9am removed in the single-tenant collapse —
        # gdx_dispatch.tasks.trial_reminders was a SaaS trial-lifecycle task (deleted).
        "outlook-renew-subscriptions-every-6h": {
            # Microsoft Graph webhook subscriptions expire every ~3 days.
            # renew_all_outlook_subscriptions skips rows with last_error set.
            "task": "outlook.renew_all_outlook_subscriptions",
            "schedule": crontab(minute=0, hour="*/6"),
            "options": {"queue": "priority:low"},
        },
        "outlook-poll-fallback-every-30m": {
            # Catches missed webhooks (transient Graph/network issues).
            "task": "outlook.poll_outlook_mailboxes_fallback",
            "schedule": crontab(minute="*/30"),
            "options": {"queue": "priority:low"},
        },
        "phone-com-reconcile-nightly": {
            # P1.5 — webhooks cover the live path; this is a daily backstop
            # for missed deliveries (transient Phone.com outages, brief
            # network blips). Catalog (extensions/numbers) refreshes here
            # too since there is no webhook event covering them.
            # Was every 15min through Wave B; the dual-write race between
            # poll and webhook (poll re-writing stale state over a fresh
            # webhook upsert) was the audit's main worry.
            "task": "phone_com.sync_all_phone_com_tenants",
            "schedule": crontab(hour=3, minute=45),  # 03:45 UTC nightly
            "options": {"queue": "priority:low"},
        },
        "phone-com-reconcile-call-reports-nightly": {
            # P3.11 — compare Phone.com server-computed analytics against
            # our local phone_com_stats_daily. Logs drift, doesn't auto-fix.
            "task": "phone_com.reconcile_all_call_reports",
            "schedule": crontab(hour=5, minute=15),
            "options": {"queue": "priority:low"},
        },
        "phone-com-push-contacts-nightly": {
            # P2.8 — push GDX customers as Phone.com contacts so caller-ID
            # on the desk phones / mobile app shows the customer's name.
            # Capped per-run by push_contacts.cap so it never runs long.
            "task": "phone_com.push_all_contacts",
            "schedule": crontab(hour=5, minute=0),
            "options": {"queue": "priority:low"},
        },
        "phone-com-rotate-webhook-secret-weekly": {
            # P1.4 — Phone.com doesn't sign webhooks, so URL-secret rotation
            # is our only hardening lever. Weekly rotation with a 1h grace
            # window keeps the blast radius of any URL leak short.
            "task": "phone_com.rotate_all_webhook_secrets",
            "schedule": crontab(day_of_week="sun", hour=8, minute=0),
            "options": {"queue": "priority:low"},
        },
        "phone-com-stats-rollup-nightly": {
            # D-pc-8 (an earlier session). Stats roll-up piggybacks on every sync
            # (sync.py:197); if sync errors mid-run the inline roll-up is
            # skipped and the dashboard goes stale. Nightly fan-out is the
            # backstop — independent of sync, recovers stats even on a
            # full sync outage. 04:30 UTC = 12:30 AM ET, post-midnight
            # boundary so today's calls land in today's stat_date.
            "task": "phone_com.roll_up_all_phone_com_stats",
            "schedule": crontab(hour=4, minute=30),
            "options": {"queue": "priority:low"},
        },
        "purge-empty-draft-estimates-nightly": {
            # S-autosave slice 5. Server-side draft autosave on /estimates/new
            # creates a draft row the moment a customer is picked. If the
            # tab is abandoned before any lines are added, that row sits
            # forever as an empty draft. Hard-delete after 7 days.
            "task": "estimates.purge_empty_drafts_for_all_tenants",
            "schedule": crontab(hour=3, minute=55),  # 03:55 UTC, just before archive
            "options": {"queue": "priority:low"},
        },
        "archive-stale-draft-estimates-nightly": {
            # 2026-04-29 UX audit F-47. Per-tenant policy `estimate_draft_archive_days`
            # (default 60). 0 disables. Soft-deletes drafts older than the
            # threshold so /estimates doesn't accumulate forgotten test/abandoned
            # rows. Logs a count of archived rows per tenant for the audit trail.
            "task": "estimates.archive_stale_drafts_for_all_tenants",
            "schedule": crontab(hour=4, minute=0),  # 04:00 UTC nightly
            "options": {"queue": "priority:low"},
        },
        "refresh-customer-rolling-volumes-nightly": {
            # Sprint 1.0.6 — defensive backstop. Hot paths refresh on
            # payment.received and on stale-read at estimate-create; this
            # catches drift on customers who haven't been touched recently
            # but whose rolling window has slid past an old payment.
            "task": "refresh_all_customer_rolling_volumes",
            "schedule": crontab(hour=4, minute=15),  # 04:15 UTC nightly
            "options": {"queue": "priority:low"},
        },
        "tech-locations-prune-daily-3am": {
            # Sprint 5 / S5-C5 — drop tech_location rows older than the
            # per-tenant gps_retention_days setting (default 45).
            "task": "gdx_dispatch.tasks.tech_locations_prune.prune_tech_locations_for_all_tenants",
            "schedule": crontab(hour=3, minute=0),
            "options": {"queue": "priority:low"},
        },
        "timeclock-sweep-stuck-shifts-every-30m": {
            # MH-7b — auto-close shifts open longer than MAX_SHIFT_HOURS
            # (16h). This entry lived only in the vestigial Sprint-1
            # gdx_dispatch/celery_app.py (which nothing ran), so stuck
            # shifts were never closed — the 2026-07-07 prod audit found
            # one open for 66 days. The clock-in router enforces the same
            # cap inline, so the sweep only catches abandoned sessions.
            "task": "gdx_dispatch.tasks.timeclock_sweep.sweep_stuck_shifts_for_all_tenants",
            "schedule": crontab(minute="*/30"),
            "options": {"queue": "priority:low"},
        },
        "qb-sync-schedule-dispatcher-every-5m": {
            # 2026-05-20 Banking sprint. Walks every tenant DB, picks
            # rows whose qb_sync_schedule.next_run_at has passed, and
            # queues qb_banking_sync_task. Hourly/4h/Daily/Weekly
            # cadences are encoded by how far next_run_at jumps after
            # each successful run; this dispatcher just polls. Manual
            # frequency is skipped (next_run_at is NULL).
            "task": "gdx_dispatch.modules.quickbooks.tasks.qb_sync_schedule_dispatcher",
            "schedule": crontab(minute="*/5"),
            "options": {"queue": "priority:low"},
        },
        "forecasting-observed-recurring-nightly": {
            # 2026-05-20 observed-recurring sprint. Walks every tenant DB and
            # runs the detector against qb_bank_transactions. Output is
            # RecurringStream rows in status='suggested' for the user to
            # confirm. Nightly cadence — bank feed only updates daily anyway.
            "task": "gdx_dispatch.modules.forecasting.tasks.detect_observed_recurring_dispatcher",
            "schedule": crontab(hour=4, minute=45),  # 04:45 UTC nightly
            "options": {"queue": "priority:low"},
        },
        "forecasting-measurement-tick-daily": {
            # Stage A measurement loop (docs/forecasting-accuracy-roadmap.md).
            # Daily tick: capture today's forecast snapshot + reconcile any
            # snapshots whose window has closed. Feeds Stage B rate calibration.
            # Runs after the recurring detector so the day's forecast inputs are
            # settled first.
            "task": "gdx_dispatch.modules.forecasting.tasks.advance_forecast_measurement_dispatcher",
            "schedule": crontab(hour=5, minute=0),  # 05:00 UTC nightly
            "options": {"queue": "priority:low"},
        },
        "invoice-auto-dunning-daily": {
            # PR6-billing-capture — automated dunning, OPT-IN default OFF
            # (keys off ReminderSettings.auto_send_enabled; while off, a
            # Monday nudge tells admin/owner what isn't being chased,
            # permanently dismissible). Idempotent per stored threshold;
            # per-invoice dunning_paused mutes arrangements. 13:15 UTC,
            # right after the follow-up loop.
            "task": "invoice_reminders.auto_dunning_tick",
            "schedule": crontab(hour=13, minute=15),
            "options": {"queue": "priority:low"},
        },
        "billing-followup-daily": {
            # PR5-billing-capture — the batch's enforcement loop. Counts every
            # billing leak class (ready-to-bill jobs, stale drafts, unbilled
            # approved change orders, used-never-billed parts) and upserts ONE
            # persistent NextAction that clears itself when the pipeline is
            # clean. 13:00 UTC ≈ start of the office day ET.
            "task": "billing_followup.daily_tick",
            "schedule": crontab(hour=13, minute=0),
            "options": {"queue": "priority:low"},
        },
    }
