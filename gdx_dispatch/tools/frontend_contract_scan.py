#!/usr/bin/env python3
"""Frontend ↔ backend contract scanner.

Every other scanner in this directory checks backend against backend, or
backend against the database. Nothing checked the browser↔API seam — which
is where a Settings card sat for months reporting "Not Connected" while
Stripe was charging real cards, because the Vue read ``result.stripe`` and
the handler never returned a ``stripe`` key.

That bug is invisible to unit tests on either side. The backend returns what
it returns; the frontend renders what it gets. Only the PAIR is wrong. This
scanner checks pairs.

CHECKS
------

  **C1 dead-call** — the Vue calls a path no backend route serves. The button
    404s. (Found ``/api/stripe-connect/onboard``; the real router is at
    ``/api/stripe/connect``.)

  **C2 method-mismatch** — the path exists but not for that verb. Silent 405,
    and invisible by eye because the URL looks right. (``window.open`` GETs a
    POST-only endpoint.)

  **C3 phantom-field** — the Vue reads a response field the handler cannot
    return. The UI shows a permanent default and looks merely "empty" rather
    than broken. Best-effort: only reported for handlers that return a
    literal dict, so the key set is knowable.

  **C5 stub-endpoint** — the handler's entire body is a return of empty
    literals (``{"members": [], "tiers": []}``). The page renders, the request
    succeeds, and the user sees an empty screen forever with nothing logged.
    Found the Loyalty, Maps and SSO pages, and route optimisation, all backed
    by permanent blanks.

  **C6 fake-success** — a MUTATING route (POST/PUT/PATCH/DELETE) whose entire
    body is a single ``return``. It cannot have changed anything, yet it
    answers 200 with a success sentinel, so the UI pops "Saved" and reloads
    the unchanged value. Strictly worse than a 405: the user believes the
    write landed. Found `PATCH /api/pricing/{entry_id}` answering
    ``{"ok": true}`` to a Vue that toasts "Entry updated".

  **C4 orphan-route** — a backend route no frontend code calls. Not always a
    bug (mobile, webhooks, public API, integrations), so this is OFF by
    default: ``--check C4``. Use it to hunt dead surface, and read each hit.

USAGE
-----
    python3 gdx_dispatch/tools/frontend_contract_scan.py
    python3 gdx_dispatch/tools/frontend_contract_scan.py --check C1,C2
    python3 gdx_dispatch/tools/frontend_contract_scan.py --json /tmp/fe.json

    # Ground truth instead of static parsing (needs the app to import):
    docker run --rm --entrypoint python -e JWT_SECRET=<32+ bytes> \
      -v $PWD:/app -w /app docker-app \
      gdx_dispatch/tools/frontend_contract_scan.py --dump-routes /app/routes.json
    python3 gdx_dispatch/tools/frontend_contract_scan.py --routes routes.json

Static parsing resolves ``APIRouter(prefix=)`` and full ``/api/...`` decorator
paths, plus the handful of ``include_router(prefix=)`` cases. ``--routes``
skips all that guesswork by asking the live app.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

ALL_CHECKS = ("C1", "C2", "C3", "C4", "C5", "C6")
DEFAULT_CHECKS = ("C1", "C2", "C3", "C5", "C6")

HTTP_METHODS = ("get", "post", "put", "patch", "delete")

# `api.del`/`api.delete` both map to DELETE; postQueued/patchQueued are the
# offline-queue variants of POST/PATCH.
FE_METHOD_MAP = {
    "get": "GET",
    "post": "POST",
    "put": "PUT",
    "patch": "PATCH",
    "del": "DELETE",
    "delete": "DELETE",
    "postQueued": "POST",
    "patchQueued": "PATCH",
}

_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".claude", "uploads", "backups",
}

# Paths the Vue legitimately references that are not FastAPI routes.
_NON_ROUTE_PREFIXES = ("/api/placeholder", "/api/...")


def _tracked(root: Path) -> list[str]:
    try:
        res = subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True
        )
        out = [ln for ln in res.stdout.split("\n") if ln.strip()]
        if out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    return [
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and not any(part in _SKIP_DIRS for part in p.relative_to(root).parts)
    ]


def _is_excluded(rel: str) -> bool:
    """Frontend files whose API strings are not real calls.

    ``_ViewTemplate.vue`` is scaffolding (`/api/my-feature`), and the test
    fixtures assert against invented URLs on purpose — reporting either as a
    dead route is noise that buries the real hits.
    """
    return (
        "/frontend/" not in rel
        or "/node_modules/" in rel
        or "/dist/" in rel
        or "__tests__" in rel
        or rel.endswith((".spec.js", ".test.js"))
        or "/frontend/tests/" in rel
        or "/frontend/e2e/" in rel
        or rel.rsplit("/", 1)[-1].startswith("_")
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def normalize(path: str) -> str:
    """Canonical path shape: every dynamic segment becomes ``{}``.

    ``/api/jobs/{job_id}/financials`` and `/api/jobs/${id}/financials``
    must compare equal, or every parameterised route reads as dead.

    A ``${...}`` that does NOT directly follow a ``/`` is a suffix, not a
    path segment — the Vue builds `/api/payments${qs}` where ``qs`` is
    ``"?source=x"`` or ``""``. Treating it as a segment made every
    query-string call look like a dead route.
    """
    p = path.split("?")[0].split("#")[0]
    p = re.sub(r"(?<!/)\$\{[^}]*\}.*$", "", p)       # trailing query/suffix var
    p = re.sub(r"\$\{[^}]*\}", "{}", p)              # JS template segment
    p = re.sub(r"\{[^}]*\}", "{}", p)                # FastAPI path param
    p = re.sub(r":[A-Za-z_][A-Za-z0-9_]*", "{}", p)  # :param style
    p = re.sub(r"/+", "/", p)
    return p.rstrip("/") or "/"


def path_matches(fe: str, be: str) -> bool:
    """Segment-wise match where ``{}`` on EITHER side is a wildcard.

    The Vue legitimately computes whole segments — ``/api/jobs/${id}/${target}``
    with ``target`` of ``"reactivate"``/``"uncomplete"``. That cannot be
    resolved statically, so a frontend ``{}`` must be allowed to match a
    backend literal. Costs a little precision; removes a whole false-positive
    class that would otherwise bury the real findings.
    """
    fs, bs = fe.split("/"), be.split("/")
    if len(fs) != len(bs):
        return False
    return all(a == b or a == "{}" or b == "{}" for a, b in zip(fs, bs, strict=True))


def methods_for(fe_path: str, by_path: dict[str, set[str]]) -> set[str] | None:
    """Union of methods across every backend path this call could hit.

    ``None`` means no route matches at all (C1).
    """
    exact = by_path.get(fe_path)
    if exact:
        return exact
    matched: set[str] = set()
    for be_path, methods in by_path.items():
        if path_matches(fe_path, be_path):
            matched |= methods
    return matched or None


# ───────────────────────────── backend route table ─────────────────────────


def routes_from_app() -> list[dict]:
    """Ground truth: ask the running app for its route table.

    Most entries in ``app.routes`` are FastAPI ``_IncludedRouter`` wrappers,
    not routes — the real ``APIRoute``s hang off ``.original_router.routes``.
    Reading only the top level yields 9 routes instead of ~1100, so recurse.
    """
    from gdx_dispatch.app import app  # noqa: PLC0415 — optional, import-heavy

    out: list[dict] = []
    seen: set[int] = set()

    def walk(routes, prefix: str = "") -> None:
        for route in routes or ():
            if id(route) in seen:
                continue
            seen.add(id(route))

            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(getattr(inner, "routes", None), prefix)
                continue

            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            sub = getattr(route, "routes", None)

            if sub and not methods:  # Mount / sub-application
                walk(sub, prefix + (path or ""))
                continue
            if not path or not methods:
                continue
            endpoint = getattr(route, "endpoint", None)
            fqn = (
                f"{getattr(endpoint, '__module__', '?')}.{getattr(endpoint, '__name__', '?')}"
                if endpoint is not None else ""
            )
            for method in methods:
                if method in ("HEAD", "OPTIONS"):
                    continue
                # FIRST registration wins in this app (route-order shadowing),
                # so only keep the winner for each (method, path).
                if any(o["method"] == method and o["path"] == prefix + path for o in out):
                    continue
                out.append({"method": method, "path": prefix + path, "endpoint": fqn})

    walk(app.routes)
    return out


def routes_from_source(root: Path, tracked: list[str]) -> list[dict]:
    """Static fallback: resolve APIRouter(prefix=) + decorator paths."""
    out: list[dict] = []
    include_prefixes = _include_router_prefixes(root)

    for rel in tracked:
        if not rel.endswith(".py") or "/tests/" in rel:
            continue
        src = _read(root / rel)
        if "APIRouter" not in src and "@router." not in src and "@app." not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        # router variable -> prefix
        prefixes: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            call = node.value
            if not (isinstance(call, ast.Call) and _is_apirouter(call)):
                continue
            prefix = ""
            for kw in call.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    prefix = str(kw.value.value)
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    prefixes[tgt.id] = prefix

        extra = include_prefixes.get(rel, "")
        for node in ast.walk(tree):
            for dec in getattr(node, "decorator_list", []) or []:
                parsed = _route_from_decorator(dec)
                if not parsed:
                    continue
                var, method, raw = parsed
                prefix = prefixes.get(var, "")
                full = raw if raw.startswith("/api") else f"{prefix.rstrip('/')}{raw}"
                if not full.startswith("/api") and extra:
                    full = f"{extra.rstrip('/')}{full}"
                out.append({"method": method, "path": full or raw})
    return out


def _is_apirouter(call: ast.Call) -> bool:
    fn = call.func
    return (isinstance(fn, ast.Name) and fn.id == "APIRouter") or (
        isinstance(fn, ast.Attribute) and fn.attr == "APIRouter"
    )


def _route_from_decorator(dec: ast.AST) -> tuple[str, str, str] | None:
    if not isinstance(dec, ast.Call):
        return None
    fn = dec.func
    if not (isinstance(fn, ast.Attribute) and fn.attr in HTTP_METHODS):
        return None
    if not (dec.args and isinstance(dec.args[0], ast.Constant)):
        return None
    raw = dec.args[0].value
    if not isinstance(raw, str):
        return None
    var = fn.value.id if isinstance(fn.value, ast.Name) else ""
    return var, fn.attr.upper(), raw


def _include_router_prefixes(root: Path) -> dict[str, str]:
    """Map module rel-path -> prefix added at include_router() time."""
    src = _read(root / "gdx_dispatch" / "app.py")
    found: dict[str, str] = {}
    for m in re.finditer(
        r"include_router\(\s*([A-Za-z_][A-Za-z0-9_]*)[^)]*?prefix=[\"']([^\"']+)[\"']", src
    ):
        var, prefix = m.group(1), m.group(2)
        mod = re.search(rf"import\s+(\w+)\s+as\s+{re.escape(var)}\b", src)
        if mod:
            found[f"gdx_dispatch/routers/{mod.group(1)}.py"] = prefix
    return found


# ───────────────────────────── frontend call sites ─────────────────────────

RE_API_CALL = re.compile(
    r"\bapi\.(get|post|put|patch|del|delete|postQueued|patchQueued)\s*\(\s*([`'\"])([^`'\"]*)\2"
)
RE_FETCH = re.compile(r"\bfetch\s*\(\s*([`'\"])([^`'\"]*)\1(?:\s*,\s*\{[^}]*?method:\s*[`'\"](\w+)[`'\"])?")
RE_JS_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/|<!--.*?-->", re.S)


