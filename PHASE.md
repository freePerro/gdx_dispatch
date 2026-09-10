# Phase

**HARDENING** — declared 2026-09-07.

<!-- PHASE: HARDENING -->
<!-- SWEEP_BUDGET_THRESHOLD: 5 -->

> The two HTML comments above are **machine-read** by
> `~/.claude/hooks/session_checklist.py`. Change the number there and the gate
> changes with it — there is no second copy to keep in sync.

This file answers one question: *what kind of work are we doing right now, and
what ends it?* It replaces `NORTH_STAR.md` <!-- archived outside this repo 2026-09-07; never tracked here; link-ok --> (archived 2026-09-07 to
`~/.claude/archive/NORTH_STAR-2026-04-09.md`; its multi-tenant bet was
countermanded by the single-tenant decision in `CLAUDE.md`).

Read this at session start. It is a **present-tense** doc: fix it or retire it,
never let it drift.

---

## Current phase: HARDENING

We are closing defects in what exists, not adding surface area. The app is
mature enough that the frontier is quality, not features. That is a deliberate
choice with an end condition — not a default we drifted into.

### Exit condition (both must hold)

1. **`sweep-finding` backlog < 5.** See "The sweep budget" below.
2. ~~**Both live money defects closed.**~~ ✅ **MET 2026-09-07.**
   - **#422** — a payment landing after a void resurrected the invoice to paid,
     with its parts already released. Closed by #662: a void is terminal at the
     ledger chokepoint, the payment still records, and an operator gets a
     `payment_on_voided_invoice` audit row.
   - **#445** — GL §6 refund-on-overpayment double-dipped. Closed by #663:
     refunds are credit-first, capped by this invoice's overpayment.
   - **#661**, found while fixing #422 and closed by #664: the
     `payment_exceeds_receivable` audit row had never once been written —
     `begin_nested()` around a writer whose guard installer commits on first use
     per engine.
   - **Released in `v1.118.1`** (tagged 2026-09-07 14:55; all three fix commits
     verified as ancestors of the tag). Whether prod is *serving* it is a
     separate check and has not been made here.

### Next phase, already queued

**FEATURE — customer statements.** The 3-PR plan is written; 7 decisions are
owed by Doug before PR A. Nothing starts until the exit condition above is met.

### While hardening

- **No new sweeps** while the sweep budget is CLOSED (see below). Discovery
  tools count: `/redteam`, the agent-quality agent,
  `gdx_dispatch/tools/comment_drift_scan.py`,
  `gdx_dispatch/tools/frontend_contract_scan.py`, `/ux-audit` and orphan-route
  sweeps are each a sweep by another name. `/audit` on the diff being
  committed is not a sweep and is still required.
- **Net-zero filing** while the sweep budget is CLOSED: a PR or session may
  file a new `sweep-finding` only if it also closes one, or Doug says yes to
  that specific filing. Everything else goes on the close-out's *found, not
  filed* list and Doug decides. Adopted 2026-09-10; "Why deferral costs
  something" below says how this meets CLAUDE.md step 4.
- **One issue per session** (CLAUDE.md). Doug can widen it; Claude does not.
- **No new feature work** without Doug saying so explicitly. A feature request
  from Doug overrides this file; a feature idea from Claude does not.
- `live-defect` work is **never** rate-limited. If a user can hit it on prod
  today, it gets fixed regardless of budget.

---

## The sweep budget

Our own audits produce most of the backlog. Over the 30 days to 2026-09-07 we
opened **99** issues and closed **54** (measured against the tracker
2026-09-07; an earlier 48/47 "parity" reading was wrong). Intake runs at
roughly 1.8x closure, so running another sweep is choosing more backlog, not
less.

Every open issue carries exactly one of:

| label | meaning | rate-limited? |
|---|---|---|
| `live-defect` | a real user can hit this on prod today | **never** |
| `sweep-finding` | our own audit produced it; no user has hit it | **yes** |
| `deferred` | adjudicated, deliberately not doing, reason recorded | n/a |

**The rule:** when open `sweep-finding` count is **5 or more**, the sweep
budget is CLOSED — do not start a new audit, scan, or sweep. Drain first.

The count is printed at every session start by
`~/.claude/hooks/session_checklist.py`. Do not estimate it; read it.

## Why deferral costs something

A sweep that defers instances to a follow-up issue files them as
`sweep-finding`, which spends sweep budget. This is deliberate. Deferral used to
be free, which is why "a finding names an instance; the fix owns the class"
(CLAUDE.md step 4) kept losing to "file it as #NNN" — see #558, #560, #637.

`~/.claude/hooks/github_merge_gate.py` blocks a sweep PR whose body lacks:

```
Class:     <the shape the code gets wrong>
Searched:  <files / globs>
Instances: <N found / N fixed / N deferred → #NNN (reason)>
```

…or whose numbers don't add up (`fixed + deferred != found`).

**While the budget is CLOSED, a deferred instance is counted but not
automatically filed** (net-zero, above). It is filed only if the same PR closes
a `sweep-finding` or Doug approves it. Otherwise the PR body says so —
`N deferred → not filed (net-zero; close-out list)` — and the instance goes on
the session's close-out list. The gate reads only the three numbers, so that
line passes as written.

These rules are enforced by this file, not by a hook (decided 2026-09-10).
`~/.claude/hooks/session_checklist.py` could be taught to stop a
`sweep-finding` filing while the budget is CLOSED; add that only if the rule
gets ignored.

---

## Changing phase

Doug declares the phase. Claude does not switch it, and does not treat the exit
condition as met without naming the evidence: the label count, and the two
issue numbers with their closed state.

## History

| date | phase | ended by |
|---|---|---|
| 2026-09-07 | HARDENING | *(current — money half met; sweep backlog 22, needs <5)* |
