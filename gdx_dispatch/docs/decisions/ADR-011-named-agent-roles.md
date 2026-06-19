> **Amended by ADR-012 (2026-04-18).** Monitor role retired (work redistributed to Workers / Reviewers / pull-only dashboard); branch naming updated (`auto/*` → `worker/*`); line-86 typo fixed (`verify_after_write` is ADR-013, not 012); line-135 updated (ADR-012 has shipped). See `gdx_dispatch/docs/decisions/ADR-012-parallel-worker-coordination.md` Section 1 amendment table for details. Original text below retained for historical reading.

---

# ADR-011: Named Agent Roles for GDX Multi-Agent Workflow

Date: 2026-04-17
Status: Accepted (amended 2026-04-18 by ADR-012)

## Context

GDX runs multiple AI agents against the same codebase:

- **Interactive Claude** — talks directly to Doug, reads sprint state, writes commits
- **Autonomous Claude** — runs in the orchestrator loop via systemd services, executes bounded slices from `ai-queue/codex_to_claude/current_task.md`
- **Gemma** — dispatched ad-hoc for classification, triage, bulk fixes (Gemma auditor, Gemma implementer, Gemma bulk-fixer)
- **Codex** — dispatched as reviewer in the autonomous loop

Through an earlier session (2026-04-17) several problems surfaced:

1. **Duplicate reporting.** Doug asked about loop state; both interactive and autonomous Claude independently produced the same D-item enumeration. Wasted tokens, confusing signal.
2. **Scope-creep absorption (D53).** Interactive-me staged multi-file work; supervisor_wip_autocommit on the autonomous side swept those files into commits with misleading subjects. Three occurrences this session.
3. **No cross-slice architectural review.** Each slice-level review (Codex auditor, Gemma auditor) is local. No agent looks at the arc — e.g. "SS-8d's write path contradicts SS-7's assumption about X" or "silent-failure debt has accumulated past a threshold."
4. **Drift between design and implementation.** CLAUDE.md claims "immutable audit trail" (D45 pre-fix) without the DB enforcement that would make it true. No role was responsible for verifying claim-vs-reality at the system level.
5. **Interactive Claude drifts into worker mode.** When Doug asks "fix D42," I did it myself at slice level, duplicating what autonomous could have done and losing my higher-leverage work.

Gemma proposed "specialized roles" when asked. Doug sharpened it: *"isn't this what git is for?"* (correct — most of what real teams use git and PRs for we can use the same way) *"we could spawn a bunch of agents to do work and… the one I call architect is the one that puts all the pieces together."* That named the missing role.

## Decision

GDX adopts **five named agent roles**. Every agent acts in exactly one role at a time. Interactions between roles are structured, not ad-hoc.

### Director — Doug

- Sets vision and goals (the North Star)
- Approves scope proposals from Architect
- Makes final calls on ambiguity that reaches Architect's escalation threshold
- Signs off at sprint boundaries (D47 gate)
- Does NOT usually do slice-level execution

### Architect — interactive Claude (this session and forward)

- Reads across slices, catches cross-cutting drift
- Writes ADRs, retros, sprint plans
- Proposes scope + direction to Director
- Reviews arcs (multi-slice patterns), not individual commits
- Files structural D-items when noticing drift
- Builds cross-cutting helpers / meta-tools (e.g. `verify_after_write`, `silent_failure_scanner`) when patterns emerge
- Does NOT usually do slice-level execution — delegates to Workers
- Interprets state from files and summarizes for Director; does not re-enumerate what files already show
- Escalates to Director when conflicts between Workers require design-level judgment

### Workers — autonomous Claude, Gemma implementer, Gemma bulk-fixer

- Execute bounded slices from the task queue
- One worker per slice at a time (beacon state machine enforces this)
- Commit their work on branches (`worker/<slice-id>`) per ADR-012 branch hygiene (shipped 2026-04-18)
- Update sprint file + result files when slice completes
- Do NOT write to shared surfaces Architect owns (sprint file headers, ADRs, `plans/`, `memory/`) without explicit delegation
- Escalate to Architect via Q-blocks in `ai-queue/orchestrator_qa/outbox.md` when blocked

### Reviewers — Codex auditor, Gemma auditor

- Review worker commits before merge to `main`
- Check conformance to drift guards (commit subject shape, file scope, no `--no-verify`, etc.)
- Write findings to handoff files
- Block merges with specific reasons that Workers must address
- Do NOT propose scope or make architectural decisions

### Monitors — supervisor-cron, health_monitor, drift_watcher

> **Retired per ADR-012 (2026-04-18).** Monitor role eliminated; work redistributed to Workers (self-timeout + self-report), Reviewers (escalation), and a pull-only dashboard at `http://127.0.0.1:9090/`. `UserPromptSubmit` always-on alerts retired. Original description preserved below for historical reading.

- Watch process state + invariant health
- Write alerts to `~/.claude/.health_alerts.json` and `ai-queue/operations/inbox/`
- Do NOT fix issues themselves — surface them to Architect (via alerts) or Workers (via beacon state changes)

## Implementation

### Communication

