"""Checked-in plugins import nothing from gdx_dispatch outside plugin_api.

plugin_api is the contract a plugin binds to (ADR-013, "a versioned plugin API
contract"); everything else in gdx_dispatch is core internals that may move
without notice. The eventlog handler imported ``core.database.SessionLocal``
because a request-free handler had no sanctioned way to open a session
(GDXA-699); ``plugin_api.context.plugin_session`` is that way now, and this
guard keeps the next plugin from reaching past the contract again.

What counts as reaching past it:

- an import of any gdx_dispatch module outside plugin_api, or a dotted path
  ``gdx_dispatch.<not plugin_api>`` reached from an imported ``gdx_dispatch``;
- a private (``_``) name taken from plugin_api by import or by static
  attribute;
- a name taken from plugin_api, by import or by a static attribute chain of
  any depth, that is really a core class, function or module re-exported by
  accident (``plugin_api.catalog`` imports core models) - resolved at runtime
  against the object's defining module;
- a string literal naming a gdx_dispatch module outside plugin_api
  (``importlib``, or an entry-point style ``module:attr``).

Not caught: ``getattr`` with any name, a function's ``__globals__``, an import built from pieces at
runtime, and a core *instance* re-exported under a public name, since an
instance carries no defining module. One such name stays public on purpose:
``plugin_api.context.SessionLocal``, the core session factory, which a
git-ignored private plugin binds to. The plugins this guard reads are refused
it by name (``_LEGACY``); they use ``plugin_session()`` or ``get_plugin_db``. A plugin's ``tests/`` directory is not scanned:
tests do not ship in the wheel, and plugin tests drive core fixtures on
purpose.

This keeps the names a plugin binds to stable; it is not a capability
boundary. ``plugin_session()`` still hands out a full Session on the shared
database.

Enumeration reads the git index (``tools/tracked_files``), so a git-ignored
private plugin present on one machine neither reddens nor escapes the guard.
Plugins in the separate public plugins repo are outside this repo's reach.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import re

from gdx_dispatch.tools.tracked_files import REPO_ROOT, read_tracked_files

_ALLOWED = "gdx_dispatch.plugin_api"
# Public in plugin_api only for a plugin outside this repo; refused here.
_LEGACY = {"SessionLocal"}
_DOTTED = re.compile(r"(gdx_dispatch(?:\.[A-Za-z_][A-Za-z0-9_]*)+)(?::[A-Za-z_][A-Za-z0-9_.]*)?")


def _is_allowed(module: str) -> bool:
    return module == _ALLOWED or module.startswith(_ALLOWED + ".")


def _via_api(root: ast.expr, first: str | None, api_names: set[str]) -> bool:
    """An attribute chain reaches into plugin_api. A bare ``gdx_dispatch`` root
    (bound by ``import gdx_dispatch.plugin_api.x``) counts only when the chain
    goes on through ``plugin_api``; any other path is reported as core."""
    if not isinstance(root, ast.Name):
        return False
    if root.id == "gdx_dispatch":
        return first == "plugin_api"
    return root.id in api_names


def contract_violations(source: str, filename: str = "<src>") -> list[str]:
    """Every way ``source`` reaches into gdx_dispatch past plugin_api."""
    out: list[str] = []
    tree = ast.parse(source, filename=filename)
    # Names bound to plugin_api (modules or objects), for the private-attribute check.
    api_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_allowed(alias.name):
                    api_names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and (
            _is_allowed(node.module) or node.module == "gdx_dispatch"
        ):
            api_names.update(a.asname or a.name for a in node.names)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _LEGACY:
            root, first = node.value, None
            while isinstance(root, ast.Attribute):
                root, first = root.value, root.attr
            if _via_api(root, first, api_names):
                out.append(f"{filename}:{node.lineno} .{node.attr} (legacy, via {root.id})")
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "gdx_dispatch" and node.attr != "plugin_api"):
            out.append(f"{filename}:{node.lineno} gdx_dispatch.{node.attr}")
        if (isinstance(node, ast.Attribute) and node.attr.startswith("_")
                and not node.attr.startswith("__")):
            root, first = node.value, None
            while isinstance(root, ast.Attribute):
                root, first = root.value, root.attr
            if _via_api(root, first, api_names):
                out.append(f"{filename}:{node.lineno} .{node.attr} (private, via {root.id})")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "gdx_dispatch" and not _is_allowed(alias.name):
                    out.append(f"{filename}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            mod = node.module
            if mod.split(".")[0] != "gdx_dispatch":
                continue
            if mod == "gdx_dispatch":
                # `from gdx_dispatch import plugin_api` is the only allowed form.
                for alias in node.names:
                    if alias.name != "plugin_api":
                        out.append(f"{filename}:{node.lineno} from gdx_dispatch import {alias.name}")
            elif not _is_allowed(mod):
                out.append(f"{filename}:{node.lineno} from {mod} import ...")
            else:
                for alias in node.names:
                    if alias.name == "*":
                        out.append(f"{filename}:{node.lineno} from {mod} import * (star)")
                    if alias.name in _LEGACY:
                        out.append(f"{filename}:{node.lineno} from {mod} import {alias.name} (legacy)")
                    if alias.name.startswith("_"):
                        out.append(f"{filename}:{node.lineno} from {mod} import {alias.name} (private)")
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and _DOTTED.fullmatch(node.value)
              and not _is_allowed(_DOTTED.fullmatch(node.value).group(1))):
            out.append(f"{filename}:{node.lineno} string {node.value!r}")
    return out


def _owner(obj) -> str | None:
    if inspect.ismodule(obj):
        return obj.__name__
    if inspect.isclass(obj) or inspect.isroutine(obj):
        return obj.__module__ or ""
    return None


def _is_core(owner: str | None) -> bool:
    return bool(owner) and owner.split(".")[0] == "gdx_dispatch" and not _is_allowed(owner)


def reexported_core_names(source: str, filename: str = "<src>") -> list[str]:
    """Names taken from plugin_api, by import or by a static attribute chain,
    whose object is defined in core: a re-export by accident, not the contract.
    Imports the plugin_api modules named, so this runs against the real tree."""
    out: list[str] = []
    tree = ast.parse(source, filename=filename)
    bound: dict[str, object] = {}  # local name -> the object it is bound to
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module and (
            _is_allowed(node.module) or node.module == "gdx_dispatch"
        ):
            mod = importlib.import_module(node.module)
            for alias in node.names:
                if node.module == "gdx_dispatch" and alias.name != "plugin_api":
                    continue
                obj = getattr(mod, alias.name, None)
                if obj is None:  # a submodule not yet imported in this process
                    try:
                        obj = importlib.import_module(f"{node.module}.{alias.name}")
                    except ImportError:
                        obj = None
                bound[alias.asname or alias.name] = obj
                if _is_core(_owner(obj)):
                    out.append(f"{filename}:{node.lineno} {node.module}.{alias.name} is {_owner(obj)}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_allowed(alias.name):
                    continue
                leaf = importlib.import_module(alias.name)
                if alias.asname:
                    bound[alias.asname] = leaf
                else:
                    bound["gdx_dispatch"] = importlib.import_module("gdx_dispatch")
    inner = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or id(node) in inner:
            continue
        chain: list[str] = []
        root = node
        while isinstance(root, ast.Attribute):
            chain.append(root.attr)
            root = root.value
        if not (isinstance(root, ast.Name) and root.id in bound):
            continue
        chain.reverse()
        if root.id == "gdx_dispatch" and chain[0] != "plugin_api":
            continue  # contract_violations already reports gdx_dispatch.<core>
        obj = bound[root.id]
        for i, attr in enumerate(chain):
            obj = getattr(obj, attr, None)
            if _is_core(_owner(obj)):
                path = ".".join([root.id, *chain[: i + 1]])
                out.append(f"{filename}:{node.lineno} {path} is {_owner(obj)}")
                break
            if obj is None:
                break
    return out


def _plugin_sources() -> list[str]:
    return sorted(
        p for p in read_tracked_files()
        if p.startswith("plugins/") and p.endswith(".py")
        and "/build/" not in p and "/tests/" not in p
    )


def test_plugin_sources_are_enumerated():
    """Non-vacuity: the guard below must have files to read."""
    assert any(p.startswith("plugins/gdx-plugin-eventlog/") for p in _plugin_sources())


def test_checked_in_plugins_import_only_plugin_api():
    bad: list[str] = []
    for rel in _plugin_sources():
        src = (REPO_ROOT / rel).read_text(encoding="utf-8")
        bad += contract_violations(src, rel) + reexported_core_names(src, rel)
    assert not bad, (
        "Plugins may import from gdx_dispatch only through gdx_dispatch.plugin_api "
        "(a DB session: plugin_api.context.get_plugin_db / plugin_session):\n  "
        + "\n  ".join(bad)
    )


def test_detector_flags_core_imports_and_passes_plugin_api():
    bad = """