def frontend_calls(root: Path, tracked: list[str]) -> list[dict]:
    calls: list[dict] = []
    for rel in tracked:
        if not rel.endswith((".vue", ".js", ".ts")):
            continue
        if _is_excluded(rel):
            continue
        raw = _read(root / rel)
        if not raw:
            continue
        # strip comments so documentation examples aren't treated as calls
        src = RE_JS_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), raw)
        for m in RE_API_CALL.finditer(src):
            url = m.group(3)
            if not url.startswith("/api"):
                continue
            calls.append({
                "file": rel,
                "line": src[: m.start()].count("\n") + 1,
                "method": FE_METHOD_MAP[m.group(1)],
                "path": url,
                "expr": m.group(0)[:80],
            })
        for m in RE_FETCH.finditer(src):
            url = m.group(2)
            if not url.startswith("/api"):
                continue
            calls.append({
                "file": rel,
                "line": src[: m.start()].count("\n") + 1,
                "method": (m.group(3) or "GET").upper(),
                "path": url,
                "expr": m.group(0)[:80],
            })
    return calls


# ───────────────────────────── C3: response fields ─────────────────────────

RE_ASSIGNED_CALL = re.compile(
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*await\s+api\.(get|post|put|patch|del|delete)"
    r"\s*\(\s*([`'\"])([^`'\"]*)\3"
)


