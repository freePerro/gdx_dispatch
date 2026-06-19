"""Repo-wide brake for the 2026-05-19 bug class: a route handler reading
``payload.<attr>`` where ``<attr>`` is NOT a declared field on the
request model. Pydantic v2 raises ``AttributeError`` on missing-attribute
access, so every such read is a guaranteed 500 on that endpoint
(``JobCreate`` was missing ``holding_area_id`` → 6-day prod outage).

This is an AST scan, DB-free, runs in the default suite. It generalizes
the create_job-specific scan in test_jobs_create_payload_contract.py to
EVERY handler in ``gdx_dispatch/routers/`` — including update_job and
closeout_job, which the SQLite smoke (test_jobs_endpoints_smoke.py)
cannot execute faithfully because of Postgres-only SQL.

Design priority: ZERO false positives. A false positive fails CI for
everyone, so the scanner is deliberately conservative — when it cannot
prove a model's full field set, or cannot prove ``arg`` still refers to
the model at the access site, it SKIPS rather than risk a wrong failure.
That means it can miss some cases; it must never cry wolf.

KNOWN COVERAGE LIMITS (stated, not hidden — the point of this file is
honesty):
- Only ``arg.<attr>`` is inspected. A handler that does
  ``data = payload.model_dump(); data["x"]`` is the SAME bug class in
  dict-key shape and is INVISIBLE here (e.g. ``update_job``). Closing
  that needs a separate model_dump/dict-key scan — tracked follow-up.
- Bodies annotated ``Model | None`` / ``Optional[Model]`` (≈9 live
  handlers) are skipped (non-Name annotation) to keep zero-FP. They are
  uncovered by this scan; the always-on smoke covers create_job, the
  e2e suite covers the rest.
- Models with an imported/unprovable base are skipped (can't enumerate
  inherited fields without cross-module resolution).
"""
from __future__ import annotations

import ast
from pathlib import Path

ROUTERS_DIR = Path(__file__).resolve().parents[1] / "routers"

# Pydantic v2 instance API + common base attrs that are legitimately
# accessed on a model instance and are NOT declared fields. Anything
# starting with ``model_`` or a dunder is also allowed (see _is_allowed).
_PYDANTIC_API = {
    "dict", "json", "copy", "schema", "parse_obj", "parse_raw", "parse_file",
    "from_orm", "validate", "construct", "update_forward_refs",
}


def _is_allowed(attr: str) -> bool:
    return (
        attr.startswith("model_")
        or attr.startswith("__")
        or attr in _PYDANTIC_API
    )


def _base_names(cls: ast.ClassDef) -> list[str]:
    out = []
    for b in cls.bases:
        if isinstance(b, ast.Name):
            out.append(b.id)
        elif isinstance(b, ast.Attribute):  # e.g. pydantic.BaseModel
            out.append(b.attr)
    return out


def _own_fields(cls: ast.ClassDef) -> set[str]:
    """Field names declared directly on this class body."""
    fields: set[str] = set()
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.add(stmt.target.id)
        elif isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    fields.add(t.id)
    # config / private aren't request fields but are never read as data
    return {f for f in fields if not f.startswith("_") and f != "model_config"}


def _resolve_models(tree: ast.Module) -> dict[str, set[str] | None]:
    """Map local class name -> field set, or None if its full field set
    can't be proven (an unresolved/imported base) → callers must SKIP it."""
    classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    resolved: dict[str, set[str] | None] = {}

    def resolve(name: str, seen: frozenset[str]) -> set[str] | None:
        if name in resolved:
            return resolved[name]
        if name in seen:                       # cyclic base — give up safely
            return None
        cls = classes.get(name)
        if cls is None:
            return None
        bases = _base_names(cls)
        # Must ultimately root in BaseModel; any non-local, non-BaseModel
        # base means we can't enumerate inherited fields → unprovable.
        fields = _own_fields(cls)
        for b in bases:
            if b == "BaseModel":
                continue
            if b in classes:
                parent = resolve(b, seen | {name})
                if parent is None:
                    resolved[name] = None
                    return None
                fields = fields | parent
            else:
                resolved[name] = None          # imported/unknown base
                return None
        if "BaseModel" not in bases and not any(b in classes for b in bases):
            # Not a pydantic model at all (e.g. a plain helper class).
            resolved[name] = None
            return None
        resolved[name] = fields
        return fields

    for cname in classes:
        resolve(cname, frozenset())
    return resolved


def _arg_is_reassigned(func: ast.AST, argname: str) -> bool:
    """True if argname is bound to anything inside the function body —
    then a later ``argname.attr`` may not be the model. Be conservative."""
    for n in ast.walk(func):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == argname:
                    return True
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and isinstance(
            getattr(n, "target", None), ast.Name
        ) and n.target.id == argname:
            return True
        elif isinstance(n, ast.NamedExpr) and isinstance(n.target, ast.Name) and n.target.id == argname:
            return True
        elif isinstance(n, ast.For) and isinstance(n.target, ast.Name) and n.target.id == argname:
            return True
    return False


def _scan_file(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    models = _resolve_models(tree)
    violations: list[str] = []

    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = func.args
        all_args = list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)
        for arg in all_args:
            ann = arg.annotation
            # Only bare-Name annotations that resolve to a provable local
            # pydantic model. Optional[X] / X | None / imported models are
            # intentionally skipped (no FP over coverage).
            if not isinstance(ann, ast.Name):
                continue
            fields = models.get(ann.id)
            if fields is None:                 # unknown/unprovable → skip
                continue
            argname = arg.arg
            if _arg_is_reassigned(func, argname):
                continue
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == argname
                    and not _is_allowed(node.attr)
                    and node.attr not in fields
                ):
                    violations.append(
                        f"{path.relative_to(ROUTERS_DIR.parent.parent)}:"
                        f"{node.lineno} {func.name}() reads "
                        f"`{argname}.{node.attr}` but {ann.id} has no such "
                        f"field (declared: {sorted(fields)})"
                    )
    return violations


def test_no_handler_reads_undeclared_request_model_attr() -> None:
    files = sorted(ROUTERS_DIR.rglob("*.py"))
    assert files, "no router files found — path wrong, scanner is inert"
    all_v: list[str] = []
    for f in files:
        all_v.extend(_scan_file(f))
    assert not all_v, (
        "Handler(s) read a request-model attribute that the model does "
        "not declare → guaranteed AttributeError/500 on that endpoint "
        "(the 2026-05-19 JobCreate.holding_area_id class):\n  "
        + "\n  ".join(all_v)
    )


def test_scanner_is_not_inert() -> None:
    """Guard the guard: prove the scanner actually flags the bug on a
    synthetic sample. If this fails, the real scan above is theater."""
    sample = (
        "from pydantic import BaseModel\n"
        "class Body(BaseModel):\n"
        "    title: str\n"
        "def handler(payload: Body):\n"
        "    return payload.title, payload.holding_area_id\n"
    )
    tree = ast.parse(sample)
    models = _resolve_models(tree)
    assert models.get("Body") == {"title"}
    # reproduce the access-check on the in-memory sample
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    reads = {
        n.attr for n in ast.walk(func)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
        and n.value.id == "payload"
    }
    undeclared = {r for r in reads if not _is_allowed(r)} - models["Body"]
    assert undeclared == {"holding_area_id"}, (
        "scanner failed to flag the known bug shape — it is inert"
    )
