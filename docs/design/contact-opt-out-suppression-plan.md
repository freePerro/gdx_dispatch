# Contact opt-out is recorded and then ignored

Status: **PLAN rev2** (written 2026-08-26, revised same day after Doug's
decision). Nothing built. No rival plan — every other "suppress" in
`docs/design/` is about reminder idempotency, pay-link hiding, or GL match
candidacy.

> **DECISION (Doug, 2026-08-26): the customer should be able to choose.**
> That is a bigger change than this plan was first written for, and rev1 said
> so wrongly — see §3. Two booleans cannot express a choice; this now needs a
> migration and a surface the customer can actually reach.

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

**rev1 said "no migration, no new column, no new endpoint". That was true only
for the blanket reading, and Doug chose customer choice — so it is now wrong.**
What survives from the table above: the chokepoints, the `skip_reason` channel
and the `kind` label are all still the right places to enforce. What does not:
`email_opt_out` / `sms_opt_out` are two booleans, and a boolean cannot record a
choice between classes. See §3.

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

## 3. DECIDED — the customer chooses (Doug, 2026-08-26)

Not a blanket flag the office sets on their behalf. The customer picks what
they receive.

**What that costs, stated up front, because rev1 understated it:**

1. **A migration.** `email_opt_out` / `sms_opt_out` are booleans. A choice
   between classes needs per-class preferences — a `customer_contact_prefs`
   table keyed on (customer, channel, class) reads better than a widening row
   of nullable booleans, and leaves room for classes nobody has invented yet.
   The two existing columns become the coarse "all off" case and must keep
   working during and after the migration; 12 rows carry them today, all on
   soft-deleted customers (§2).
2. **A surface the customer can reach without logging in.** A choice they
   cannot exercise is not a choice. That means a tokenised preferences page
   reachable from a link in the email itself — the same shape as the public
   estimate-approval and pay pages, which already work this way. A portal page
   alone is not enough: most recipients are not portal users.
3. **The suppression read** — the original scope, unchanged, now keyed on the
   preference rather than the boolean.

**The classes to offer.** Derived from the `kind` values the send chokepoint
already passes, so the taxonomy is not invented: marketing/campaign, invoice
and receipt, estimate and proposal, payment reminder, appointment and job
updates.

### The one thing this decision does not settle — and it needs an answer

**Is there a floor?** "The customer chooses" taken literally means a customer
can switch off invoice delivery. They then do not receive a bill, do not pay,
and get chased for it — a worse outcome for them than the emails they turned
off, and a real cost to the business.

Most systems draw the line at **contract paperwork**: marketing, reminders and
job updates are freely choosable; the invoice or receipt for work performed is
not, because it is the documentation of a contract the customer entered.

**Recommended:** every class choosable except invoice/receipt, which stays on
and is shown on the preferences page as "we still have to send you your bill"
rather than hidden. **Doug to confirm** — if the answer is that invoices are
choosable too, that is buildable, but it should be a decision taken with the
consequence in view rather than inherited from a general principle.

## 4. What to build

0. **Migration** — `customer_contact_prefs` (customer, channel, class,
   allowed) plus a resolver that treats the legacy `email_opt_out` /
   `sms_opt_out` booleans as "all classes off" so no existing record changes
   meaning. Runs on SQLite and Postgres; escape any literal `%` as `%%`.
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
5. **The customer-facing preferences page** — tokenised, reachable from a link
   in every email we send, same shape as the public estimate-approval and pay
   pages. A choice the customer cannot exercise is not a choice, and most
   recipients are not portal users. The link must be in the email body, not
   only in the portal.
6. **Audit every suppression.** A message not sent because of an opt-out is a
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

1. ~~Which classes does an opt-out silence?~~ **ANSWERED 2026-08-26: the
   customer chooses.** See §3.
2. **Is there a floor?** (§3) — recommended: every class choosable except
   invoice/receipt. Needs Doug's confirmation before build, because the
   alternative means a customer can switch off their own bill.
3. Should an opt-out block the **customer portal invite** (a login link the
   customer asked for) — transactional, or contact?
4. Should staff see the preference state on the customer record so nobody
   hand-sends around it? There is no UI for these columns at all today.