def handler_dict_keys(root: Path, tracked: list[str]) -> dict[tuple[str, str], set[str] | None]:
    """(METHOD, normalized path) -> top-level keys of a literal dict return.

    ``None`` means "cannot know" (response_model, non-literal return, multiple
    shapes) — those are skipped rather than guessed at.
    """
    result: dict[tuple[str, str], set[str] | None] = {}
    include_prefixes = _include_router_prefixes(root)

    for rel in tracked:
        if not rel.endswith(".py") or "/tests/" in rel:
            continue
        src = _read(root / rel)
        if "@router." not in src and "@app." not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue

        prefixes: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and _is_apirouter(node.value):
                pfx = ""
                for kw in node.value.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        pfx = str(kw.value.value)
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        prefixes[tgt.id] = pfx
        extra = include_prefixes.get(rel, "")

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route = None
            has_response_model = False
            for dec in node.decorator_list:
                parsed = _route_from_decorator(dec)
                if parsed:
                    route = parsed
                    if isinstance(dec, ast.Call):
                        for kw in dec.keywords:
                            if kw.arg == "response_model" and not (
                                isinstance(kw.value, ast.Constant) and kw.value.value is None
                            ):
                                has_response_model = True
            if not route:
                continue
            var, method, raw = route
            prefix = prefixes.get(var, "")
            full = raw if raw.startswith("/api") else f"{prefix.rstrip('/')}{raw}"
            if not full.startswith("/api") and extra:
                full = f"{extra.rstrip('/')}{full}"
            key = (method, normalize(full))

            if has_response_model:
                result[key] = None
                continue

            returns = [
                n for n in ast.walk(node)
                if isinstance(n, ast.Return) and n.value is not None
            ]
            keysets: list[set[str]] = []
            knowable = bool(returns)
            for ret in returns:
                if isinstance(ret.value, ast.Dict):
                    ks = set()
                    for k in ret.value.keys:
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            ks.add(k.value)
                        else:
                            knowable = False
                    keysets.append(ks)
                else:
                    knowable = False
            # Union across return sites; a single non-literal return makes the
            # shape unknowable and the route is skipped.
            result[key] = set().union(*keysets) if (knowable and keysets) else None
    return result


