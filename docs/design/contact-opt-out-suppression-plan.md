# Contact opt-out is recorded and then ignored

Status: **PLAN** (written 2026-08-26, verified against `origin/main @ f1cbd97`).
Nothing built. No rival plan — every other "suppress" in `docs/design/` is
about reminder idempotency, pay-link hiding, or GL match candidacy.

`customers.email_opt_out` and `customers.sms_opt_out` are **written by the CCPA
endpoint and read by nothing before a send**. The first live customer who opts
out will keep receiving invoices, reminders and campaigns.

---

## 0. What already exists — do not rebuild

Most of this is done. The gap is one missing read.

| Piece | Where | State |
| --- | --- | --- |
| The columns | `models/tenant_models.py:229-230` `email_opt_out` / `sms_opt_out` | **Exist** (migration `111_ccpa_and_integrations`) |
| The write path | `routers/gdpr.py:452-453` sets both, audits at `:478` | **Built** |
| One email chokepoint | `core/transactional_email.py:289` `send_transactional_email` | **Built** — 9 call sites, all real mail goes through it |
| A skip channel | same function returns `(sent, provider, skip_reason)` | **Built** — a suppression already has somewhere to report itself |
| A message-kind label | its `kind` parameter (`:301`) | **Built** — this is the lever for marketing-vs-transactional |
| One SMS chokepoint | `core/sms.py` `send_sms` | **Built** |

**No migration is needed. No new column. No new endpoint.** The work is a read
in two places plus a decision about which message classes it applies to.

## 1. The defect

Nothing anywhere consults the columns before sending:

```
core/transactional_email.py     opt-out / suppression mentions: 0
routers/campaigns.py                                            0
modules/campaigns/service.py                                    0
core/sms.py                                                     0
routers/invoice_reminders.py                                    1   ← unrelated
```

The single hit in `invoice_reminders.py:515` is about reminder idempotency
("manual NULL-threshold logs never suppress"), not opt-out.

So the sequence today is: a customer opts out → `gdpr.py` records it, audits it,
returns `{"ok": true}` → and every send path carries on. The record is honest;
the behaviour does not follow it.

## 2. Blast radius: real defect, zero exercised harm

Measured on prod 2026-08-26 — and this is the part that stops the plan being
written in a panic:

```
opted_out_total  soft_deleted  still_live
             12            12           0

invoices sent to an opted-out customer: 0
```

All twelve carry **no email address and no phone number**, have 0 invoices,
0 estimates, $0, and 1 job between them. Created across five days in late March,
then deleted one at a time — twelve distinct minutes — on 2026-04-08. They are
setup junk, not customers exercising a right.

**Nobody's opt-out has been ignored.** This is a loaded trap, and the trigger is
the first genuine request. Fix it before that day, not after.

*(How the flags came to be set is unknown: the audit log begins 2026-06-22 and
these predate it by two months. Not guessed at here.)*

## 3. The decision this plan cannot make for itself

**Which message classes does an opt-out silence?** This is the whole design, and
it is a legal question wearing a technical costume.

* **Marketing — campaigns.** Suppress unconditionally. No exceptions, no
  override. This is the class with statutory weight (CAN-SPAM for email, and
  TCPA for SMS, which is stricter and does not carve out much).
* **Transactional — invoice, receipt, estimate, portal link, payment reminder.**
  **Genuinely open.** These are not marketing; they are the paperwork of a
  contract the customer entered. Blanket-suppressing them means a customer can
  opt out of *being invoiced*, and a garage-door install still has to be billed.
  Suppressing them may be the wrong reading of both the law and the business.
* **SMS generally.** Treat as stricter than email. A tech's "on my way" text to
  a customer who has opted out of SMS should not go.

Recommended default, to be confirmed rather than assumed:
**hard-suppress marketing on both channels; leave transactional email flowing;
suppress all SMS.** Anything else needs Doug to say so, because getting it
wrong in the permissive direction is a compliance problem and getting it wrong
in the restrictive direction stops the business invoicing people.

## 4. What to build

1. **One helper**, `core/contact_prefs.py::is_suppressed(db, *, customer_id,
   channel, kind) -> str | None` — returns a reason or `None`. One place, so the
   two chokepoints cannot drift the way the four photo re-encoders did.
2. **Read it in `send_transactional_email`** before dispatch. On suppression,
   return `(False, None, "recipient_opted_out")` — the existing `skip_reason`
   channel, so every caller already handles it and nothing needs a new branch.
3. **Read it in the campaigns send path** (`routers/campaigns.py:355`
   `send_campaign` and `modules/campaigns/`), filtering recipients before the
   batch rather than per-send, and **count the suppressed** in the result so the
   number is visible rather than silent.
4. **Read it in `core/sms.py::send_sms`**, same shape.
5. **Audit every suppression.** A message not sent because of an opt-out is a
   compliance event and must be reconstructable: who would have been contacted,
   on what channel, for what, and when it was withheld. Per invariant #1, a
   suppression with no trail is as bad as a send with no trail.

## 5. Traps

* **Do not gate on the in-memory DNC list.** `routers/communications.py:756`
  keeps `_DNC_LIST_BY_TENANT` — a module-level dict that dies on every container
  restart, entirely separate from these columns, with no UI caller. It looks
  like a do-not-contact store and is not one. See
  `communications-parallel-fake-plan` (to be written) for its removal.
* **`email_opt_out` is nullable.** `NULL` means "never asked", not "opted out".
  Test the three-state, not the boolean.
* **A suppressed send must not look like a failure.** The callers treat
  `sent=False` as a delivery problem in places; a suppression is a *correct
  outcome*. It needs its own reason string and must not trip bounce detection,
  dunning retries, or the "email failed" surfaces.
* **The counterfactual must exist before this is called done.** A test that
  proves nothing is sent to an opted-out customer must be shown going red
  against today's code — the whole point is that a green suite proves nothing
  here, since today's suite is green *with* the defect.

## 6. Verification

* Per-channel, per-kind tests: opted-out + marketing → suppressed + audited;
  opted-out + transactional → per the §3 decision; `NULL` → sends.
* Each confirmed to go **red** with the suppression check removed.
* A campaign run whose result counts suppressed recipients.
* Prod re-query of §2 after deploy — the numbers should be unchanged, because
  no live customer has an opt-out. If that count moves, someone opted out for
  real and the fix earned itself immediately.

## 7. Open decisions

1. **§3 — which classes does an opt-out silence?** Recommended default above.
   Doug's call; nothing gets built until it is answered, because the answer *is*
   the feature.
2. Should an opt-out block the **customer portal invite** (a login link the
   customer asked for) — transactional, or contact?
3. Should staff see an opt-out badge on the customer record so nobody hand-sends
   around it? Currently there is no UI for these columns at all.
