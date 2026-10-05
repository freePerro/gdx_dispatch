# HubX order email → doors marked ordered

**Date:** 2026-09-29
**Status:** PLAN — built on branch `feat/hubx-door-ordered`, PR pending.
**Trigger:** Doug: the order-confirmation emails carry the QCD number that is on
the estimate; when one arrives, the door should move from "need to order" to
"ordered" in the dispatch area and everywhere else that shows it, like the
leads area.

## What already exists (do not rebuild)

- **The QCD is already on the estimate.** A captured door's spec rides on
  `EstimateLine.line_metadata["Number"]` (`core/door_specs.py`).
- **Conversion already copies it to the job.** `_copy_estimate_lines_to_job`
  (`routers/estimates.py`) writes one `job_parts_needed` row per estimate line,
  status `needed`, with the scalar spec in `notes` as `Number=QCD…`. Verified on
  production: a job with two doors has both rows carrying their QCD.
- **"Ordered" already means something.** `job_parts_needed.status` is
  `needed → ordered → received`. The office's "Mark Ordered" button
  (`routers/parts_needed.py`) sets it, and every surface that shows parts
  (job detail, Parts to Order, mobile job cards) reads it.
- **The dispatch lane already exists.** Accepted estimates land in the
  "Order Doors" holding area. Production also has "Need to order" and
  "Waiting on doors".
- **The emails are already mirrored.** Outlook sync stores every message in
  `outlook_messages`, but only Graph's 255-character body preview.
- **Not the distributor order parser.** `modules/vendor_orders` parses the
  distributor's order-confirmation PDF, whose lot-number field was meant for
  a QCD. Measured on production: 0 of 52 carry one. The QCD arrives in the
  door manufacturer's HubX "Order submission from SubDealer …" email instead.

Prior art: a web search found no public HubX integration or API
(chiohd.com, orderentry.chiohd.com, the HubX mobile app listings;
searched 2026-09-29). The email is the only signal.

## Evidence from production (read-only, 2026-09-29)

- 38 HubX submission emails since 2025-11. **13 carry two or three doors**, and
  the preview cuts off every item after the first — the body must be fetched.
- 13 of 38 QCDs match an accepted estimate today; the rest are quotes never
  converted, declined or expired estimates, or stock orders. That count is
  measured against today's data. In 4 of the 13 the estimate was accepted
  after the email arrived, 3 of them within minutes, so a design without a
  retry would have caught at most 9.
- Inside a 30-day lookback, exactly one open job changes on the first sync:
  its two doors go `ordered` and it leaves "Order Doors". Every other
  in-window match is a completed job or an unaccepted estimate. The unaccepted
  ones are retried, so if one of them is accepted while its email is still
  under 30 days old, its job changes then.

## Design

`modules/vendor_orders/hubx.py`, called at the end of each mailbox sync
(`modules/outlook/tasks.py`), after the folder mirror has committed.

1. Candidates are mirrored messages from the HubX sender whose subject starts
   "Order submission", received in the last 30 days, not already recorded.
   At most 20 per sync; each costs one Graph `GET /me/messages/{id}`.
2. The body is parsed into items (QCD, quantity, job/PO name, model, size).
3. Each QCD resolves through **accepted** estimates only, to exactly one job.
4. On that job, the door row whose notes carry that QCD goes
   `needed → ordered`. Any other status is left alone.
5. When every door on the job is ordered, and the job sits in "Order Doors"
   or "Need to order", it moves to "Waiting on doors". A door is its QCD on
   the accepted estimate. It counts as ordered when its parts row has left
   `needed`, or, when it has no parts row, when a HubX order for it has been
   recorded against this job. A job in any other lane, or none, is not moved.
6. Every QCD is recorded once in `hubx_door_orders` with its outcome, so
   re-reading the message never re-flips a door the office set back by hand.
7. Every fetched message is recorded once in `hubx_order_emails`, keyed on its
   Message-ID, so no message is fetched twice.
