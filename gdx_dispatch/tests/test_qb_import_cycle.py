"""GDXA-1922 — importing any quickbooks module first must not null the webhook tasks.

`core/celery_app.py` imports every celery task module at its bottom, and it is
reached from deep inside model loading (`models` → `core.integrations` →
`core.webhooks` → `core.webhooks.tasks` → `celery_app`). `quickbooks/tasks.py`
used to take `QBRateLimitError` and the pull/push functions by name from
`quickbooks/sync.py`. With any quickbooks module imported first, `sync` was
still half-built when the chain reached `tasks`, the name import failed, and
`core/webhooks/__init__.py` swallowed the ImportError by binding
`emit_webhook`, `deliver_webhook_task` and `retry_failed_webhooks_task` to
None. Now the errors live in the leaf `quickbooks/errors.py`, and `tasks`
binds `sync` as a module and looks its functions up at call time.

Two checks:

1. Behaviour: in a fresh interpreter per quickbooks module (discovered from
   the package directory, so a new module is covered), import it, then
   `gdx_dispatch.core.webhooks`, and require real task objects. In this
   session the modules are already complete, so only a fresh interpreter can
   reproduce the cycle.
2. The shape: `quickbooks/tasks.py` takes no name from `quickbooks.sync` at
   load time.

Not covered: modules outside quickbooks. On 2026-10-19 a one-off sweep that
imported each of the 642 non-test gdx_dispatch modules first found three
still red, all `modules.phone_com.*` (phone_com/tasks.py → phone_com.sync).
That cycle belongs to its owner. Probing every module costs about four
seconds each, too slow for the default gate.
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
QB_DIR = PKG / "modules" / "quickbooks"


def _quickbooks_modules() -> list[str]:
    mods = ["gdx_dispatch.modules.quickbooks"]
    mods += sorted(
        f"gdx_dispatch.modules.quickbooks.{p.stem}"
        for p in QB_DIR.glob("*.py")
        if p.stem != "__init__"
    )
    return mods


ENTRY_MODULES = _quickbooks_modules()

PROBE = (
    "import importlib, sys\n"
    "importlib.import_module(sys.argv[1])\n"
    "import gdx_dispatch.core.webhooks as w\n"
    "for name in ('emit_webhook', 'deliver_webhook_task', 'retry_failed_webhooks_task'):\n"
    "    print(name, getattr(w, name) is not None)\n"
)


def test_the_discovery_finds_the_quickbooks_modules():
    # A glob that matches nothing would make the probe below vacuous. 13 on
    # 2026-10-19; the floor is only "it still sees the cycle's members".
    for needed in ("sync", "tasks", "errors", "router", "oauth"):
        assert f"gdx_dispatch.modules.quickbooks.{needed}" in ENTRY_MODULES, ENTRY_MODULES


@pytest.mark.parametrize("module", ENTRY_MODULES)
def test_webhook_tasks_survive_importing_quickbooks_module_first(module):
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, module],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ},
    )
    assert proc.returncode == 0, f"`import {module}` then webhooks failed:\n{proc.stderr[-3000:]}"
    assert len(proc.stdout.splitlines()) == 3, proc.stdout
    nulled = [line.split()[0] for line in proc.stdout.splitlines() if line.endswith(" False")]
    assert not nulled, (
        f"importing {module} first left {nulled} as None on gdx_dispatch.core.webhooks: "
        "an import cycle through core/celery_app's task imports failed and "
        "core/webhooks/__init__.py swallowed the ImportError"
    )


def test_quickbooks_tasks_takes_no_name_from_sync_at_load():
    path = QB_DIR / "tasks.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    hits = [
        f"tasks.py:{node.lineno} from {node.module} import {', '.join(a.name for a in node.names)}"
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "gdx_dispatch.modules.quickbooks.sync"
    ]
    assert not hits, (
        f"{hits}: celery_app imports quickbooks/tasks.py while sync can be half-built, "
        "so a name import from sync there fails and nulls the webhook tasks (GDXA-1922). "
        "Import errors from quickbooks.errors and call sync's functions through the module."
    )