from gdx_dispatch.core.database import SessionLocal
import gdx_dispatch.models.job
from gdx_dispatch import services
from gdx_dispatch.plugin_api.context import _SessionLocal
importlib.import_module("gdx_dispatch.core.database")
from gdx_dispatch.plugin_api import context
context._SessionLocal()
import gdx_dispatch.plugin_api.context as c
c._SessionLocal()
import gdx_dispatch.plugin_api.events
gdx_dispatch.core.database.SessionLocal()
x = "gdx_dispatch.core.database:SessionLocal"
from gdx_dispatch.plugin_api.context import SessionLocal
context.SessionLocal()
gdx_dispatch.plugin_api.context.SessionLocal()
from gdx_dispatch.plugin_api.context import *
"""
    good = """
from gdx_dispatch.plugin_api.context import plugin_session
from gdx_dispatch.plugin_api import PluginManifest
import gdx_dispatch.plugin_api.events
from gdx_dispatch import plugin_api
from gdx_dispatch_plugins import thing
from .models import EventLogEntry
importlib.import_module("gdx_dispatch.plugin_api.events")
PluginManifest.__name__
gdx_dispatch.plugin_api.events.PluginEvent
"""
    assert len(contract_violations(bad)) == 13
    assert contract_violations(good) == []


def test_reexport_check_flags_core_objects_behind_plugin_api():
    bad = (
        "from gdx_dispatch.plugin_api.catalog import CustomCatalog\n"
        "from gdx_dispatch.plugin_api import catalog\n"
        "catalog.CustomCatalogItem\n"
        "import gdx_dispatch.plugin_api.catalog as cat\n"
        "cat.CustomCatalog\n"
        "from gdx_dispatch import plugin_api\n"
        "plugin_api.catalog.CustomCatalog\n"
        "import gdx_dispatch.plugin_api.catalog\n"
        "gdx_dispatch.plugin_api.catalog.CustomCatalogItem\n"
    )
    good = (
        "from gdx_dispatch.plugin_api.catalog import upsert_catalog_items\n"
        "from gdx_dispatch.plugin_api.context import plugin_session, PluginContext\n"
        "from gdx_dispatch.plugin_api import PluginManifest, context\n"
        "context.plugin_session\n"
        "plugin_api.context.PluginContext.tenant_id\n"
        "import gdx_dispatch.plugin_api.events\n"
        "gdx_dispatch.plugin_api.events.PluginEvent\n"
    )
    assert len(reexported_core_names(bad)) == 5
    assert reexported_core_names(good) == []


def test_reexport_check_imports_a_submodule_not_yet_loaded(monkeypatch):
    """``from plugin_api import <submodule>`` is resolved by importing it, so
    the verdict does not depend on what an earlier test happened to import.
    monkeypatch restores the real module for every later test."""
    import sys

    import gdx_dispatch.plugin_api as pkg

    monkeypatch.delitem(sys.modules, "gdx_dispatch.plugin_api.catalog", raising=False)
    monkeypatch.delattr(pkg, "catalog", raising=False)
    src = "from gdx_dispatch.plugin_api import catalog\ncatalog.CustomCatalog\n"
    assert len(reexported_core_names(src)) == 1


def test_plugin_session_closes_and_get_plugin_db_shares_it(monkeypatch):
    """The sanctioned request-free session is closed on exit, and the route
    dependency goes through the same factory."""
    from gdx_dispatch.plugin_api import context

    made: list[object] = []

    class _S:
        closed = False

        def close(self):
            self.closed = True

    def _factory():
        s = _S()
        made.append(s)
        return s

    monkeypatch.setattr(context, "SessionLocal", _factory)
    with context.plugin_session() as db:
        assert not db.closed
    assert db.closed

    gen = context.get_plugin_db()
    db2 = next(gen)
    gen.close()
    assert db2.closed and len(made) == 2

    try:
        with context.plugin_session() as db3:
            raise ValueError("boom")
    except ValueError:
        pass
    assert db3.closed