8. An order recorded `no_estimate` is re-matched on every sync while it is
   inside the 30-day window. The QCD is stored, so the retry needs no Graph
   call. This catches the common order: submit the cart, then accept.

Each part flip and lane move writes an audit row. The actor is
`hubx-order-ingest`, and the details carry the QCD and Graph message id.

The leads list shows a door tag beside the progress chip: "Doors to order",
"1 of 2 doors ordered" or "Doors ordered". It is derived on read from the same
parts rows.

## Decisions and their reasons

- **Accepted estimates only.** A sent or expired quote with the same QCD is
  not a sale. Matching it would mark a door ordered on a job that does not
  exist yet.
- **Ambiguous means untouched.** Two jobs carrying one QCD is a data problem.
  Guessing marks the wrong customer's door.
- **Closed jobs are never rewritten.** An `ordered` part surfaces in the
  invoice parts checklist. Old mail must not reopen a finished job.
- **30-day lookback.** It covers a missed sync without letting a first deploy
  rewrite months of history.
- **One record per QCD, not per message.** That makes the office's manual
  reset stick.
- **Holding-area names, not ids.** The lanes are tenant data. Conversion
  already routes by name ("Order Doors"), and a missing lane means no move.

## Not built

- **An order whose estimate is accepted more than 30 days after the email.**
  The retry stops at the lookback window and the door stays `needed`.
- **Replaying mail after a HubX format change.** A message whose body yields
  no QCD is still recorded as read, with `item_count` 0, so a later parser fix
  cannot replay it without deleting its row. Today's format parsed 10 of 10
  real bodies.
- **A dispatch-card door tag.** The lane move is what the dispatcher sees.
  The tag is on the leads page only.
- **Tier-accepted estimates get no part flip.** The tier copy path writes no
  `Number=` into the parts notes, so there is no door row to flip. The lane
  move still waits for every door's QCD to be ordered. The leads door tag
  shows nothing for these jobs.
- **Mobile-built tier estimates never move on their own.** Their estimate
  lines carry all three tiers' doors untagged, so the unchosen doors are never
  ordered. The job stays in its lane and the office moves it by hand.

## Adversarial audit (2026-09-29)

- **Re-fetch loop, fixed.** A message whose doors were all recorded, or that
  parsed to no items, was never marked read. It was fetched again on every
  sync and counted against the per-sync cap, so twenty of them starved new
  orders. Graph ids also change on a folder move. Fix: `hubx_order_emails`,
  one row per fetched message, keyed on Message-ID.
- **Early lane move on tier jobs, fixed.** The move checked only parts rows,
  and tier jobs have none carrying a QCD. Fix: the move checks every QCD on
  the accepted estimate.
- **Postgres JSON lookup, checked.** `line_metadata["Number"].as_string()`
  compiles to `->>` on a plain JSON column. It was run read-only on production
  and returned the expected job.
- **Round 2: order emailed before acceptance, fixed.** On production, 4 of 13
  matched orders had the estimate accepted after the email. They were
  recorded `no_estimate` for good. Fix: `retry_unmatched` re-matches them on
  each sync inside the window.
- **Round 2: real bodies checked.** The parser ran read-only against the 10
  newest real HubX bodies on production and parsed all 10.
- **Round 3: retry window keyed on the wrong time, fixed.** The retry
  filtered on when the record was written, so a first deploy's backlog stayed
  retryable for a second month. It now filters on when the email arrived.
- **Round 3, recorded not fixed.** Two concurrent syncs retrying the same
  record can each write a duplicate audit row. The values written are the
  same. The retry test starts from a sent estimate already linked to a job,
  which production never has. The auditor ran the real path, where the job
  is created at acceptance, and it applied correctly.
- **Open question for Doug.** Does "Order submission" mean the manufacturer
  has the order, or can the dealer still reject the cart?