def _scope_window(lines: list[str], start: int, var: str) -> list[str]:
    """Lines from the call site to the end of its enclosing block.

    A fixed line window is wrong: Vue files declare `const response = ...`
    in one function and again in the next, so a flat 25-line lookahead
    attributed the SECOND function's field reads to the FIRST function's
    endpoint. Stop at a dedent past the declaration (end of the enclosing
    function) or at a re-declaration of the same name.
    """
    decl = lines[start]
    base = len(decl) - len(decl.lstrip())
    redecl = re.compile(rf"\b(?:const|let|var)\s+{re.escape(var)}\b")
    out = [decl]
    for line in lines[start + 1: start + 60]:
        if not line.strip():
            out.append(line)
            continue
        if len(line) - len(line.lstrip()) < base:
            break
        if redecl.search(line):
            break
        out.append(line)
    return out


def phantom_fields(root: Path, tracked: list[str], handler_keys) -> list[dict]:
    out: list[dict] = []
    for rel in tracked:
        if not rel.endswith((".vue", ".js", ".ts")) or "/frontend/" not in rel:
            continue
        if _is_excluded(rel):
            continue
        raw = _read(root / rel)
        if not raw:
            continue
        src = RE_JS_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), raw)
        lines = src.split("\n")
        for m in RE_ASSIGNED_CALL.finditer(src):
            var, method, url = m.group(1), FE_METHOD_MAP[m.group(2)], m.group(4)
            if not url.startswith("/api"):
                continue
            keys = handler_keys.get((method, normalize(url)))
            if not keys:          # unknown shape or no route — C1/C2 cover the latter
                continue
            start = src[: m.start()].count("\n")
            window = "\n".join(_scope_window(lines, start, var))
            for fm in re.finditer(rf"\b{re.escape(var)}\s*\??\.\s*([A-Za-z_$][\w$]*)", window):
                field = fm.group(1)
                if field in keys or field in ("then", "catch", "finally", "data", "value"):
                    continue
                out.append({
                    "check": "C3",
                    "file": rel,
                    "line": start + 1 + window[: fm.start()].count("\n"),
                    "detail": (
                        f"reads `{var}.{field}` from {method} {url}, but the handler "
                        f"returns only {sorted(keys)}"
                    ),
                    "expr": fm.group(0),
                })
    return out


