"""GDXA-1888 — importing the timesheet modules first must not null the webhook tasks.

`core/celery_app.py` imports every celery task module at its bottom, and it is
itself reached from deep inside model loading (`models` → `core.integrations`
→ `core.webhooks` → `core.webhooks.tasks` → `celery_app`). The timeclock sweep
used to take `AUTO_CLOSE_NOTE` / `MAX_SHIFT_HOURS` from `routers/timeclock`,
whose own imports run back into `core/timesheet_hours`. Imported first,
`timesheet_hours` was still half-built when the chain reached it, the import
failed, and `core/webhooks/__init__.py` swallowed the ImportError by binding
`emit_webhook`, `deliver_webhook_task` and `retry_failed_webhooks_task` to
None. Production loads `main` first and never saw it; scripts and tests did.

Two checks:

1. Behaviour: in a fresh interpreter per entry module, import that module,
   then `gdx_dispatch.core.webhooks`, and require real task objects. A fresh
   interpreter matters: in this session the modules are already complete, so
   the cycle cannot reproduce here.
2. The router shape: no task module that `celery_app` imports at load time
   may import from `gdx_dispatch.routers` at module level. A router pulls in
   half the app, so a task importing one at load time is how this cycle got
   built. A function-scope import (job_timer_sweep's) runs after loading and
   is fine.

Not covered, on purpose: the wider class, where a celery task module imports
from ANY half-built module. On 2026-10-19 importing `modules.quickbooks.*` or
`modules.phone_com.*` first still nulled the webhook tasks, through
`quickbooks/tasks.py` → `quickbooks.sync` and `phone_com/tasks.py` →
`phone_com.sync`. That was red on main before this change and is outside the
timeclock's files; it is deferred to its owners, not guarded here.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
REPO_ROOT = PKG.parent
CELERY_APP = PKG / "core" / "celery_app.py"

# The chain GDXA-1888 ran through, each as a first import, plus main as the
# production order that was always fine.
ENTRY_MODULES = [
    "gdx_dispatch.core.timeclock_policy",
    "gdx_dispatch.core.timesheet_hours",
    "gdx_dispatch.core.timesheet_export",
    "gdx_dispatch.core.timesheet_delivery",
    "gdx_dispatch.core.time_off",
    "gdx_dispatch.routers.timeclock",
    "gdx_dispatch.tasks.timeclock_sweep",
    "gdx_dispatch.main",
]

PROBE = (
    "import importlib, sys\n"
    "importlib.import_module(sys.argv[1])\n"
    "import gdx_dispatch.core.webhooks as w\n"
    "for name in ('emit_webhook', 'deliver_webhook_task', 'retry_failed_webhooks_task'):\n"
    "    print(name, getattr(w, name) is not None)\n"
)


@pytest.mark.parametrize("module", ENTRY_MODULES)
def test_webhook_tasks_survive_importing_module_first(module):
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, module],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ},
    )
    assert proc.returncode == 0, f"`import {module}` then webhooks failed:\n{proc.stderr[-3000:]}"
    nulled = [line.split()[0] for line in proc.stdout.splitlines() if line.endswith(" False")]
    assert len(proc.stdout.splitlines()) == 3, proc.stdout
    assert not nulled, (
        f"importing {module} first left {nulled} as None on gdx_dispatch.core.webhooks: "
        "an import cycle through core/celery_app's task imports failed and "
        "core/webhooks/__init__.py swallowed the ImportError"
    )


def _celery_registered_task_modules() -> list[str]:
    """Module paths `celery_app` imports at module level from gdx_dispatch, read
    from the source so a new task module is covered without a list here."""
    tree = ast.parse(CELERY_APP.read_text(), filename=str(CELERY_APP))
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("gdx_dispatch."):
            for alias in node.names:
                if alias.asname and alias.asname.endswith("_tasks"):
                    found.append(f"{node.module}.{alias.name}")
    return found


TASK_MODULES = _celery_registered_task_modules()


def test_the_scan_finds_celery_task_modules():
    # Guard against a parser that matches nothing and makes the next test
    # vacuous. 15 task modules on 2026-10-19; the floor is only "it still sees them".
    assert "gdx_dispatch.tasks.timeclock_sweep" in TASK_MODULES, TASK_MODULES


def _module_file(dotted: str) -> Path:
    base = REPO_ROOT.joinpath(*dotted.split("."))
    return base / "__init__.py" if base.is_dir() else base.with_suffix(".py")


def _module_level_router_imports(path: Path) -> list[str]:
    """`gdx_dispatch.routers...` imports executed when the module loads: the
    module body and any top-level if/try, but not function or class bodies."""
    tree = ast.parse(path.read_text(), filename=str(path))
    hits: list[str] = []
    stack: list[ast.stmt] = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("gdx_dispatch.routers"):
            hits.append(f"{path.name}:{node.lineno} from {node.module}")
        elif isinstance(node, ast.Import):
            hits += [f"{path.name}:{node.lineno} import {a.name}" for a in node.names if a.name.startswith("gdx_dispatch.routers")]
        for field in ("body", "orelse", "finalbody", "handlers"):
            stack.extend(getattr(node, field, []) or [])
    return hits


@pytest.mark.parametrize("module", TASK_MODULES)
def test_celery_task_module_does_not_import_a_router_at_load(module):
    path = _module_file(module)
    assert path.exists(), f"{module} resolved to {path}, which does not exist"
    hits = _module_level_router_imports(path)
    assert not hits, (
        f"{module} imports a router at load time ({hits}). celery_app imports this "
        "module while models are still loading, so a router import here can close "
        "a cycle back into a half-built core module (GDXA-1888). Move the shared "
        "names to a leaf module, as core/timeclock_policy.py does."
    )
