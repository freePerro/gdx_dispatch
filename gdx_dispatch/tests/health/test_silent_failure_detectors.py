"""Silent-failure detectors — manifest-at-session-start health checks.

Each test is a drift detector, not a unit test. A red light in the VS Code
Test Explorer means the live system has drifted from its declared config —
investigate before the orchestrator loop trusts it.

Run: `pytest -m health -v gdx_dispatch/tests/health/`
Surface: VS Code Test Explorer (Python extension discovers automatically)

Design rules followed here:
    1. Tests must FAIL LOUD on drift (no swallowed errors, no skip-on-missing).
    2. Tests must check the LIVE state, not a mock or a log snapshot.
    3. Tests must name the fix in the assertion message, not just the failure.
    4. Tests must be hermetic-ish: no side effects on real beacon/task files.
       The alert-path detector writes ONLY heartbeat rows, clearly tagged.

Each detector maps to one of the 12 silent failures observed 2026-04-16
an earlier session. See ai-queue/operations/active_sprint.md for the full catalog.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.health

REPO_ROOT = Path(__file__).resolve().parents[3]
CLAUDE_HOME = Path(os.environ.get("CLAUDE_HOME") or (Path.home() / ".claude"))
ALERTS_JSONL = REPO_ROOT / "ai-queue" / "orchestrator_status" / "alerts.jsonl"
HEALTH_ALERTS_JSON = CLAUDE_HOME / ".health_alerts.json"
ORCHESTRATOR_STATUS = REPO_ROOT / "ai-queue" / "orchestrator_status"


# ─────────────────────────────────────────────────────────────────────────────
# Detector 1: Alert path reachability — the dead man's switch
# ─────────────────────────────────────────────────────────────────────────────

def test_alert_path_reachability():
    """Dead man's switch: write a heartbeat; verify the full notifier path is intact.

    Maps to an earlier session failure #12 ("The Silenced Alert"): settings.json validation
    error disabled ALL hooks including the UserPromptSubmit alert notifier; the
    orchestrator's stall-watchdog fired correctly but the alert never reached Doug.

    If any step here fails, the alert path is dead — every other detector below
    is a lie, because even when they fail they won't reach anyone.
    """
    # 1. Alert bus directory must be writable
    ORCHESTRATOR_STATUS.mkdir(parents=True, exist_ok=True)

    # 2. Append a heartbeat row (clearly tagged so the consumer can filter it out)
    heartbeat_id = f"health_heartbeat_{int(time.time() * 1000)}"
    heartbeat = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "id": heartbeat_id,
        "source": "test_alert_path_reachability",
        "severity": "HEARTBEAT",
        "body": "dead man's switch ping",
    }
    with open(ALERTS_JSONL, "a") as f:
        f.write(json.dumps(heartbeat) + "\n")

    # 3. Read back and verify the row landed (deque avoids loading the whole log into memory)
    with open(ALERTS_JSONL) as f:
        tail = deque(f, maxlen=20)
    recent = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            recent.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # malformed lines are a separate problem, not this test's concern
    assert any(r.get("id") == heartbeat_id for r in recent), (
        f"Wrote heartbeat {heartbeat_id} to {ALERTS_JSONL} but it is not readable. "
        "Alert bus is write-swallowing — investigate disk, permissions, concurrent writers."
    )

    # 4. Notifier hook must exist + be executable
    hook = CLAUDE_HOME / "hooks" / "health_alert_prompt.py"
    assert hook.exists(), (
        f"Alert notifier {hook} does not exist. "
        "This is The Silenced Alert failure class — the path to Doug is severed."
    )
    assert os.access(hook, os.X_OK) or hook.stat().st_mode & 0o444, (
        f"Alert notifier {hook} is not executable/readable."
    )

    # 5. Hook must parse (no SyntaxError). Use sys.executable so we're checking with
    # the same interpreter running pytest — `python3` on PATH may be a different env.
    parse_check = subprocess.run(
        [sys.executable, "-c", f"import ast; ast.parse(open({str(hook)!r}).read())"],
        capture_output=True, timeout=5,
    )
    assert parse_check.returncode == 0, (
        f"Alert notifier has SyntaxError or cannot be parsed: "
        f"{parse_check.stderr.decode()[:300]}"
    )

    # 6. health_monitor itself must have run recently — if the monitor is dead,
    #    no alerts are being generated to reach the notifier in the first place.
    monitor_status_path = ORCHESTRATOR_STATUS / "health_monitor_status.json"
    assert monitor_status_path.exists(), (
        f"{monitor_status_path} missing — health_monitor may never have run."
    )
    status = json.loads(monitor_status_path.read_text())
    last_check_str = status.get("last_check", "")
    assert last_check_str, "health_monitor_status.json has no last_check field"
    last_check = datetime.fromisoformat(last_check_str.replace("Z", "+00:00"))
    age = datetime.now(timezone.utc) - last_check
    assert age < timedelta(minutes=5), (
        f"health_monitor last ran {age} ago (>5 min) — monitor process is likely dead. "
        f"Check: systemctl --user status orchestrator-health-monitor (or equivalent)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detector 2: MCP bridge points at live vLLM with the right model name
# ─────────────────────────────────────────────────────────────────────────────

def test_mcp_local_generate_points_at_live_vllm():
    """MCP bridge's LOCAL_BASE/LOCAL_MODEL must reach a live vLLM serving that exact name.

    Maps to failure #1 (port 11430 → 11440 drift caused silent 502s for hours)
    and failure #2 (stale `gemma-4:26b-a4b` colon name 404s in vLLM logs still).

    Checks both axes, because either one being wrong silently breaks Gemma dispatch.
    """
    mcp_path = CLAUDE_HOME / "local-llm-mcp.py"
    assert mcp_path.exists(), f"MCP bridge {mcp_path} missing"
    mcp_source = mcp_path.read_text()

    port_match = re.search(
        r'LOCAL_BASE\s*=\s*["\']http://localhost:(\d+)/v1', mcp_source
    )
    model_match = re.search(r'LOCAL_MODEL\s*=\s*["\']([^"\']+)["\']', mcp_source)
    assert port_match, "LOCAL_BASE not parseable in MCP bridge — source drifted"
    assert model_match, "LOCAL_MODEL not parseable in MCP bridge — source drifted"

    port = port_match.group(1)
    expected_model = model_match.group(1)

    # Live vLLM must answer /v1/models and serve the expected name.
    # 10s timeout accommodates vLLM's slower response under load (prefix cache warmup
    # or concurrent inference from the orchestrator driver).
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/v1/models", timeout=10
        ) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        pytest.fail(
            f"MCP bridge declares port {port} but nothing answers: {e}. "
            f"Either vLLM is down OR the MCP bridge was left pointed at a dead port "
            f"(this is exactly failure #1 from an earlier session)."
        )
    except Exception as e:
        pytest.fail(
            f"MCP bridge declares port {port} but got {type(e).__name__}: {e}"
        )

    served = {m["id"] for m in data.get("data", [])}
    assert expected_model in served, (
        f"MCP bridge expects model {expected_model!r} but vLLM at port {port} "
        f"serves {served}. This is failure #2 — model name drift causes silent 404s."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detector 3: Database schema conformance — ORM vs live DB
# ─────────────────────────────────────────────────────────────────────────────

def test_database_schema_conformance():
    """Every ORM-declared column for our SS-2/SS-3 tables must exist in the live DB.

    Maps to failure #9 (SQLite pytest passed, real PG rejected `CREATE RULE IF NOT EXISTS`)
    and failure #10 (Identity.type missing — ORM model lied about runtime assumptions).

    This test runs against the CONTROL DB (configured via GDX_CONTROL_DB_URL).
    It does not run against per-tenant DBs — those need their own detector.
    """
    db_url = (
        os.environ.get("GDX_CONTROL_DB_URL")
        or os.environ.get("CONTROL_DB_URL")
        or os.environ.get("DATABASE_URL")
    )
    assert db_url, (
        "No control DB URL found (checked GDX_CONTROL_DB_URL, CONTROL_DB_URL, "
        "DATABASE_URL). Set one — skipping this test hides real drift."
    )

    # Import Base first so all registered tables are loaded before introspection.
    # We import the extension modules for their side-effect (registering tables
    # onto Base.metadata) but use Base as the single source of truth.
    import sqlalchemy.exc
    from sqlalchemy import create_engine, inspect

    from gdx_dispatch.models import platform_extensions  # noqa: F401
    from gdx_dispatch.control.models import Base

    # Context-awareness: if we can't reach the DB from this host, the detector
    # can't do its job HERE — but the drift it's looking for may still exist
    # AT the DB. Skip loudly with the reason, not silently, so the caller
    # knows to run this from a reachable host (e.g., the VPS).
    engine = create_engine(db_url)
    try:
        with engine.connect():
            pass
    except sqlalchemy.exc.OperationalError as e:
        pytest.skip(
            f"Control DB unreachable from this host: {type(e.orig).__name__}: "
            f"{str(e.orig)[:180]}. Run this detector from a host that can reach "
            f"the DB (e.g., the VPS, or via SSH tunnel). This is context-aware "
            f"skip, not silent drift — the test will run where it applies."
        )
    inspector = inspect(engine)

    drift: list[str] = []
    for tablename, table in Base.metadata.tables.items():
        if not inspector.has_table(tablename):
            drift.append(f"{tablename}: table not present in live DB")
            continue
        live_cols = {c["name"]: c for c in inspector.get_columns(tablename)}
        declared_cols = {c.name: c for c in table.columns}

        missing = set(declared_cols) - set(live_cols)
        if missing:
            drift.append(f"{tablename}: live DB missing columns {sorted(missing)}")

        # Nullable + type drift. Both are real drift classes we've hit.
        # - Nullable: ORM says NOT NULL, live says NULL (or vice versa).
        # - Type: ORM says String(255), migration shipped TEXT; or ORM says JSON, live has JSONB.
        # `str(type).upper()` uses SQLAlchemy's dialect-emitted DDL — "VARCHAR(255)" on
        # both sides normally matches. Project precedent: gdx_dispatch/tools/tenant_isolation_audit.py
        # uses the same plain-string pattern against information_schema.data_type.
        # False positives on first run should be normalized, not suppressed — they're
        # usually real silent drift.
        for name in set(declared_cols) & set(live_cols):
            declared_nullable = bool(declared_cols[name].nullable)
            live_nullable = bool(live_cols[name].get("nullable", True))
            if declared_nullable != live_nullable:
                drift.append(
                    f"{tablename}.{name}: nullable drift "
                    f"(ORM={declared_nullable}, live={live_nullable})"
                )
            declared_type = str(declared_cols[name].type).upper()
            live_type = str(live_cols[name].get("type", "")).upper()
            if declared_type and live_type and declared_type != live_type:
                drift.append(
                    f"{tablename}.{name}: type drift "
                    f"(ORM={declared_type!r}, live={live_type!r})"
                )

    assert not drift, (
        "Schema drift detected between ORM declarations and live DB:\n  "
        + "\n  ".join(drift)
        + "\nThis is the class of bug that hid Identity.type for a sprint cycle. "
        "Run an alembic autogenerate + review the diff."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detector 4: auto-accept-hook.sh integrity vs canonical
# ─────────────────────────────────────────────────────────────────────────────

def test_auto_accept_hook_integrity():
    """auto-accept-hook.sh must match the canonical baked into hook_validator.py.

    Maps to failure #3: the VS Code extension `tjcg.auto-accept-claude-code`
    clobbers this file on every VS Code launch. hook_validator.py self-heals
    at SessionStart, but this test catches drift BETWEEN self-heals (i.e.,
    the window where the bad version is active).

    Extension was uninstalled 2026-04-16 an earlier session, so this test should pass
    cleanly now — it becomes the canary that notices if the extension (or a
    replacement) is reinstalled.
    """
    hook_path = CLAUDE_HOME / "hooks" / "auto-accept-hook.sh"
    validator_path = CLAUDE_HOME / "hooks" / "hook_validator.py"

    assert hook_path.exists(), f"{hook_path} missing — alert path may be severed"
    assert validator_path.exists(), (
        f"{validator_path} missing — cannot read canonical content to compare against"
    )

    # Load hook_validator.py via importlib.util so we don't pollute sys.path or
    # sys.modules for other tests in the session (hook_validator is not a real
    # installable package; it's just a hook script that happens to be importable).
    spec = importlib.util.spec_from_file_location("hook_validator", validator_path)
    assert spec and spec.loader, f"Could not build import spec for {validator_path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # isolated — not registered in sys.modules
    canonical = module.AUTO_ACCEPT_CANONICAL

    actual = hook_path.read_text()

    # Compare via splitlines() so accidental CRLF drift (e.g., a Windows editor
    # saving the file) doesn't fail this test for a cosmetic reason.
    if actual.splitlines() != canonical.splitlines():
        assert False, (
            f"auto-accept-hook.sh drifted from canonical "
            f"(sha256 actual={hashlib.sha256(actual.encode()).hexdigest()[:12]}, "
            f"canonical={hashlib.sha256(canonical.encode()).hexdigest()[:12]}). "
            f"Likely cause: VS Code extension was reinstalled. "
            f"Fix: run hook_validator.py to restore from canonical."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Detector 5: Watchdog escalation — recurring WARNs must become CRITICAL
# ─────────────────────────────────────────────────────────────────────────────

def test_watchdog_escalation():
    """Alerts that recur N times must escalate to CRITICAL severity automatically.

    Maps to failure #6 (stall-watchdog fired 5x before a human noticed) and
    failure #7 (watcher at 0% CPU fired as WARN but never escalated).

    Without this detector, high-recurrence alerts silently become background
    noise — the Silenced Alert failure class repeats every session.
    """
    ESCALATION_THRESHOLD = 3  # after this many repeats, severity must be CRITICAL

    # Read from the canonical health_alerts location used by vllm_health_check.py.
    # Accept either shape:
    #   - single JSON object (dict or list)  → parse whole file
    #   - JSONL (newline-delimited records) → parse line-by-line
    # Different writers in the stack have used both; we accept both.
    if not HEALTH_ALERTS_JSON.exists():
        pytest.fail(
            f"{HEALTH_ALERTS_JSON} does not exist — no alert history is being "
            "persisted. Alert recurrence tracking cannot work without this file."
        )

    raw = HEALTH_ALERTS_JSON.read_text().strip()
    if not raw:
        pytest.skip(f"{HEALTH_ALERTS_JSON} is empty — no history to evaluate yet")

    records: list[dict] = []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            records = list(parsed.values())
        elif isinstance(parsed, list):
            records = parsed
        else:
            pytest.fail(
                f"{HEALTH_ALERTS_JSON} has unexpected root type {type(parsed).__name__}"
            )
    except json.JSONDecodeError:
        # Fall back to JSONL — line-by-line, skipping malformed lines with a count
        malformed = 0
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                malformed += 1
        if malformed:
            # Malformed lines in an alert file are themselves a silent-failure signal
            records.append({
                "id": "_meta_malformed_jsonl",
                "seen_count": malformed,
                "severity": "CRITICAL",
                "message": f"{malformed} unparseable JSONL lines in {HEALTH_ALERTS_JSON}",
            })

    unescalated: list[str] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        count = rec.get("seen_count") or rec.get("count") or rec.get("seen") or 0
        severity = (rec.get("severity") or rec.get("level") or "").upper()
        alert_id = rec.get("id") or rec.get("message", "unknown")[:60]
        if count >= ESCALATION_THRESHOLD and severity != "CRITICAL":
            unescalated.append(
                f"{alert_id!r}: seen {count}x, severity={severity!r} (should be CRITICAL)"
            )

    assert not unescalated, (
        f"Found {len(unescalated)} alert(s) repeated >= {ESCALATION_THRESHOLD} times "
        "without CRITICAL escalation:\n  " + "\n  ".join(unescalated)
        + "\nThis is the 'Silenced Alert' failure class — high-recurrence alerts are "
        "still WARN-only. Fix the escalation step in health_monitor / vllm_health_check."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detector 6: VPS is in sync with origin/main
# ─────────────────────────────────────────────────────────────────────────────

def test_vps_in_sync_with_origin():
    """VPS's /opt/gdx_dispatch checkout must be at origin/main. Maps to failure #8.

    an earlier session observed VPS 123 commits behind origin — silent for days.
    The drift had been growing invisibly; a detector like this catches it at
    +1 commit, not +123.
    """
    VPS_HOST = os.environ.get("GDX_VPS_HOST", "your-server")
    VPS_REPO = os.environ.get("GDX_VPS_REPO", "/opt/gdx_dispatch")

    # Reachability probe before the actual check.
    probe = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", VPS_HOST, "true"],
        capture_output=True, timeout=10,
    )
    if probe.returncode != 0:
        pytest.skip(
            f"ssh {VPS_HOST!r} unreachable (rc={probe.returncode}, "
            f"stderr={probe.stderr.decode()[:120]!r}). "
            f"Detector requires VPS SSH access."
        )

    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", VPS_HOST,
         f"cd {VPS_REPO} && git fetch origin 2>/dev/null && "
         f"git rev-list --count HEAD..origin/main"],
        capture_output=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"Could not compute drift on VPS: rc={result.returncode}, "
        f"stderr={result.stderr.decode()[:200]}"
    )
    raw = result.stdout.decode().strip()
    try:
        behind = int(raw or "0")
    except ValueError:
        pytest.fail(
            f"git rev-list returned non-numeric output: {raw[:120]!r}. "
            f"Likely a remote error (lock file, permission denied, corrupt ref). "
            f"stderr: {result.stderr.decode()[:200]}"
        )
    assert behind == 0, (
        f"VPS {VPS_REPO} is {behind} commits behind origin/main. "
        f"This is failure #8 — deploy this drift or pull it. "
        f"`ssh {VPS_HOST} 'cd {VPS_REPO} && git pull --ff-only'` if safe."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detector 7: Agent progress velocity — no wheel-spinning
# ─────────────────────────────────────────────────────────────────────────────

def test_agent_progress_velocity():
    """When the orchestrator beacon says *_in_progress, commits must be fresh.

    Maps to failure #5 (Gemma bulk-fixer wheel-spinning, no commits landed
    for hours, only visible in JSONL tail) and failure #6 (stall-watchdog
    fired 5x for the same blocker).

    Logic: if the beacon claims active work but no commits landed recently,
    the agent is stuck — either orphaned or wheel-spinning on a fix it can't
    produce. Surfaces the same signal the stall-watchdog emits, but at the
    pytest surface (VS Code Test Explorer) instead of only in alert logs.
    """
    MAX_GAP_MINUTES = 15

    beacon_path = ORCHESTRATOR_STATUS / "handoff_status.md"
    if not beacon_path.exists():
        pytest.skip(f"{beacon_path} missing — orchestrator loop not active here")

    beacon_text = beacon_path.read_text()
    state_match = re.search(r'^\s*-\s*state:\s*`(\w+)`', beacon_text, re.MULTILINE)
    if not state_match:
        pytest.skip(f"Cannot parse beacon state from {beacon_path}")
    state = state_match.group(1)

    if state not in ("claude_in_progress", "codex_in_progress"):
        # No active work claimed — no velocity requirement. This is not drift.
        return

    # Liveness check: if beacon claims in-progress, the dispatched process
    # should actually exist. If the process is dead, the beacon is stale
    # AND no commits will land — that's orphan (failure #6), not wheel-spin.
    # Distinguishing orphan vs wheel-spin matters for the fix.
    expected_cmd = "claude --print" if state == "claude_in_progress" else "codex"
    pgrep = subprocess.run(
        ["pgrep", "-af", expected_cmd],
        capture_output=True, timeout=5,
    )
    process_alive = bool(pgrep.stdout.strip())

    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log",
         f"--since={MAX_GAP_MINUTES} minutes ago", "--oneline"],
        capture_output=True, timeout=10,
    )
    assert result.returncode == 0, (
        f"git log failed: rc={result.returncode}, "
        f"stderr={result.stderr.decode()[:200]}"
    )

    commits = result.stdout.decode().strip()
    if not commits:
        diagnosis = (
            "orphan (dispatch dead, beacon stale — failure #6)"
            if not process_alive
            else "wheel-spinning (process alive, no progress — failure #5)"
        )
        pytest.fail(
            f"Beacon says state={state!r} but no commits in last {MAX_GAP_MINUTES} min. "
            f"Process alive={process_alive}. Diagnosis: {diagnosis}. "
            f"Fix path differs: orphan needs beacon reset, wheel-spin needs intervention."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Detector 8: Orchestrator systemd timers fired recently
# ─────────────────────────────────────────────────────────────────────────────

def test_orchestrator_systemd_timers_fresh():
    """Known orchestrator user-systemd timers must be enabled AND have fired
    within their declared cadence. Uses `systemctl show --property=...` which
    emits machine-readable fields, avoiding the locale/format fragility of
    regex-parsing `list-timers` output (Gemma's REWRITE verdict).

    Catches the general "timer silently stopped firing" drift class — e.g.,
    the vLLM semantic health check not running means vLLM could be dead and
    we wouldn't notice until a call 502s.
    """
    # Each timer's max allowed age since last fire, in seconds.
    # Values pad ~15% over the scheduled interval for scheduler jitter.
    EXPECTED_TIMERS = {
        "orchestrator-vllm-health.timer": 35 * 60,       # scheduled: every 30 min
        "orchestrator-supervisor-cron.timer": 15 * 60,   # scheduled: every 10 min
        "orchestrator-gemma-bulk-fixer.timer": 10 * 60,  # scheduled: every 5 min
    }

    def _show(timer_name: str) -> dict:
        """Read machine-readable properties for a single timer unit."""
        cmd = [
            "systemctl", "--user", "show",
            "--property=LoadState,UnitFileState,ActiveState,LastTriggerUSec",
            timer_name,
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=5)
        if r.returncode != 0:
            return {}
        props = {}
        for line in r.stdout.decode().splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                props[k.strip()] = v.strip()
        return props

    def _to_epoch(systemd_ts: str) -> int | None:
        """Convert a systemd LastTriggerUSec value to Unix epoch seconds.

        systemd emits strings like `Wed 2026-04-16 17:25:34 UTC` (wallclock)
        or `0` / `n/a` / empty when the timer has never fired. Delegate parsing
        to `date -d` which handles all TZ names + formats systemd produces,
        avoiding locale-sensitive Python strptime breakage.
        """
        if not systemd_ts or systemd_ts in ("0", "n/a"):
            return None
        r = subprocess.run(
            ["date", "-d", systemd_ts, "+%s"],
            capture_output=True, timeout=3,
        )
        if r.returncode != 0:
            return None
        try:
            return int(r.stdout.decode().strip())
        except ValueError:
            return None

    # Probe systemd --user is available at all
    probe = subprocess.run(
        ["systemctl", "--user", "is-system-running"],
        capture_output=True, timeout=5,
    )
    if probe.returncode not in (0, 1):  # 1 = degraded, still running
        pytest.skip(
            f"systemctl --user not available (rc={probe.returncode}): "
            f"{probe.stderr.decode()[:160]}. "
            "Runs only in a user session with XDG_RUNTIME_DIR set."
        )

    now = int(time.time())
    problems: list[str] = []
    for timer_name, max_age_s in EXPECTED_TIMERS.items():
        props = _show(timer_name)
        if not props or props.get("LoadState") in ("", "not-found"):
            problems.append(f"{timer_name}: NOT LOADED (missing from systemd user scope)")
            continue
        unit_state = props.get("UnitFileState", "")
        if unit_state not in ("enabled", "enabled-runtime", "static", "generated"):
            problems.append(
                f"{timer_name}: UnitFileState={unit_state!r} "
                f"(not enabled — will never fire)"
            )
            continue
        if props.get("ActiveState") != "active":
            problems.append(
                f"{timer_name}: ActiveState={props.get('ActiveState')!r} "
                f"(timer loaded but not active)"
            )
            continue
        last_ts = props.get("LastTriggerUSec", "")
        epoch = _to_epoch(last_ts)
        if epoch is None:
            problems.append(
                f"{timer_name}: LastTriggerUSec={last_ts!r} "
                f"— never fired since boot / enable"
            )
            continue
        age_s = now - epoch
        if age_s > max_age_s:
            problems.append(
                f"{timer_name}: last fired {age_s // 60}min {age_s % 60}s ago "
                f"(max allowed {max_age_s // 60}min)"
            )

    assert not problems, (
        "Orchestrator timer(s) not firing on schedule:\n  "
        + "\n  ".join(problems)
        + "\nEither systemd isn't running the timer, the unit isn't enabled, "
        "or the service it triggers is failing silently."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detector 9: settings.json hooks resolve to real scripts
# ─────────────────────────────────────────────────────────────────────────────

def test_settings_json_declared_hooks_are_real_files():
    """Every hook declared in settings.json must point at a real, parseable file.

    Maps to failure #4 (settings.json validation error disabled ALL hooks —
    "The Silenced Alert"). The original shape-only check (valid JSON) missed
    this class; this detector verifies the SEMANTIC effect — each declared
    hook has a script behind it that actually exists and isn't a typo'd path.
    """
    settings_path = CLAUDE_HOME / "settings.json"
    assert settings_path.exists(), f"{settings_path} missing"

    try:
        settings = json.loads(settings_path.read_text())
    except json.JSONDecodeError as e:
        pytest.fail(f"settings.json is not valid JSON: {e}")

    hooks_cfg = settings.get("hooks", {})
    assert isinstance(hooks_cfg, dict), (
        f"hooks field must be a dict, got {type(hooks_cfg).__name__} "
        f"— this is exactly The Silenced Alert shape mismatch"
    )

    broken: list[str] = []
    for event, entries in hooks_cfg.items():
        if not isinstance(entries, list):
            broken.append(
                f"{event}: hooks value is not a list "
                f"({type(entries).__name__}) — whole event will be dropped by Claude Code"
            )
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                broken.append(f"{event}[{i}]: entry is not a dict")
                continue
            hooks_list = entry.get("hooks", [])
            if not isinstance(hooks_list, list):
                broken.append(f"{event}[{i}].hooks: not a list")
                continue
            for j, hook in enumerate(hooks_list):
                if not isinstance(hook, dict):
                    continue
                if hook.get("type") != "command":
                    continue
                command = hook.get("command", "")
                # Use shlex.split for proper shell tokenization — handles
                # quoted args, escaped spaces, etc., instead of naive split().
                try:
                    tokens = shlex.split(command)
                except ValueError as e:
                    broken.append(
                        f"{event}[{i}].hooks[{j}]: command is not shell-parseable "
                        f"({type(e).__name__}: {e})"
                    )
                    continue
                if not tokens:
                    continue
                # Determine invocation shape:
                #   - `python3 /path/to/hook.py` → interpreter invokes the script,
                #     x-bit not required but readability + interpreter-on-PATH are.
                #   - `/path/to/hook.sh arg` → direct-exec, x-bit IS required.
                INTERPRETERS = {"python", "python3", "bash", "sh", "zsh", "node", "ruby", "perl"}
                first = os.path.basename(tokens[0])
                interpreter_invoked = first in INTERPRETERS
                if interpreter_invoked:
                    # Script path is the first positional arg after the interpreter
                    script_tok = next(
                        (t for t in tokens[1:]
                         if t.startswith("/") or t.startswith("~/")),
                        None,
                    )
                    if script_tok is None:
                        continue  # no path to verify
                    script = Path(os.path.expanduser(script_tok))
                    if not script.exists():
                        broken.append(
                            f"{event}[{i}].hooks[{j}]: {tokens[0]} would read "
                            f"{script} which does not exist"
                        )
                    elif not os.access(script, os.R_OK):
                        broken.append(
                            f"{event}[{i}].hooks[{j}]: {script} exists but is "
                            f"not readable (interpreter cannot load it)"
                        )
                else:
                    # Direct-exec path: first token IS the script; needs x-bit.
                    if tokens[0].startswith("/") or tokens[0].startswith("~/"):
                        script = Path(os.path.expanduser(tokens[0]))
                        if not script.exists():
                            broken.append(
                                f"{event}[{i}].hooks[{j}]: command references "
                                f"{script} which does not exist"
                            )
                        elif script.is_file() and not os.access(script, os.X_OK):
                            broken.append(
                                f"{event}[{i}].hooks[{j}]: {script} exists but is "
                                f"not executable (chmod +x or hook won't fire — "
                                f"direct-exec invocation requires x-bit)"
                            )

    assert not broken, (
        "settings.json declares hooks that don't resolve to real scripts:\n  "
        + "\n  ".join(broken)
        + "\nThese hooks will never fire — Silenced Alert class."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Detector 10: Legacy service_account token rejected post-SS-4 (D17 gate)
# ─────────────────────────────────────────────────────────────────────────────

def test_legacy_service_account_token_rejected_after_ss4_cutover():
    """Legacy X-Service-Key tokens must be rejected once D17 lands.

    Maps to failure #11 (service_accounts still read by middleware post-SS-4
    data migration). Until D17 (service_accounts → access_tokens consumer
    cutover) lands, legacy tokens still authenticate — dual-state authority.

    This detector is SKIP today and must FAIL on the first run after D17 ships
    if the cutover is incomplete. Flip the skip to the real assertion when
    D17's `verify_service_key` dual-read-with-deprecation-logging is in place.
    """
    pytest.skip(
        "D17 (service_accounts → access_tokens consumer cutover) not implemented. "
        "Detector fires once D17 Slice 1 lands (feature-flagged dual-read in "
        "verify_service_key). Active sprint: "
        "`ai-queue/operations/active_sprint.md` D17."
    )