# ───────────────────────────── C5: stub endpoints ──────────────────────────


def _is_empty_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Dict):
        return bool(node.values) and all(_is_empty_literal(v) for v in node.values)
    if isinstance(node, (ast.List, ast.Tuple)):
        return not node.elts
    if isinstance(node, ast.Constant):
        return node.value in (None, 0, "", False)
    return False


_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _sentinel_helpers(tree: ast.Module) -> set[str]:
    """Module-level helpers that take no args and return only a literal.

    `ui_compat._ok()` -> `{"ok": True}`. A handler whose whole body is
    `return _ok()` did no work; one whose body is
    `return add_proposal_tier(estimate_id, ...)` delegates to a service and
    is a perfectly normal thin controller. Without this distinction C6
    reports every thin controller in the codebase.
    """
    out: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        a = node.args
        if a.args or a.posonlyargs or a.kwonlyargs or a.vararg or a.kwarg:
            continue
        ret = _single_return(node)
        if ret is not None and ret.value is not None and isinstance(
            ret.value, (ast.Dict, ast.List, ast.Tuple, ast.Constant)
        ):
            out.add(node.name)
    return out


def _does_no_work(value: ast.AST, sentinels: set[str]) -> bool:
    """True when the returned expression cannot have changed anything."""
    if isinstance(value, (ast.Dict, ast.List, ast.Tuple, ast.Constant)):
        return True
    # `return _ok()` — a no-arg call to a literal-returning local helper
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in sentinels
        and not value.args
        and not value.keywords
    )


def _single_return(node) -> ast.Return | None:
    """The lone `return` making up this function's whole body, if any."""
    body = [
        st for st in node.body
        if not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant))
    ]
    if len(body) == 1 and isinstance(body[0], ast.Return):
        return body[0]
    return None