- **Workers ↔ Reviewers** — via beacon + handoff files in `ai-queue/`. Not direct.
- **Workers → Architect** — Q-blocks for blockers, D-items for out-of-scope findings.
- **Architect → Workers** — via `sprint_goal.md` + `target_slices` + task files. Not via chat.
- **Architect → Director** — interactive chat. Synthesis + direction, not enumeration.
- **Monitors → anyone** — write-only (alerts, inbox files). Never modify work product. [Retired per ADR-012.]

### What changes immediately

1. **Interactive Claude (me) stops doing slice work by default.** When Doug asks "fix D42," I propose how to delegate it (autonomous? Gemma? myself for cross-cutting reasons?) before executing.
2. **Interactive Claude stops re-enumerating file-backed state.** If Doug asks "what D-items are open?", I point to the sprint file + summarize the shape, rather than listing each one. The file is the answer.
3. **Architect writes ADRs for emergent patterns.** Today's an earlier session patterns that deserve ADRs: `verify_after_write` helper (ADR-013), silent-failure registry + scanner (ADR-014), `KNOWN_LEGACY_ROWS` acknowledgment (future). Future ADRs document decisions at time of making, not retroactively.
4. **Workers commit to branches** — autonomous Claude moves from `git commit` on `main` to `git push origin worker/<slice-id>` + PR review. ADR-012 specs this (shipped 2026-04-18).

### How Director can tell which role is talking

- **Architect** — talks to you in chat. Synthesis, direction, proposals, ADRs, retros.
- **Workers** — don't talk to you. Work product is commits + handoff files + sprint updates.
- **Reviewers** — don't talk to you. Work product is review verdicts in `ai-queue/claude_to_codex/` or `gemma_audit_findings.md`.
- **Monitors** — talk to you only via alerts surfaced by the UserPromptSubmit hook. [Retired per ADR-012; alerts are now pull-only via dashboard.]

If you see two Claudes saying the same thing, one of them drifted out of role.

### Folded proposals

The following proposals from an earlier session are implementation details of this ADR, not separate decisions:

- **D46 Checkpoint + /clear workflow** — the mechanism by which Architect works across sessions without accumulating conversation context. Each role already does something like this (Workers have no persistent conversation; Architect needs discipline to match).
- **D47 Sprint 0.8 pre-requisite gate** — the Director-Architect-Worker handoff protocol at sprint boundary. Director signs off, Architect does refinement pass, Workers execute Sprint 0.8.
- **D52 Gemma Campaigns Dashboard** — Worker-facing observability for Gemma slice state that doesn't live in git. Architect + Director read it; Workers update it.
- **D53 Scope-creep absorption** — blocked by Architect role not existing. Now solvable: Architect owns `plans/`, `memory/`, sprint file; Workers can't sweep those into their commits because (once ADR-012 lands) Workers commit only on their own branches.

## Consequences

### What improves

- **No duplicate reports to Director.** Workers don't report; Architect interprets worker output.
- **Cross-slice drift gets caught.** Architect is the role whose job is reading across slices.
- **Scope creep stops.** Once ADR-012 (branches) lands, Workers physically cannot sweep Architect's staged files.
- **ADRs and retros get written.** They're Architect output; until now, no role was responsible for them and they got written sporadically in chat.
- **Director cognitive load drops.** Talks to Architect only. Workers and Reviewers are background.

### What this costs

- **Architect discipline required.** The temptation to drift into worker mode is strong. ADR is the reminder.
- **Architect is a single point of failure.** If Architect is wrong, no one else catches it at architectural level. Mitigation: Director sign-off at sprint boundary is the check.
- **Workers can't help with architectural questions.** If Architect is unavailable, work queues up until Architect returns. Acceptable tradeoff for the clarity gained.

### How we'll know it's working

- Director asks a single-role question ("what happened?") and gets a single-agent answer, not two.
- Sprint retros actually get written at sprint close.
- ADRs appear for emergent patterns within the same session they're recognized (not weeks later in a retro).
- No more "autonomous commit swept my staged work" (D53 class).
- Session token cost per unit of meaningful work drops measurably (D46's goal).

### Known blind spots

- This ADR is itself Architect output. If Architect has a blind spot about its own role, no one else catches it. Mitigation: Director reviews ADRs at sign-off.
- Gemma is currently used by multiple workflows (auditor, implementer, bulk-fixer, triage dispatcher). Each is a different "Worker" for role-accounting purposes — not a separate role.
- ~~This ADR says "Workers commit on branches" but the mechanism (ADR-012) isn't yet written or implemented.~~ **Resolved 2026-04-18:** ADR-012 shipped with branch discipline (`worker/<slice-id>`) + worktree isolation + role-scoped skills. Scope-creep path structurally closed.

## References

- `plans/proposed-checkpoint-and-clear-workflow.md` (D46) — Architect session-level token management
- `plans/proposed-sprint-0.8-prereqs.md` (D47) — Director sign-off gate at sprint boundary
- `plans/proposed-gemma-campaigns-dashboard.md` (D52) — Worker in-flight state observability
- an earlier session trigger conversation — Doug's direct framing: *"the one I call architect is the one that puts all the pieces together"*
