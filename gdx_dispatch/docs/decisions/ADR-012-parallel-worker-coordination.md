> **⚠ MOVED 2026-04-20** — The Plan O graph runtime described here was extracted to `~/Desktop/gdx-orchestrator/`. The legacy autonomous-loop machinery (watcher, gemma_implementer, etc.) was deleted from this repo. systemd units stopped + removed. See `~/Desktop/gdx-orchestrator/LEARNINGS.md` for the full migration story. This document is preserved as a historical reference.

---

# ADR-012: Parallel Worker Coordination — Role-Scoped Skills, Git Worktrees, Work-Orders, Branches, and Merge Queue

Date: 2026-04-18
Status: Proposed

## Context

ADR-011 defined five agent roles (Director, Architect, Workers, Reviewers, Monitors) but relied on disciplinary enforcement — "Architect should not do slice work." Discipline fails. The session-lessons file documents at least three concrete drift events in an earlier session alone (D53 scope-creep absorption: autonomous commits swept Architect's staged files into their own commits, overwriting Verification Manifests). The rule existed; the mechanism to prevent violation did not.

Three related pressures forced this ADR:

1. **Token cost.** `/start` costs ~75–85k tokens per full load (measured 2026-04-18). Running it on every Worker dispatch wastes context that Workers don't need.
2. **No parallel execution.** Today's autonomous loop is strictly serial — one slice at a time enforced by the beacon state machine. Throughput is capped at `1 worker × slice duration`.
3. **Industry converged on a stack we don't have.** Research conducted 2026-04-18 (log: `ai-queue/rd/operations/research_merge_coordination_2026-04-18.md`) found that February 2026 every major tool shipped multi-agent capabilities (Grok Build 8 agents, Windsurf 5, Claude Code Agent Teams, Codex CLI, Devin parallel sessions). Cursor 3 (April 2026) added `/worktree`. Claude Code Agent Teams use git worktrees + shared task list. We are behind the industry standard by about one release cycle.

Repo audit (2026-04-18) found we have ~35% of the stack already: a strong coordination layer (`plans/platform-sprints/state/` with task_graph, dependency_registry, contract_registry, ownership_map, integration_queue), AI-assisted merge-conflict resolution (`gdx_dispatch/tools/orchestrator/merge_bot.py`, scoped to `ai-queue/` files), a sprint-file-as-shared-task-list, and solid beacon dispatch. The missing pieces are structural: filesystem isolation (no worktrees in use), branch discipline (all commits direct to `main`), CODEOWNERS, merge queue, and a work-order schema with explicit `files OUT` / API-contract refs / approver fields.

## Decision

Adopt a five-part integrated pattern: **Role-scoped skills + Work-orders + Git worktrees + Branch discipline + Merge queue.** Each part structurally enforces what ADR-011 only named.

### 1. Role-scoped skills

Every interactive or dispatched agent session opens by invoking a role-specific skill. The skill loads only the context needed for that role and restricts tool availability to role-appropriate actions. Four skills plus a fallback picker:

| Skill | Weight | Loads | Tool surface | Blocks |
|---|---|---|---|---|
| `/architect` | heavy — today's `/start` | sprint file, memory, north star, ADR index, platform-sprints state | sprint edits, ADR writing, plan writing, delegate-to-worker, scope-doc writing | slice-level code commits; writes to `worker/*` branches |
| `/worker <work-order>` | light | the named work-order file + scoped files declared in it + CLAUDE.md Build Rules | edit scoped files only; run scoped tests; commit to `worker/<slice-id>` | writes to `sprint`, `plans/`, `memory/`, `CLAUDE.md`, `.github/`, `gdx_dispatch/docs/decisions/` |
| `/reviewer <sha>` | light | commit diff, drift guards, ADR-010/011/012 summaries | comment on handoff files, block merges, post review verdict | mutation of sprint state or any source file |
| `/start-picker` | minimal | just the role menu | prompt for role, invoke selected skill | anything else |

**Invocation rule:** Doug (or dispatch) invokes explicitly by skill name. If no role specified, `/start-picker` runs and asks. Autonomous dispatch invokes the appropriate skill for its role directly.

**Amendment to ADR-011 — Monitor role retired.** ADR-011 defined five roles including Monitors ("watch process state, write alerts, never fix"). Research and direct observation (see `ai-queue/rd/operations/research_merge_coordination_2026-04-18.md` addendum) showed the Monitor pattern (a) has no analog in industry multi-agent systems — Claude Code Agent Teams, Cursor 3, and Codex subagents all handle observability via Team-Lead-receives-teammate-messages-automatically + on-demand dashboards, never a dedicated watcher role; (b) burns tokens by firing the same CRITICAL alerts into every Architect prompt via `UserPromptSubmit`, often unchanged for hours; (c) mostly watches for conditions that disappear or change ownership in a parallel-worktree world. Work previously assigned to Monitors moves as follows:

| Previous Monitor responsibility | New home |
|---|---|
| Beacon-stuck detection | Worker self-timeout + self-report on failure |
| Redo-cap classification | Reviewer — "2 redos on same slice = escalate to Architect" |
| Silent-failure scanner runs | Pre-commit detector; result is Reviewer input, not an alert stream |
| Canary / detector-of-detectors | CI job or pre-commit gate, not a persistent service |
| Bug triage daemon | Worker (classification work is Worker scope) |
| Cost/token observability | Pull-only dashboard (existing at `http://127.0.0.1:9090/`); never pushed into Architect context |
| Production health (prod DB / VPS / domain) | Director-facing dashboard, checked on-demand, not at every prompt |
| `health_alert_prompt.py` UserPromptSubmit hook | Retired; replaced by on-demand dashboard read at `/architect` session open |

Net effect: four agent roles (Director, Architect, Workers, Reviewers), not five. The observability surface stays — it just moves from push to pull.

### 2. Work-order schema

Architect writes work-orders; Workers execute them. A work-order is a markdown file with this schema:

```
# <slice-id>: <one-line objective>
approver: <codex | gemma-auditor | human>
branch: worker/<slice-id>
files_in: [explicit list — files the Worker may read and edit]
files_out: [explicit list — files the Worker is expected to produce or modify]
api_contracts: [refs to plans/platform-sprints/state/contract_registry.md entries if any shared interface is touched]
acceptance_criteria: [testable bullets]
forbidden_paths: [sprint, plans/, memory/, CLAUDE.md, .github/, gdx_dispatch/docs/decisions/]
parallel_work_note: [what OTHER slices are running now; what this slice must NOT touch]
manifest_requirement: [Verification Manifest required — yes/no; default yes]
```

Work-orders live at `ai-queue/work-orders/<slice-id>.md`. The existing `current_task.md` handoff pattern continues for in-flight state; work-orders are the durable spec.

### 3. Git worktrees for filesystem isolation

Each Worker spawns in a git worktree, not the main checkout. The `Agent` tool with `isolation: "worktree"` already provides this primitive; the autonomous loop's watcher must be taught to use it.

```
# Architect / watcher spawn:
git worktree add ../gdx-worker-<slice-id> worker/<slice-id>
# Worker runs in that directory; main checkout is untouched
# On completion:
git worktree remove ../gdx-worker-<slice-id>
```

Effects: physically impossible for two Workers to edit "the same file" — each has an independent filesystem. Scope-creep becomes mechanically bounded.

### 4. Branch discipline

Autonomous Workers commit to `worker/<slice-id>` branches only. The main branch is merged via PR, not direct push. The watcher's subprocess-dispatch (`gdx_dispatch/tools/orchestrator/watcher.py`) changes from "commit + push to main" to "commit to branch + push to origin worker/<slice-id>".

CODEOWNERS (`.github/CODEOWNERS`) gates merge authorization. **Important:** CODEOWNERS requires actual GitHub accounts — AI agents (Codex, Gemma auditor) cannot appear here. Approval has two tiers:

- **Tier 1 — work-order approver** (pre-merge quality gate): the `approver:` field in the work-order file names Codex or Gemma-auditor or human. This is the Reviewer role per ADR-011. AI review lives here.
- **Tier 2 — CODEOWNERS** (merge authorization, GitHub-level): **human only**. For the solo-founder phase, that's Doug for all sensitive paths. When collaborators are added later, CODEOWNERS grows; the work-order approver layer stays where it is.

Initial `.github/CODEOWNERS` draft (Tier 2 only):
- `gdx_dispatch/docs/decisions/**` → Doug
- `plans/**`, `memory/**`, `active_sprint.md` → Doug
- `gdx_dispatch/tools/orchestrator/**`, `infra/**`, `.github/**` → Doug
- all other paths → Doug (default, can loosen later)

### 5. Merge queue for integration

GitHub Merge Queue is **not available** on Free or Team plans for private repos (only public repos or Enterprise Cloud), so we build on existing in-house tooling instead. `merge_bot.py` already does AI-assisted conflict resolution for `ai-queue/` files; this ADR extends it to act as the merge queue:

- **Queue semantics:** when a `worker/<slice-id>` branch is marked ready (Tier 1 work-order approver signed off), `merge_bot.py` puts it in a local queue. The queue serializes: take one branch, rebase onto latest `main`, run CI, merge on pass, bounce on fail. One-at-a-time integration, like Bors-NG's pattern.
- **Semantic conflict resolution:** when rebase produces conflicts, `merge_bot.py`'s existing vLLM-assisted resolver attempts a fix. Scope expands incrementally — start with test fixtures + generated files, add production code paths only after a track record.
- **No third-party dependency.** Everything runs on our own infrastructure. Zero ongoing cost since vLLM is local.

If the in-house queue ever becomes a bottleneck or reliability concern, Mergify (free-tier for private-repo eligibility to be verified) is the first fallback; GH Enterprise Cloud upgrade is the nuclear option.

### 6. API-first slice sequencing

When two slices would touch a shared interface, the Architect ships the interface contract as its own slice FIRST. The contract lives at `plans/platform-sprints/state/contract_registry.md`. Downstream slices consume the frozen contract; they cannot modify it. This is the "think first, code later" pattern from Amazon / Airbnb RFC practice.

For the GDX product API: OpenAPI spec gets generated from FastAPI route annotations and checked in as source of truth (Command Center already does this). Drift between checked-in spec and runtime-generated spec is a CI failure.

### 7. Session checkpoint and `/clear` workflow

Role-scoped skills + work-orders enable a session pattern where Architect doesn't have to hold full project context for the life of the work. The natural rhythm:

1. **Work a discrete unit** — a slice design, an ADR draft, a cross-cutting review, a work-order authoring pass.
2. **Commit durable state** — the ADR, the work-order, the sprint update, the Verification Manifest.
3. **`/clear`** — discard conversation.
4. **Next session invokes `/architect`** (or `/worker`, `/reviewer`) fresh, loading only what that role needs from files.

The mechanism works because files are the bridge: sprint file, work-order files, manifest, memory registry files, and per-sprint-block "Next session opening move" notes capture decisions durably. Fresh-Claude never needs the conversation transcript because the facts are on disk.

**Natural checkpoint triggers:**
- A D-item closes and commits.
- A slice ships (Worker commits `worker/<slice-id>`, Reviewer accepts).
- An ADR or plan lands.
- A major investigation finishes (findings written to a registry file).
- Topic switch ("done with silent failures, now mobile verification").

**Bad checkpoints:**
- Mid-investigation with un-committed reasoning.
- When on-disk state hasn't captured the decision arc.
- When the next step depends on short-term details only in chat.

**Prerequisites that make `/clear`-safe checkpointing possible** (load-bearing — violating any of these means fresh-Claude loses state):

1. Sprint file is a summary, not a narrative — "what's done / what's queued / why blocked."
2. Manifest Blind Spots are **tagged** (`CLOSED` / `DEFERRED` / `ACCEPTED`) — see D39's tagging standard.
3. Registry files (silent-failure registry, caller-audit registry, `contract_registry.md`) capture **decisions**, not just findings.
4. Each sprint block ends with a **"Next session opening move"** paragraph — a gift from past-me to future-me.

**Complementary token-saving practices:**
- Subagents for bounded research — context lives in the subagent, main Architect stays lean.
- Gemma / Cerebras dispatch for classification — doesn't consume Architect session context.
- Files over chat — "for future-me = file; for Doug right now = chat."
- Terse updates.

**Measurement:** session-token usage per role tracked in the pull dashboard. Baselines captured to `ai-queue/helper-performance/session_baselines.jsonl` at session close. Success criterion = measurable token-cost reduction for comparable work vs. continuous-session baseline.

This section absorbs the content of `plans/proposed-checkpoint-and-clear-workflow.md`. That proposal is SUPERSEDED by this ADR; the previously-planned ADR-015 slot is no longer needed.

## Implementation sketch

The ADR decides the shape. Actual implementation follows as bounded work-orders:

| Work-order | Touches | Effort |
|---|---|---|
| Create 4 role skill files + `/start-picker` at `~/.claude/skills/` (user-scoped) | new files | S |
| Upgrade `current_task.md` format + write 1 real work-order | `ai-queue/work-orders/` | S |
| Write `.github/CODEOWNERS` | new file | S |
| Teach `watcher.py` to spawn Workers in worktrees + commit to `worker/<slice-id>` | `gdx_dispatch/tools/orchestrator/watcher.py` | M |
| Add Worker self-timeout + `report_agent_job_result` semantics (industry pattern — workers that exit without reporting are marked errored) | `gdx_dispatch/tools/orchestrator/watcher.py` + skill | M |
| Retire `health_alert_prompt.py` UserPromptSubmit hook; wire on-demand dashboard read into `/architect` session open | `.claude/settings.json`, `/architect` skill | S |
| Audit existing systemd Monitor services (`supervisor-cron`, `health_monitor`, `drift_watcher`, `canary`, `bug_triage`) — decide per-service: keep / narrow / stop | unit files + service code | M |
| Extend `merge_bot.py` to act as merge queue (rebase + test + merge one-at-a-time on `worker/*` branches) | `gdx_dispatch/tools/orchestrator/merge_bot.py` | M |
| Extract GDX OpenAPI spec to checked-in file + CI drift check | new file + `.github/workflows/` | M |
| Expand `merge_bot.py` eligible-paths policy | `gdx_dispatch/tools/orchestrator/merge_bot.py` | S |

Sequence: skills + CODEOWNERS + work-order format first (cheap, unblocks the rest); retire the alert hook early to reclaim the token savings; watcher change + worker self-report + merge queue next; Monitor-services audit + OpenAPI + merge-bot expansion last.

## Consequences

### What improves

- **Scope creep becomes mechanically impossible.** A Worker skill doesn't have tool access to `plans/` or `memory/`; a Worker worktree literally can't commit to `main`. D53 class closed.
- **Token cost drops per Worker.** A Worker session loads ~5–8k tokens (work-order + scoped files + Build Rules) instead of 75–85k (`/start` full load). 10× savings per Worker dispatch.
- **Parallel execution becomes possible.** N Workers in N worktrees = N-way parallelism. Bottleneck shifts to Architect slice-design quality and merge queue serialization.
- **Merge conflicts bounded by design.** Non-overlapping `files_in`/`files_out` prevents most conflicts. The ones that remain get AI-assisted resolution via expanded `merge_bot.py`.
- **Industry alignment.** We match April-2026 Cursor 3 / Claude Code Agent Teams practice; no pioneering required for the core pattern.
- **Architect's work becomes leverage.** Slice decomposition + work-order design is the single quality gate. That's where the Architect role is highest-leverage.
- **Architect context stays clean.** Retiring the always-on `UserPromptSubmit` alert hook eliminates the same CRITICAL re-firing into every prompt. Architect reads the dashboard on-demand at session open; alerts live in a pull-only view. Roles drop from five to four (Monitor retired per amendment above).

### What this costs

- **Disk usage.** Each worktree is a full checkout (~500MB–1GB for GDX). N parallel workers = N × that. Cheap but not free.
- **Architect slice-design discipline is now load-bearing.** Overlapping `files_in` lists → merge conflicts. Missing API-first sequencing → broken contracts. The Architect role becomes the bottleneck by design; this is correct but intense.
- **Skill-maintenance overhead.** Four role skill files + picker must stay in sync with each other and with ADR-011/012 as the system evolves.
- **One-time migration cost.** Existing autonomous loop expects serial main-branch commits. Watcher rewrite is medium effort.
- **Building our own merge queue.** Choosing to extend `merge_bot.py` instead of GH Merge Queue or Mergify means we own the queue logic, the failure modes, and the maintenance. The upside is zero ongoing cost and full control; the downside is code we didn't have to write if we'd upgraded the GitHub plan.
- **Loss of always-on alert visibility.** Retiring `UserPromptSubmit` means the Architect no longer gets passive notice of silent detector findings. Architect has to open the dashboard. Real cost if Architect forgets to check.

### How we'll know it's working

- No D53-class scope-creep events for 30 days after implementation.
- At least one session runs two Workers in parallel, both land via PR with no conflicts.
- Worker session tokens drop to <10k on a typical dispatch (measured via a Worker-session token counter that lands with the skill).
- `contract_registry.md` has at least 3 entries referenced by downstream work-orders within 60 days.

## Known blind spots

- **`Agent` tool's `isolation: "worktree"` semantics may differ from Cursor 3's `/worktree`.** Haven't tested side-by-side.
- **In-house merge queue (`merge_bot.py` extension) has never been battle-tested as a queue** — today it only does conflict resolution on `ai-queue/`. Turning it into a true queue (rebase-test-merge serialization, failure handling, retry logic) is unproven code. Risk of subtle bugs at integration time.
- **AgenticFlict's 27% Claude Code merge-conflict rate** is from a mixed orchestrated + solo dataset; I haven't read the paper carefully enough to know how much of it applies to our specific pattern. Could be worse than projected.
- **DDD bounded-context decomposition for GDX has never been done.** Today's slice-ids are ad-hoc. The API-first sequencing rule depends on bounded contexts existing, which they don't yet formally.
- **Shared task list for parallel Workers** — Claude Code Agent Teams docs say all agents read/write a shared list; whether that's a file, a protocol, or a convention is not verified. Our `active_sprint.md` + `ai-queue/work-orders/` is a file-based approximation.
- **Merge-bot expansion to production code paths** is a risk: AI-resolved conflicts in `gdx_dispatch/core/` or `gdx_dispatch/routers/` could ship subtle bugs. Expansion policy must gate on reviewer sign-off until a track record accumulates.
- **Monitor-retirement blind spot.** We haven't quantified the token cost savings from retiring the UserPromptSubmit alert hook — the claim is plausible but unmeasured. Also, genuine high-severity events (production outage, security incident) need SOME surface; moving them to a dashboard means Architect must remember to check. If a real incident fires at session-open time and Architect doesn't open the dashboard, the signal goes unseen.
- **Existing systemd Monitor services (`supervisor-cron`, `health_monitor`, `drift_watcher`, `canary`, `bug_triage`) are still in place.** Retiring them is out of ADR scope; this ADR only retires the role + the always-on alert hook. Per-service audit is a separate work-order.

## Resolved by Director (2026-04-18)

- **Skill names:** `/architect`, `/worker`, `/reviewer`, `/start-picker` (full-word names).
- **Skill location:** `~/.claude/skills/` (user-scoped; skills travel with Doug's machine, not the repo).
- **Branch naming:** `worker/<slice-id>`.
- **CODEOWNERS:** Human-only (AI agents cannot be CODEOWNERS). Tier-2 GitHub merge authorization is Doug-only for the solo-founder phase. Tier-1 work-order `approver:` (Codex / Gemma auditor / human) is the AI review layer and does not appear in CODEOWNERS. Doug's GitHub username to be filled into `.github/CODEOWNERS` at implementation time.
- **Merge queue:** Extend `gdx_dispatch/tools/orchestrator/merge_bot.py` into an in-house merge queue (zero ongoing cost; vLLM is local). GH Merge Queue is not available on our plan for private repos; Mergify / Aviator reserved as fallbacks if the in-house queue proves unreliable.
- **Fold in `/clear` + checkpoint workflow:** an earlier session decision to absorb `plans/proposed-checkpoint-and-clear-workflow.md` into this ADR (Section 7) rather than splitting into a separate ADR-015. Net effect: 4 remaining ADRs in the refinement plan (012 this, 013 verification, 014 silent-failure, 016 beacon triage); previously-planned ADR-015 slot freed.
- **UserPromptSubmit retirement scope:** Fully retire `health_alert_prompt.py`. Observability moves entirely to pull-only dashboard. No narrowed / urgent-signal fallback. (Resolves former Q6.)
- **ADR numbering shape confirmed:** 012 (this), 013 verification, 014 silent-failure, 016 beacon triage. No ADRs planned beyond 016. (Resolves former Q9.)

## Out of ADR scope

- **Existing systemd Monitor services** (`supervisor-cron`, `health_monitor`, `drift_watcher`, `canary`, `bug_triage`) — per-service keep / narrow / stop decisions are work-order-level, not architecture-level. Handled by the Monitor-services audit work-order in the Implementation sketch.
- **Implementation sequence choice** — whether to ship skills-first-experiment-first or go straight to watcher rewrite is an execution-order question, not an architectural decision. The Implementation sketch proposes a sequence; the actual order is decided at work-order dispatch time.

## References

- ADR-011: Named Agent Roles (this ADR is its structural enforcement layer)
- D53: Autonomous-commit scope-creep absorbs supervisor staged work — the problem this solves
- D46: Checkpoint + /clear workflow — session-management complement
- Research log: `ai-queue/rd/operations/research_merge_coordination_2026-04-18.md`
- Repo audit (subagent output, 2026-04-18): `~35% stack readiness`; three biggest gaps identified
- Cursor 3 `/worktree` command (April 2026)
- Claude Code Agent Teams (MindStudio write-up)
- AgenticFlict dataset: `https://arxiv.org/html/2604.03551v1`
- Existing GDX primitives: `gdx_dispatch/tools/orchestrator/merge_bot.py`, `plans/platform-sprints/state/contract_registry.md`, `plans/platform-sprints/state/ownership_map.md`, `Agent` tool `isolation: "worktree"` flag
