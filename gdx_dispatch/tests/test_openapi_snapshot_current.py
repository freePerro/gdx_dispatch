"""The checked-in route-table snapshot must match the app — and the app must
have every router it tries to include.

`gdx_dispatch/openapi_routes.txt` is generated (one `METHOD path` per line).
Before this gate a 2.8 MB openapi.json sat stale for weeks (53 operations
gone, 48 missing) while the invariants registry claimed a drift check that
was never built. Two things can make the route table wrong, and both are
guarded here:

1. a route added or removed without regenerating the snapshot;
2. a router whose import fails on this machine — app.py wraps every include
   in try/except and falls back to an EMPTY APIRouter, so a missing system
   library (WeasyPrint's pango/cairo, say) silently deletes routes.

Determinism is pinned too: two separate processes must publish the same
document. On 2026-08-31 they did not — a schema default baked a random
secret per process and a set-typed method list flipped an operationId.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import json
import pathlib
import re
import subprocess
import sys

from gdx_dispatch.tools import openapi_snapshot as snap

APP_PY = pathlib.Path(__file__).resolve().parents[1] / "app.py"


def test_snapshot_matches_the_app_route_table():
    added, removed = snap.drift(snap.operations(snap.build_spec()), snap.load_snapshot())
    assert not added and not removed, (
        "gdx_dispatch/openapi_routes.txt has drifted from the app. "
        f"Run `{snap.REGEN}` and commit the result.\n"
        + "\n".join(f"  + {op}" for op in added)
        + "\n"
        + "\n".join(f"  - {op}" for op in removed)
    )


def test_snapshot_file_is_the_tools_own_rendering():
    """Sorted, headed, one op per line — a hand edit shows up as a byte diff."""
    text = snap.SNAPSHOT.read_text(encoding="utf-8")
    assert text == snap.render(snap.parse(text))
    assert len(snap.parse(text)) > 1000, "a truncated snapshot is not a snapshot"


def _modules_named_by_app_py() -> set[str]:
    """Every gdx_dispatch module app.py can import, found by walking its AST —
    `from x import a as b` (resolved to `x.a`), `import x.y`, parenthesized
    multi-line forms, imports inside try/except and functions, and the
    `__import__("gdx_dispatch...")` / `importlib.import_module("...")` string
    forms. A regex over source lines missed four of these (audit 2026-08-31),
    one of them a stub-fallbacked router — exactly the silent-drop case."""
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("gdx_dispatch"):
            found.add(node.module)
            for alias in node.names:
                if alias.name != "*":
                    found.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("gdx_dispatch"):
                    found.add(alias.name)
        elif isinstance(node, ast.Call):
            fn = node.func
            is_dunder = isinstance(fn, ast.Name) and fn.id == "__import__"
            is_importlib = isinstance(fn, ast.Attribute) and fn.attr == "import_module"
            if (is_dunder or is_importlib) and node.args and isinstance(node.args[0], ast.Constant):
                val = node.args[0].value
                if isinstance(val, str) and val.startswith("gdx_dispatch"):
                    found.add(val)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # app.py also builds dotted names at runtime: `__import__(f"gdx_dispatch.{s}")`
            # over a list of "routers.xxx" strings. Any module-shaped string
            # under a known package is treated as an import target.
            if re.fullmatch(r"gdx_dispatch\.[\w.]+", node.value):
                candidate = node.value
            elif re.fullmatch(r"(routers|modules|core|api|tasks)\.[\w.]+", node.value):
                candidate = f"gdx_dispatch.{node.value}"
            else:
                continue
            # Logger names look the same ("gdx_dispatch.app.startup_config").
            # A string counts as an import target only when its parent is an
            # importable PACKAGE — gdx_dispatch.routers is; gdx_dispatch.app
            # is a module, so its logger names are ignored.
            parent = candidate.rpartition(".")[0]
            try:
                spec = importlib.util.find_spec(parent)
            except (ImportError, ValueError):
                spec = None
            if spec is not None and spec.submodule_search_locations:
                found.add(candidate)
    return found


def test_every_router_app_tries_to_include_is_importable():
    """The import-failure fallback in app.py is silent at runtime. Import
    every module app.py names; a failure here carries the real exception
    instead of surfacing as 'routes drifted'. A `pkg.name` that is an
    attribute rather than a submodule (a function, a router object) is
    accepted when the package imports and exposes it."""
    modules = _modules_named_by_app_py()
    assert len(modules) > 300, f"expected app.py to name >300 modules/objects, found {len(modules)}"
    failures = {}
    for name in sorted(modules):
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            pkg, _, attr = name.rpartition(".")
            try:
                if not hasattr(importlib.import_module(pkg), attr):
                    failures[name] = f"{type(exc).__name__}: {exc}"
            except Exception as exc2:  # noqa: BLE001
                failures[name] = f"{type(exc2).__name__}: {exc2}"
        except Exception as exc:  # noqa: BLE001 — every failure, with its cause
            failures[name] = f"{type(exc).__name__}: {exc}"
    assert not failures, "modules app.py includes but cannot import here:\n" + "\n".join(
        f"  {k}: {v}" for k, v in failures.items()
    )


def _spec_digest_in_a_fresh_process() -> str:
    code = (
        "import hashlib, json\n"
        "from gdx_dispatch.tools.openapi_snapshot import build_spec\n"
        "print(hashlib.sha256(json.dumps(build_spec(), sort_keys=True).encode()).hexdigest())\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=300, check=True,
        cwd=str(APP_PY.resolve().parents[1]),
    )
    return out.stdout.strip().splitlines()[-1]


def test_published_document_is_identical_across_processes():
    """Two boots, one document. A per-process value in the schema (a random
    default, a set-ordered operationId) would make /openapi.json change on
    every restart — and make any snapshot of it unreproducible."""
    a, b = _spec_digest_in_a_fresh_process(), _spec_digest_in_a_fresh_process()
    assert a == b, f"openapi document differs between processes: {a[:12]} != {b[:12]}"


def test_no_random_looking_default_is_published():
    """The concrete 2026-08-31 leak: IntegrationCreate.secret defaulted to a
    64-hex token computed at import and shipped in the schema."""
    doc = json.dumps(snap.build_spec())
    assert not re.search(r'"default":\s*"[0-9a-f]{64}"', doc)


def test_drift_helper_reports_both_directions():
    added, removed = snap.drift({("GET", "/x"), ("POST", "/y")}, {("GET", "/x"), ("DELETE", "/z")})
    assert added == ["POST /y"] and removed == ["DELETE /z"]


def test_render_parse_round_trip():
    ops = {("GET", "/b"), ("POST", "/a")}
    text = snap.render(ops)
    assert text.startswith("#") and snap.parse(text) == ops
    assert hashlib.sha256(text.encode()).hexdigest() == hashlib.sha256(snap.render(snap.parse(text)).encode()).hexdigest()
