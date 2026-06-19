> **⚠ MOVED 2026-04-20** — The Plan O graph runtime described here was extracted to `~/Desktop/gdx-orchestrator/`. The legacy autonomous-loop machinery (watcher, gemma_implementer, etc.) was deleted from this repo. systemd units stopped + removed. See `~/Desktop/gdx-orchestrator/LEARNINGS.md` for the full migration story. This document is preserved as a historical reference.

---

# Local LLM Integration

How the GDX orchestrator (and any future feature that wants a local language model) talks to the two local endpoints running on Doug's workstation, and — critically — how **vLLM is structurally different from the other local LLM servers we have used in the past**.

This doc exists because memory files decay between sessions; the knowledge here is load-bearing for the orchestrator loop and needs a permanent home.

---

## Live endpoints

| Name     | URL                                             | Container     | Backend       | Model               | Status (2026-04-18) |
|----------|-------------------------------------------------|---------------|---------------|---------------------|---------------------|
| primary  | `http://127.0.0.1:11440/v1/chat/completions`    | `localvllm`   | **vLLM**      | `gemma-4-26b-a4b`   | **healthy**         |
| fallback | `http://127.0.0.1:11430/v1/chat/completions`    | `localai`     | llama-swap (llama.cpp) | `gemma-4:26b-a4b`, `gemma-4:31b` | broken (abort core dump on cold start) |

Check container health before pointing new code at the fallback: `docker logs localai --tail 50`. If you see `signal: aborted (core dumped)`, don't use it.

vLLM is launched with:

```
vllm serve --model /model --dtype float16 --gpu-memory-utilization 0.92 \
  --max-model-len 32768 --served-model-name gemma-4-26b-a4b \
  --trust-remote-code --enable-prefix-caching --enable-chunked-prefill
```

Those flags matter — see "Prefix caching" and "Max context" below.

---

## Why vLLM is not just another chat-completions endpoint

LocalAI, Ollama, and raw `llama-server` (from llama.cpp) all expose an OpenAI-compatible `/v1/chat/completions` endpoint, and they all silently ignore fields they don't understand. vLLM accepts the same shape but supports **additional features that silently work** — meaning code that uses those features falls back gracefully on non-vLLM backends, but gets dramatically more reliable output on vLLM.

The three that matter today:

### 1. Constrained JSON decoding via `response_format` with `json_schema`

**What it does.** vLLM uses xgrammar/outlines to constrain token sampling so the output matches your JSON schema. Output is *guaranteed* valid JSON with the exact field names and types you specified — no prompt engineering, no `_extract_json` post-processing, no hallucinated field names.

**Non-vLLM endpoints ignore this.** LocalAI does not implement json_schema. Ollama does not. llama-server does not.

**What it looks like:**

```python
import json
import urllib.request

body = {
    "model": "gemma-4-26b-a4b",
    "messages": [
        {"role": "system", "content": "..."},
        {"role": "user", "content": "..."},
    ],
    "max_tokens": 1024,
    "temperature": 0.2,
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "my_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "verdict": {"type": "string", "enum": ["accept", "redo", "blocked"]},
                    "reason": {"type": "string"},
                },
                "required": ["verdict", "reason"],
                "additionalProperties": False,
            },
        },
    },
}
req = urllib.request.Request(
    "http://127.0.0.1:11440/v1/chat/completions",
    data=json.dumps(body).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
with urllib.request.urlopen(req, timeout=120) as resp:
    data = json.loads(resp.read())
# data["choices"][0]["message"]["content"] is guaranteed valid JSON matching schema.
```

**The weaker alternative** (`response_format: {"type": "json_object"}`) enforces "valid JSON" but does NOT enforce field names — the model picks its own field names. Always prefer `json_schema` when you want specific fields.

**Defensive note.** If your code might be repointed at a non-vLLM backend, still include an explicit JSON-shape instruction in your prompt AND keep a `_extract_json` helper that strips markdown code fences — the combination is monotonic: vLLM ignores the prompt instruction because it already enforces the schema, non-vLLM ignores the `response_format` field and falls back to the prompt instruction.

### 2. Prefix caching

`--enable-prefix-caching` caches the KV state of the system prompt. If you call the same system prompt with 50 different user messages, call #2..#50 are much faster. The orchestrator's reviewer benefits from this because the protocol prompt is identical every tick.

Non-vLLM backends don't cache across requests like this.

### 3. Tool calling

vLLM supports OpenAI-style tool calling for models that have been fine-tuned for it (Mistral, Hermes, some Llama variants). **Gemma-4 does NOT support tool calling reliably** — do not try to use it. For structured extraction on Gemma, use json_schema instead.

---

## Other gotchas

- **Max context** — vLLM here is set to `max_model_len=32768`. Prompts larger than that get refused. Our review prompts run ~18KB (~4500 tokens) so there's headroom, but if you start dumping full sprint files or large result files, truncate defensively.
- **`max_tokens` is strict** — vLLM stops at exactly `max_tokens`. Undersize → truncated JSON (and json_schema will error out rather than give you partial data). Size generously.
- **`stop_reason` is an integer token id, not a string** — on Gemma, `stop_reason: 106` is `<end_of_turn>`. Don't try to pattern-match as a string.
- **Non-streaming is fine.** Streaming is supported if you want it, but for short structured outputs there is no latency benefit.
- **GPU is shared.** The vLLM container uses ~24GB of the GPU's VRAM. Other models can't co-reside. If someone else needs the GPU, stop the container.

---

## How the orchestrator uses this today (2026-04-18)

The `local_ai_reviewer` dispatch script (`gdx_dispatch/tools/orchestrator/local_ai_reviewer.py`) is a drop-in replacement for `codex exec <prompt>`:

1. The watcher's `ready_for_codex` branch calls `_select_reviewer_cmd()` which consults `config.reviewer_priority` + `config.reviewer_backends` in `ai-queue/orchestrator_status/config.json`.
2. When `reviewer_backends.local_ai.enabled == true`, the watcher runs `.venv/bin/python -m gdx_dispatch.tools.orchestrator.local_ai_reviewer "<prompt>"`.
3. The reviewer reads the beacon + current_result.md + sprint header, POSTs to vLLM with a `json_schema`-constrained request, and parses the verdict.
4. It executes the side-effects the Codex prompt describes: queue redo task, write Q-block, or park the beacon for supervisor review — never silently accepts ambiguous output.

Adding a new reviewer backend (claude_fallback, cerebras, etc.) is a pure config change + a new dispatch script that follows the same "read → decide → side-effect" pattern. No watcher code change needed.

---

## If you change anything here

When you swap a model, change ports, add a new reviewer, or change the schema: update this doc in the same commit. The orchestrator loop depends on its accuracy.
