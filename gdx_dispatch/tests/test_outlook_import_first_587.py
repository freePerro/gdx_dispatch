"""GDXA-587 — every Outlook module can be the first thing a fresh interpreter imports.

Importing ``gdx_dispatch.models`` runs ``core.webhooks`` → ``core.celery_app``,
which imports ``modules.outlook.tasks`` at the bottom of the file to register
its tasks. ``tasks`` used to ``from <sibling> import name`` from
``vendor_bill_ingest`` and ``tagger``; with either one imported first, it was
still half-loaded, the import failed, and ``core/webhooks/__init__.py``
swallowed the ImportError and left ``emit_webhook`` as ``None`` for the life of
the process. The exit code stayed 0, so only the state shows it.

The same shape reached past the package: ``core.next_action`` and the vendor
invoice, order and statement services each failed first too, because
``outlook.tasks`` imported ``NextAction`` and ``vendor_bill_ingest`` (which
imports those services by name) at the top.

In-process tests share ``sys.modules`` and load order, so they cannot see this:
each module is imported first in its own interpreter. The list is every module
in the package plus every app module an Outlook module imports at top level,
read from the source, never hand-listed. A failure counts here only when its
traceback runs through Outlook code; the same shape elsewhere (QuickBooks,
Phone.com, timesheets) belongs to those modules' owners.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parents[1] / "modules" / "outlook"
REPO_ROOT = PKG_DIR.parents[2]

_PROBE = (
    "import importlib, sys\n"
    "importlib.import_module(sys.argv[1])\n"
    "import gdx_dispatch.core.webhooks as w\n"
    "import gdx_dispatch.core.celery_app\n"
    "assert w.emit_webhook is not None, 'core.webhooks lost its Celery tasks'\n"
    "assert 'gdx_dispatch.modules.outlook.tasks' in sys.modules\n"
)


def _modules() -> list[str]:
    files = sorted(PKG_DIR.glob("*.py"))
    mods = {"gdx_dispatch.modules.outlook"} | {
        f"gdx_dispatch.modules.outlook.{p.stem}" for p in files if p.stem != "__init__"
    }
    for path in files:
        for node in ast.parse(path.read_text()).body:  # top level only
            if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").startswith("gdx_dispatch"):
                mods.add(node.module)
            elif isinstance(node, ast.Import):
                mods.update(a.name for a in node.names if a.name.startswith("gdx_dispatch"))
    return sorted(mods)


def _import_first(module: str) -> str | None:
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, module],
        capture_output=True, text=True, timeout=180, cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    # The swallowed failure is only logged, so read stderr as well as the code.
    failed = out.returncode != 0 or "partially initialized" in out.stderr or "caught exception" in out.stderr
    if failed and "/modules/outlook/" in out.stderr:
        return f"{module}: rc={out.returncode}\n{out.stderr[-1500:]}"
    return None


def test_package_has_the_modules_this_guards():
    mods = _modules()
    assert "gdx_dispatch.modules.outlook.vendor_bill_ingest" in mods
    assert "gdx_dispatch.modules.outlook.tagger" in mods
    assert "gdx_dispatch.modules.outlook.tasks" in mods


def test_every_outlook_module_imports_first_in_a_fresh_interpreter():
    with ThreadPoolExecutor(max_workers=6) as pool:
        failures = [f for f in pool.map(_import_first, _modules()) if f]
    assert not failures, "\n\n".join(failures)
