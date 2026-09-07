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
2. **Both live money defects closed:**
   - **#422** — a payment landing after a void resurrects the invoice to paid,
     with its parts already released.
   - **#445** — GL §6 refund-on-overpayment double-dips; live on prod (the
     audit's "latent, flag off" premise is stale).

### Next phase, already queued

**FEATURE — customer statements.** The 3-PR plan is written; 7 decisions are
owed by Doug before PR A. Nothing starts until the exit condition above is met.

### While hardening

- **No new sweeps** while the sweep budget is CLOSED (see below).
- **No new feature work** without Doug saying so explicitly. A feature request
  from Doug overrides this file; a feature idea from Claude does not.
- `live-defect` work is **never** rate-limited. If a user can hit it on prod
  today, it gets fixed regardless of budget.

---

## The sweep budget

Our own audits produce most of the backlog. Over the 30 days to 2026-09-07 we
closed 47 issues and opened 48 — the queue is at parity, so running another
sweep is choosing more backlog, not less.

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

---

## Changing phase

Doug declares the phase. Claude does not switch it, and does not treat the exit
condition as met without naming the evidence: the label count, and the two
issue numbers with their closed state.

## History

| date | phase | ended by |
|---|---|---|
| 2026-09-07 | HARDENING | *(current)* |
