from __future__ import annotations

import os

from celery.schedules import crontab


def build_beat_schedule() -> dict[str, dict[str, object]]:
    return {
        "drain-plugin-email-outbox-every-minute": {
            # Email overhaul P6: plugins queue mail on the shared DB
            # (plugin-host has no egress); this drain delivers via the
            # unified pipeline. No-ops instantly when the outbox is empty.
            "task": "gdx_dispatch.tasks.plugin_email_outbox.drain_plugin_email_outbox",
            "schedule": crontab(minute="*"),
            "options": {"queue": "priority:low"},
        },
        "planner-digest-daily": {
            # First staff-facing scheduled reminder. Emails a summary of open
            # planner tasks so call-notes taken on a busy day don't scroll away.
            # Celery has no timezone set → fires in UTC; PLANNER_DIGEST_HOUR
            # (default 13 ≈ morning US Central) tunes it. No-ops unless
            # PLANNER_DIGEST_EMAIL is set. See docs/design/call-capture-followup-plan.md.
            "task": "gdx_dispatch.tasks.planner_digest.send_planner_digest",
            "schedule": crontab(hour=int(os.getenv("PLANNER_DIGEST_HOUR", "13") or "13"), minute=0),
            "options": {"queue": "priority:low"},
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
        # "check-upcoming-appointment-reminders-hourly" was the same pattern,
        # and outlived both of the others. It fired every hour on prod and
        # logged `succeeded ... {'scheduled_count': 0}` every time, because
        # gdx_dispatch.tasks.reminders was three stubs:
        # _find_upcoming_appointment_ids returned [], _get_appointment returned
        # None, _send_sms did nothing. Its one test monkeypatched all three, so
        # it proved the stub could call itself and nothing more.
        #
        # Not removed for being unused — the data is real (36 appointments,
        # 16 in the 90 days to 2026-08-22). Removed because the stub was wired
        # to nothing that can send: its private `_send_sms` was a no-op, and
        # `core/sms.py` (the only shared sender) is Twilio, whose credentials
        # are unset on prod. The account's working outbound path is Phone.com
        # (`modules/phone_com/client.py::send_message`, called from that
        # module's router) — the stub never touched it. So implementing the
        # finder alone would still have sent zero messages while logging
        # success. Reviving this needs a product go/no-go on automated customer
        # SMS *and* wiring to the Phone.com sender, not just a query.
        # Removed 2026-08-22 along with the module; re-add with the task when
        # an outbound SMS transport actually exists.
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
        "outlook-vendor-bill-sweep-daily": {
            # Supplier bills + statements of account arrive as PDF attachments
            # from allowlisted senders. The delta sync ingests them as mail
            # lands, but a webhook drop, a budget cap, or a transient Graph
            # error leaves messages un-checkpointed — and until this entry
            # existed the ONLY thing that picked those up was an admin pressing
            # the sweep button, which nobody does. 07:15 UTC ≈ before the
            # office opens US Central. No-ops while the sender allowlist is
            # empty; the task's default window is a keep-up pass, not a
            # backfill (use the admin endpoint with an explicit `days` for
            # deeper history).
            "task": "outlook.sweep_vendor_bills_all_accounts",
            "schedule": crontab(hour=7, minute=15),
            "options": {"queue": "priority:low"},
        },
        "outlook-sync-health-check-hourly": {
            # 2026-08-04 incident: one duplicated delta message froze 29
            # folders (incl. Inbox) for FIVE DAYS while the fallback poller
            # kept reporting "healthy" — its check looks at webhook
            # subscription state only, never sync outcomes. This is the
            # alarm: folder-lag/stall detection → Sentry ERROR + a
            # self-clearing NextAction so a broken mailbox sync is seen the
            # same day, not when someone wonders why the inbox is quiet.
            "task": "outlook.sync_health_check",
            "schedule": crontab(minute=35),
            "options": {"queue": "priority:low"},
        },
        "outlook-retag-untagged-hourly": {
            # D3: forward-tagging only tags NEW mail. This picks up (a) the
            # historical backlog synced before tagging existed, and (b) any
            # message that became matchable only after its customer/job was
            # created in GDX (an initial auto-tag can't match a customer that
            # doesn't exist yet). Idempotent + batched; unmatchable rows are
            # simply retried next hour, manually-linked/cleared rows are
            # skipped (tag_strategy not NULL).
            "task": "outlook.retag_untagged_messages",
            "schedule": crontab(minute=20),
            "options": {"queue": "priority:low"},
        },
        "phone-com-messages-refresh": {
            # SMS inbox live path. Phone.com's listeners have never delivered
            # a webhook to us (verified 2026-07-23: zero POSTs to
            # /api/webhooks/phone-com/ while Outlook webhooks flowed fine),
            # so polling is what keeps the Messages page current. Messages
            # only, windowed to 48h — a handful of small per-extension
            # requests, cheap enough for every 10 minutes. The dual-write
            # race that demoted the old 15-min full poll (poll overwriting a
            # fresh webhook upsert) is moot while webhooks don't deliver;
            # revisit the cadence if listener delivery is ever fixed.
            "task": "phone_com.sync_all_recent_messages",
            "schedule": crontab(minute="*/10"),
            # expires < interval: if the low-priority queue backs up, stale
            # fan-outs are dropped instead of stacking into a burst.
            "options": {"queue": "priority:low", "expires": 540},
        },
        "phone-com-calls-refresh": {
            # Voicemail live path. Voicemail rows are synthesized from inline
            # call-log payloads, so before this poll they only landed at the
            # 03:45 UTC nightly resync — a morning voicemail stayed invisible
            # until the next day (found 2026-08-03). Calls only, windowed to
            # 48h — one or two small requests per run. Offset 5 min from the
            # messages poll above so the two fan-outs don't burst together.
            # NB the :45 firing overlaps phone-com-reconcile-nightly at
            # 03:45 — safe: the shared call harvest tolerates the upsert
            # race per-item (rollback + continue), so neither run can abort
            # the other's work.
            "task": "phone_com.sync_all_recent_calls",
            "schedule": crontab(minute="5-59/10"),
            "options": {"queue": "priority:low", "expires": 540},
        },
        "audit-chain-verify-nightly": {
            # Plan §13: the audit hash-chain's tamper-evidence was never run
            # outside tests. Nightly integrity check; logs an ERROR (→ Sentry)
            # when the chain is broken. Cheap — a single sequential hash walk.
            "task": "audit.verify_chain_nightly",
            "schedule": crontab(hour=4, minute=30),
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
        "expire-stale-estimates-nightly": {
            # Plan §15 win/loss. Mark still-'sent' estimates past their
            # valid_until as 'expired' so the pipeline stops showing months-old
            # quotes as live. valid_until is stamped on send from the tenant's
            # estimate_expiry_days (default 60).
            "task": "estimates.expire_stale_nightly",
            "schedule": crontab(hour=4, minute=15),  # 04:15 UTC nightly
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
        "bank-feeds-schedule-dispatcher-every-5m": {
            # 2026-07-17 Bank feeds (Banno). Same shape as the QB
            # dispatcher: polls the bank_feed_sync_schedule singleton and
            # queues bank_feeds_sync_task when next_run_at has passed;
            # cadence is encoded in how far next_run_at jumps. Manual
            # frequency is skipped (next_run_at NULL).
            "task": "gdx_dispatch.modules.bank_feeds.tasks.bank_feeds_schedule_dispatcher",
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
        "payroll-timesheet-hourly": {
            # Hourly, and the TASK decides — not a once-a-day cron at the
            # configured hour. Three reasons: a container down at 7am on a
            # Monday would otherwise skip a whole fortnight silently; the
            # office correcting a flagged shift at 9:15 gets the send at
            # 10:00 with no second button to press; and idempotence has to
            # exist anyway for a beat that can double-fire after a restart.
            # No-ops instantly (one settings read) when autosend is off,
            # which is the default for every install.
            "task": "payroll_timesheet.send_closed_period",
            "schedule": crontab(minute=5),
            "options": {"queue": "priority:low"},
        },
        "webhook-retry-sweep-every-5m": {
            # n8n/plugin-event platform Sprint 1. Re-dispatches webhook
            # deliveries whose backoff window elapsed, AND rescues rows stranded
            # 'pending' with next_retry_at=NULL (their after_commit enqueue
            # failed because the broker was briefly down). Without this entry the
            # retry ladder in deliver_webhook is dead — nothing walks it.
            "task": "gdx_dispatch.core.webhooks.tasks.retry_failed_webhooks_task",
            "schedule": crontab(minute="*/5"),
            "options": {"queue": "priority:high"},
        },
    }
