"""GDXA-433 — no ImportError swallowed in the models hub may drop tables silently.

`gdx_dispatch/models/__init__.py` registers each module's tables on
`TenantBase.metadata` inside `with suppress(ImportError):`. If one of those
imports breaks (a typo, a renamed class, a missing dependency), the block
quietly does nothing: `create_all`, pave and every ORM-built test schema simply
lack that module's tables, and the first sign is a runtime "no such table".
#48 was exactly that, and its guard (`test_model_registration_48.py`) pins only
the four tables of the one line it fixed.

This test owns the class instead of the instance: it parses the hub, finds
every block that swallows ImportError — counted from the source, never
hard-coded — and checks each one two ways, so a broken import fails here naming
its line. No table is hand-listed.

1. In a fresh interpreter, every name a block binds must be on
   `gdx_dispatch.models` once the hub has loaded. This is the check that sees
   an import failing only *during* the hub's own load — a suppressed module
   importing back from the half-built `gdx_dispatch.models` — which a re-run
   after the hub is complete would never reproduce.
2. Each body is re-executed without the suppression, to report the actual
   ImportError for a plain typo or missing module.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

HUB = Path(__file__).resolve().parents[1] / "models" / "__init__.py"
REPO_ROOT = HUB.parents[2]


def _names_import_error(node: ast.expr | None) -> bool:
    """True when an exception spec (a name, or a tuple of them) includes ImportError
    or its subclass ModuleNotFoundError — either one swallows a failed import."""
    if node is None:  # bare `except:` swallows everything, ImportError included
        return True
    if isinstance(node, ast.Tuple):
        return any(_names_import_error(elt) for elt in node.elts)
    name = node.id if isinstance(node, ast.Name) else getattr(node, "attr", None)
    return name in {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def _is_suppress_call(expr: ast.expr) -> bool:
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
    return name == "suppress" and any(_names_import_error(a) for a in expr.args)


def _swallowing_blocks() -> list[tuple[int, list[ast.stmt]]]:
    """(line, body) for every block in the hub that swallows an ImportError:
    `with suppress(ImportError, ...)` and `try: ... except ImportError: ...`."""
    tree = ast.parse(HUB.read_text(), filename=str(HUB))
    blocks: list[tuple[int, list[ast.stmt]]] = []
    for node in ast.walk(tree):
        swallows = (isinstance(node, ast.With) and any(_is_suppress_call(i.context_expr) for i in node.items)) or (
            isinstance(node, ast.Try) and any(_names_import_error(h.type) for h in node.handlers)
        )
        if swallows:
            blocks.append((node.lineno, node.body))
    return sorted(blocks, key=lambda b: b[0])


BLOCKS = _swallowing_blocks()


def test_the_scan_finds_the_hubs_blocks():
    # A parser that matched nothing would make the parametrized test below
    # vacuous. The hub held 37 such blocks on 2026-10-10; the floor here is
    # only "the scan still sees them", not a count to keep in step.
    assert BLOCKS, f"no suppress(ImportError)/except ImportError block found in {HUB}"


def _bound_names(body: list[ast.stmt]) -> set[str]:
    """The names a block's imports bind in the hub's namespace."""
    names: set[str] = set()
    for node in ast.walk(ast.Module(body=body, type_ignores=[])):
        if isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
    return names


@pytest.fixture(scope="module")
def fresh_hub_names() -> set[str]:
    # A new interpreter, so nothing this test session already imported can
    # complete a module the hub failed to load on its own.
    probe = "import json, gdx_dispatch.models as m; print(json.dumps(sorted(vars(m))))"
    proc = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"fresh `import gdx_dispatch.models` failed:\n{proc.stderr}"
    return set(json.loads(proc.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize(("line", "body"), BLOCKS, ids=[f"line{line}" for line, _ in BLOCKS])
def test_every_swallowed_import_lands_when_the_hub_loads_fresh(line, body, fresh_hub_names):
    missing = sorted(_bound_names(body) - fresh_hub_names)
    assert not missing, (
        f"{HUB.name}:{line}: a fresh `import gdx_dispatch.models` swallowed an ImportError "
        f"in this block — {missing} never reached the hub, so their tables are missing from "
        "create_all and the test schema. If the next test passes for this line, the failure "
        "happens only while the hub is loading (e.g. a circular import back into "
        "gdx_dispatch.models)."
    )


@pytest.mark.parametrize(("line", "body"), BLOCKS, ids=[f"line{line}" for line, _ in BLOCKS])
def test_every_swallowed_import_in_the_hub_actually_imports(line, body):
    import gdx_dispatch.models  # noqa: F401 — the hub itself must import first
    from gdx_dispatch.core.audit import TenantBase

    module = ast.Module(body=body, type_ignores=[])
    code = compile(module, filename=str(HUB), mode="exec")  # keeps the hub's line numbers
    namespace: dict = {}
    try:
        exec(code, namespace)  # noqa: S102 — our own source file, not input
    except ImportError as exc:
        pytest.fail(
            f"{HUB.name}:{line}: the block swallowing ImportError raises "
            f"{type(exc).__name__}: {exc} — its tables are silently missing from "
            "TenantBase.metadata, so create_all and the test schema lack them"
        )

    # The import working is half the contract; the other half is that what it
    # brings in lands on the metadata create_all reads.
    for name, obj in namespace.items():
        table = getattr(obj, "__table__", None)
        if table is not None and not name.startswith("__"):
            assert table.metadata is TenantBase.metadata, (
                f"{HUB.name}:{line}: {name} maps {table.name!r} on a metadata other "
                "than TenantBase.metadata — the hub import registers nothing"
            )
