> **⚠ MOVED 2026-04-20** — The Plan O graph runtime described here was extracted to `~/Desktop/gdx-orchestrator/`. The legacy autonomous-loop machinery (watcher, gemma_implementer, etc.) was deleted from this repo. systemd units stopped + removed. See `~/Desktop/gdx-orchestrator/LEARNINGS.md` for the full migration story. This document is preserved as a historical reference.

---

# Orchestrator Architecture

The orchestrator serves as the central nervous system for managing complex automated workflows and system states. It coordinates various specialized agents to ensure tasks are executed, monitored, and corrected in real-time. By decoupling observation from action, the architecture maintains high resilience and scalability. The system relies on a continuous feedback loop where state changes trigger specific responses across the component ecosystem.

* Watcher: Monitors system state and detects changes or deviations from the desired configuration.
* Auditor: Validates all actions and state changes against predefined policies and compliance rules.
* Implementers: Execute the specific tasks and configuration changes required to reach the target state.
* Push Daemon: Manages the distribution and deployment of updates to target endpoints.
* Health Monitor: Tracks the operational status and performance metrics of all active components.
* Reaper: Cleans up orphaned processes, stale resources, or failed task remnants.
* Canary: Deploys changes to a small subset of the system to validate stability before full rollout.
* Threshold Trigger: Initiates automated responses or alerts when specific metric limits are breached.

## Reviewer Backends

The watcher's `ready_for_codex` transition routes to whichever **reviewer** is enabled in `ai-queue/orchestrator_status/config.json`. A reviewer is a subprocess invoked with a single prompt arg — it reads `ai-queue/claude_to_codex/current_result.md`, makes an accept/redo/blocked decision, and executes the corresponding side-effect (queue the next Claude task, file a Q-block, or park the beacon). Codex CLI was the original reviewer; as of 2026-04-18 the system supports multiple reviewers with per-backend on/off flags so we can swap them without code changes.

Config shape (`config.json`):

```json
"reviewer_priority": ["local_ai", "codex"],
"reviewer_backends": {
  "codex":    {"enabled": false, "cmd": ["codex", "exec"]},
  "local_ai": {"enabled": true,  "cmd": [".venv/bin/python", "-m", "gdx_dispatch.tools.orchestrator.local_ai_reviewer"],
               "endpoint": "http://127.0.0.1:11440/v1/chat/completions",
               "model": "gemma-4-26b-a4b"}
}
```

Selection walks `reviewer_priority`, picks the first backend whose `enabled: true`, and runs `backend.cmd + [prompt]`. If every backend is disabled, falls back to the legacy `codex_cmd_v2` so the loop never bricks on a misconfig. Health monitor's rate-limit detector respects `enabled: false` — a disabled backend's historical failures no longer halt the loop.

See `gdx_dispatch/docs/LOCAL_LLM_INTEGRATION.md` for the local_ai reviewer's vLLM integration details, including the constrained-decoding mechanism that distinguishes vLLM from other local LLM backends.

Adding a new reviewer (e.g. `cerebras`, `claude_fallback`):

1. Write a dispatch module that takes a prompt arg and performs the read → decide → side-effect cycle (see `local_ai_reviewer.py` as the reference implementation).
2. Add it to `reviewer_backends` in `config.json` with `enabled: false` initially.
3. Add tests that pin verdict → side-effect mapping for the new dispatcher.
4. When ready, flip its `enabled` to `true` and demote the previous reviewer by flipping its `enabled` to `false`. No watcher code change.