def stub_endpoints(
    root: Path, tracked: list[str], live_endpoints: set[str] | None = None
) -> list[dict]:
    """C5 read stubs and C6 fake-success mutations.

    Both are "the body is nothing but a return", split by verb because the
    failure modes differ. A GET that returns blanks shows an empty page —
    visibly wrong. A PATCH that returns `{"ok": true}` shows a success toast
    — invisibly wrong, which is worse.

    Deliberately strict: a `delete_*` that does real work and returns `{}` is
    NOT a stub, so the body must contain nothing but the return.
    """
    out: list[dict] = []
    for rel in tracked:
        if not rel.endswith(".py") or "/tests/" in rel:
            continue
        src = _read(root / rel)
        if "@router." not in src and "@app." not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        sentinels = _sentinel_helpers(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            routes = [d for d in node.decorator_list if _route_from_decorator(d)]
            if not routes:
                continue
            ret = _single_return(node)
            if ret is None or ret.value is None:
                continue
            var, method, raw = _route_from_decorator(routes[0])

            # A no-op that loses route-order arbitration to a real handler is
            # harmless dead code, not a live bug. Only reachable when a live
            # route table was supplied — without one, everything is reported.
            if live_endpoints is not None:
                mod = rel[:-3].replace("/", ".")
                if f"{mod}.{node.name}" not in live_endpoints:
                    continue

            if method in _MUTATING:
                if not _does_no_work(ret.value, sentinels):
                    continue
                out.append({
                    "check": "C6", "file": rel, "line": node.lineno,
                    "detail": (
                        f"{method} {raw} — mutating handler whose whole body is a "
                        f"return; it changes nothing but answers success"
                    ),
                    "expr": node.name,
                })
            elif _is_empty_literal(ret.value):
                out.append({
                    "check": "C5", "file": rel, "line": node.lineno,
                    "detail": (
                        f"{method} {raw} — handler body is nothing but an empty "
                        f"return; callers always see a blank result"
                    ),
                    "expr": node.name,
                })
    return out


# ─────────────────────────────────── scan ──────────────────────────────────


def scan(root: Path, checks, routes: list[dict] | None = None) -> list[dict]:
    checks = set(checks)
    tracked = _tracked(root)
    routes = routes if routes is not None else routes_from_source(root, tracked)

    by_path: dict[str, set[str]] = defaultdict(set)
    for r in routes:
        by_path[normalize(r["path"])].add(r["method"])

    calls = frontend_calls(root, tracked)
    findings: list[dict] = []

    for call in calls:
        norm = normalize(call["path"])
        if norm.startswith(_NON_ROUTE_PREFIXES):
            continue
        served = methods_for(norm, by_path)
        if not served:
            if "C1" in checks:
                findings.append({
                    "check": "C1", "file": call["file"], "line": call["line"],
                    "detail": f"{call['method']} {call['path']} — no backend route serves this path",
                    "expr": call["expr"],
                })
        elif call["method"] not in served and "C2" in checks:
            findings.append({
                "check": "C2", "file": call["file"], "line": call["line"],
                "detail": (
                    f"{call['method']} {call['path']} — path exists but only for "
                    f"{sorted(served)}; this request gets 405"
                ),
                "expr": call["expr"],
            })

    if "C3" in checks:
        findings += phantom_fields(root, tracked, handler_dict_keys(root, tracked))

    if {"C5", "C6"} & checks:
        live_eps = (
            {r["endpoint"] for r in routes if r.get("endpoint")}
            if routes and any(r.get("endpoint") for r in routes) else None
        )
        findings += [
            f for f in stub_endpoints(root, tracked, live_eps) if f["check"] in checks
        ]

    if "C4" in checks:
        called = {normalize(c["path"]) for c in calls}
        for path in sorted(by_path):
            if path in called:
                continue
            findings.append({
                "check": "C4", "file": "-", "line": 0,
                "detail": f"{sorted(by_path[path])} {path} — no frontend caller",
                "expr": "",
            })

    order = {c: i for i, c in enumerate(ALL_CHECKS)}
    findings.sort(key=lambda f: (order[f["check"]], f["file"], f["line"]))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", default=",".join(DEFAULT_CHECKS),
                    help=f"subset of {','.join(ALL_CHECKS)} (C4 is off by default)")
    ap.add_argument("--routes", default=None, help="JSON route table from --dump-routes")
    ap.add_argument("--dump-routes", default=None, help="import the app and write its route table")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--root", default=str(REPO_ROOT))
    args = ap.parse_args(argv)

    if args.dump_routes:
        table = routes_from_app()
        Path(args.dump_routes).write_text(json.dumps(table, indent=1))
        print(f"wrote {len(table)} routes -> {args.dump_routes}")
        return 0

    checks = [c.strip().upper() for c in args.check.split(",") if c.strip()]
    unknown = set(checks) - set(ALL_CHECKS)
    if unknown:
        print(f"unknown check(s): {sorted(unknown)}", file=sys.stderr)
        return 2

    routes = None
    if args.routes:
        routes = json.loads(Path(args.routes).read_text())

    findings = scan(Path(args.root), checks, routes)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(findings, indent=1))

    for f in findings:
        loc = f"{f['file']}:{f['line']}" if f["file"] != "-" else ""
        print(f"{f['check']}  {loc}\n      {f['detail']}")

    counts: dict[str, int] = defaultdict(int)
    for f in findings:
        counts[f["check"]] += 1
    print("\n=== frontend-contract summary ===")
    print(f"  route table: {'live app' if args.routes else 'static parse'}")
    for c in ALL_CHECKS:
        if c in checks:
            print(f"  {c}: {counts[c]}")
    print(f"  total: {len(findings)}")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
