# Scheduled texts — send a reply, an invoice link or an estimate link later

**Date:** 2026-09-30
**Status:** PARTIALLY BUILT — scheduling a reply, an invoice link and an estimate link (office and tech routes), the per-minute drain, cancel, the send-later panel and the scheduled-texts list (desktop SMS, mobile SMS, the invoice / estimate text dialog) and migration 103 are built in the PR that adds this doc. NOT built: a company-wide list of scheduled texts, and hardening of the send-now reply route (see *Not built*).

Doug, 2026-09-30: "I would like to reply back to this number but do not want
the text to go out at this time of night. It should be the same way for
billing and estimates."

## What already exists (do not rebuild)

- **Plain replies** — `POST /api/phone-com/messages` (`modules/phone_com/router.py::send_message`),
  called by the desktop SMS view and the mobile SMS view. Sends immediately,
  writes a `phone_com_messages` row.
- **Link texts** — `core/link_sms.py::send_link`, the engine under "text an
  invoice" (#820) and "text an estimate" (#822): row lock, pending message row
  committed before the call, no re-POST, unconfirmed block, commit retry,
  audit on every outcome. Adapters `core/invoice_sms.py` and
  `core/estimate_sms.py` own refusals and stamps. Four routes call it
  (office invoices/estimates, tech invoices/quotes); one dialog,
  `SmsLinkDialog.vue`, fronts all four.
- **Beat + an outbox drain precedent** — `tasks/plugin_email_outbox.py` runs
  every minute; prod runs `celery-beat`, `celery-low` and `celery-high`
  (checked 2026-09-30). `modules/phone_com/tasks.py` is already in the worker
  include list and routed to `priority:low`.

## Prior art

- **Phone.com v4 cannot schedule.** The Send Message schema
  (https://apidocs.phone.com/reference/post_v4-accounts-voip-id-messages, read
  2026-09-30) takes `from`, `to`, `text`, `media`, `tag` — no send time, and no
  cancel endpoint. So the queue has to live here.
- Twilio has native Message Scheduling
  (https://www.twilio.com/docs/messaging/features/message-scheduling) — not
  our provider. Housecall Pro / Jobber schedule reminders through
  automations; the common guidance is no texts before 8 AM or after 8 PM
  recipient-local.

## Design

**One table, `scheduled_sms`** (Phone.com module, ORM + guarded migration 103):
`kind` (`message` | `invoice` | `estimate`), `entity_id`, `to_number`, `body`,
`customer_id`, `job_id`, `send_at`, `status`, `audit_action`, `tenant_id` (the
id the Phone.com token is keyed by), `created_by_user_id`, `canceled_by_user_id`,
`canceled_at`, `attempted_at`, `sent_at`, `phone_com_message_row_id`,
`error_code`, `error_message`, `created_at`, `updated_at`.

Statuses: `scheduled` → `sending` → `sent` | `failed` | `unknown` | `skipped`;
`scheduled` → `canceled`. A cancel is a status, never a delete.

**The text is rebuilt, not replayed** (audit finding 1). An invoice text quotes
the amount due, and a payment can land overnight. So the body is stored only
when the operator edited it away from the default; otherwise it is NULL and
the adapter builds it at fire time from the document as it is then. The
schedule dialog says so.

**Schedule time.** Each schedule route runs the same gate and the same
refusal check (`prepare`) as its send-now twin, so the operator learns *now*
that the customer opted out or the invoice is void. `send_at` must be
timezone-aware, at least one minute out and at most 30 days out, and is
stored converted to UTC — SQLite drops the offset (audit finding 5), so a
local-offset value would read back hours early in tests.

**Fire time.** Beat task `phone_com.send_due_scheduled_sms`, every minute.
For each due row it *claims* the row with a conditional
`UPDATE … SET status='sending' WHERE id=:id AND status='scheduled'` and
commits before calling anything. Cancel is the same conditional update from
the other side (`… SET status='canceled' WHERE id=:id AND status='scheduled'`),
so a cancel and a fire landing together cannot both win.

**The scheduler is re-checked at fire time** (audit finding 5): the user who
scheduled it must still exist and be active, and an office text still needs
`invoices.send` / `estimates.send` under the same resolver `require_permission`
uses. Otherwise the row is `skipped` with `scheduler_not_permitted`.

Found by the final-diff audit (2026-09-30) and built: the send-now routes also
refuse when texting (the `phone_com` module) is off and, for a tech, when the
job is no longer theirs. Both are re-checked at fire time too
(`texting_disabled`, `job_not_yours`). Each row's `attempted_at` is stamped at
its own claim, not at the drain's start, so a slow batch is never mistaken for
a dead worker; and one drain stops claiming after 50 seconds, leaving the rest
to the next minute, so a slow Phone.com cannot hold a `priority:high` slot for
a whole batch.

Found by the third audit round (2026-09-30) and built: **on time or not at
all.** A text found more than 30 minutes past its time (the worker or beat was
down) is `skipped` as `too_late`, never sent late — "send at 8 AM" must not
become "send at 2 AM when the worker came back". The list shows a waiting text
that is past its time as "Overdue — waiting to send" rather than a stale time.

**Stuck rows** (audit finding 3). A worker killed between the claim and the
outcome would leave a row `sending` forever; celery-low is recreated on every
deploy. Each drain first moves any row `sending` for over ten minutes to
`unknown` (`worker_interrupted`) and audits it, so the operator is told to
check the thread instead of seeing "sending" indefinitely. It is never resent.

Then:

- `invoice` / `estimate` → the adapter's `send()` with the scheduler's user as
  actor (money invariant: the draft→sent GL transition is attributed to the
  person who scheduled it, never a system identity), the stored `to` and
  `body`, and `audit_action` (`invoice_sent_sms_scheduled`, …). Every refusal
  the adapter makes at send-now time is re-made at fire time: an invoice paid
  or voided overnight, an estimate accepted, a customer who texted STOP. The
  adapters raise the same exception type for refusals and for Phone.com
  outcomes (audit finding 4), so the drain sorts by `detail.code`:
  `sms_provider_error` and a not-configured 503 → `failed`;
  `sms_outcome_unknown` → `unknown`; `sent_but_not_recorded` → `sent` with the
  code kept; anything else → `skipped`. An unexpected exception is `unknown`,
  never `skipped` — it may have been raised after the text left. A tech's scheduled invoice
  also re-checks office verification.
- `message` → its own delivery with link_sms's safety rules, not a lift of
  `send_message` (which writes its row only after the call, catches only
  `PhoneComAPIError` and writes no audit row — audit finding 2): a pending
  `phone_com_messages` row is committed before the call, every httpx error is
  sorted into definite (`failed`) or ambiguous (`unknown`), and the outcome is
  audited. A customer-linked reply re-checks `sms_opt_out`.
- **Never retried.** An SMS cannot be recalled: a 4xx / no connection is
  `failed`, a timeout / 5xx is `unknown`, and neither is sent again
  automatically. The office can schedule or send again by hand.

Every outcome is audited: `sms_scheduled`, `sms_schedule_canceled`,
`scheduled_sms_sent`, `scheduled_sms_skipped`, `scheduled_sms_failed`,
`scheduled_sms_unknown`, plus the adapters' own rows. A text that did not go
(skipped, failed or unconfirmed — including a row the reaper marks unknown after
a worker restart) also puts a broadcast alert in the office bell — found by the
fourth and fifth audit rounds: the operator scheduled it and walked away, and
nobody reads the thread at 8 AM. The alert opens billing or estimates for a
document text (pages a tech can open on the phone) and the SMS page for a reply
(only the office can schedule one).

**Routes.**

- `POST /api/phone-com/messages/schedule`, `GET /api/phone-com/scheduled`
  (`?to=` or `?kind=&entity_id=`), `POST /api/phone-com/scheduled/{id}/cancel`
  — Phone.com module gate, as the send route.
- `POST /api/invoices/{id}/schedule-sms` (invoices.send), `POST
  /api/estimates/{id}/schedule-sms` (estimates.send), `POST
  /api/mobile/invoices/{id}/schedule-sms` and `POST
  /api/mobile/quotes/{id}/schedule-sms` (job ownership, as their send-now
  twins).

**UI.** A shared "Send later" picker: *next 8:00 AM*, *next 9:00 AM*, or a
date-and-time picker, in the browser's time. It sits beside Send in the
desktop SMS view, the mobile SMS view and `SmsLinkDialog`. Pending texts show
where the operator will look for them: as dashed "Scheduled for …" bubbles
with Cancel at the bottom of the SMS thread, and as a list with Cancel inside
the invoice / estimate text dialog.

## Rejected

- **A quiet-hours rule that holds every text automatically.** It would change
  send-now behaviour for every surface; Doug asked for a choice, not a rule.
- **Scheduling through the SMS thread only.** Invoice and estimate texts
  move the document to *sent*; they must go through their adapters or the
  link would be dead when it arrives.
- **Retrying a failed scheduled text.** See "never retried".

## Not built

- Hardening the send-now reply route (`send_message`) — the gaps audit finding
  2 names are real there too, and are out of this change's scope.
- A company-wide "all scheduled texts" page.
