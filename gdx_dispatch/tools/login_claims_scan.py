"""Login-claims scan — flags a read of a claim the login dict never carries.

Bug class this catches (#701)
-----------------------------
``get_current_user`` returns exactly ``{"user_id", "tenant_id", "role"}``
(built in ``routers/auth/core.finalize_login_jwt``, its single decode path).
No ``email``, no ``name``, no ``sub``. A handler that records who acted from
``user.get("email")`` — or ``name``, ``display_name``, or ``sub`` read alone —
stores NULL or a placeholder, silently. Measured on prod 2026-09-10: 10/10
purchase orders and 7/19 payment reminders with no actor; one server error
"resolved by" ``system`` that a person resolved. ``notes.py`` had fixed its
own copy of this; the siblings had not, which is why this is a guard and not
another one-file fix.

What it flags
-------------
For every name bound to the login dict — a parameter defaulting to
``Depends(get_current_user)`` (or ``require_auth`` imported as it) or to a
same-module dependency that returns its own login-bound parameter unchanged
(``admin_ops._require_admin``), or a name assigned from the stashed copy on
``request.state.user`` — every ``name.get("<key>")`` or ``name["<key>"]`` whose
key is not ``user_id``, ``tenant_id`` or ``role``.

A read inside an ``or`` chain that also reads the id the principal really
carries is not flagged: ``user.get("sub") or user.get("user_id")`` is dead in
its first half but lands on the right id. (On a raw-claims stash the carried id
is ``sub`` — there, ``.get("user_id") or "-"`` rescues nothing.) The login dict is followed into same-module helpers it is
passed to (``signatures._user_label(user)``), because that is where half of
these hid.

``request.state.current_user`` is followed too, against the other shape it
holds in production — the raw JWT claims ``require_role`` stashes (``sub``,
``tenant_id``, ``role``, ``jti``, ``typ``, ``exp``; no ``user_id``, no email).
So is a read straight off a stash with no name in between — ``.get("k")`` or
``["k"]``, through ``(… or {})`` too:
``getattr(request.state, "current_user", {}).get("user_id")`` logged every
GDPR data access on prod as user ``-`` (4,065 of 4,065 rows, 2026-09-11). An
``or`` of the two stashes may hold either shape, so it is allowed only the keys
both carry (``tenant_id``, ``role``).
(A ``Depends(require_role(...))`` parameter is not a principal at all — it is
None or a ``Principal`` object — so it is not checked.)

What it cannot see
------------------
* A principal reached any other way — a module-level ``Depends`` alias used as
  a default, a helper in another module.
* A dict copied or rebuilt before the read (``u = dict(user)``; a plain alias,
  ``user = current_user or {}``, IS followed), or a key held in a variable.

Usage
-----
    python -m gdx_dispatch.tools.login_claims_scan

Exits 1 on an unallowed finding or a stale ``ALLOWED`` entry. Pinned by
``tests/test_login_claims_scan.py::test_repo_scan_is_clean``.
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = ("tests/", "migrations/", "frontend/", "node_modules/")
LOGIN_KEYS = frozenset({"user_id", "tenant_id", "role"})
# `request.state.current_user` is what `require_role` stashes when no login
# dict is on the request: the RAW JWT claims (`_issue` mints sub, tenant_id,
# role, jti, typ, exp). It never holds a `user_id` in production — the login
# dict shape only lands there under a test's dependency override.
RAW_CLAIM_KEYS = frozenset({"sub", "tenant_id", "role", "jti", "typ", "exp", "iat", "gdx_tid"})
LOGIN_DEPENDENCIES = frozenset({"get_current_user"})
# The id a principal carries: user_id on the login dict, sub on raw claims.
IDENTITY_KEYS = frozenset({"user_id", "sub"})

# (path relative to the package, function, key) -> why it is not a defect.
ALLOWED: dict[tuple[str, str, str], str] = {
    ("modules/ledger/router.py", "_tenant_id", "company_id"): (
        "dead fallback after tenant_id, which the login dict always carries"
    ),
    ("routers/resources.py", "_user_role", "job_title"): "dead fallback after role, which it always carries",
    ("routers/support.py", "_resolve_user", "email"): (
        "the next line resolves the email from the users row when this is empty (#622)"
    ),
    ("routers/support.py", "_resolve_user", "preferred_username"): (
        "the next line resolves the email from the users row when this is empty (#622)"
    ),
    ("routers/gdpr.py", "export_my_data", "email"): (
        "dead half of an OR filter whose other half matches Job.assigned_to on the user id"
    ),
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    function: str
    key: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}\t{self.function}\treads {self.key!r} off the login dict"


def _login_aliases(tree: ast.Module) -> set[str]:
    """Local names that mean get_current_user in this module."""
    names = set(LOGIN_DEPENDENCIES)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name in LOGIN_DEPENDENCIES:
                    names.add(a.asname or a.name)
    return names


def _depends_target(default: ast.AST | None) -> str | None:
    if isinstance(default, ast.Call) and getattr(default.func, "id", None) == "Depends" and default.args:
        target = default.args[0]
        if isinstance(target, ast.Name):
            return target.id
    return None


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[ast.arg, ast.AST | None]]:
    positional = fn.args.posonlyargs + fn.args.args
    defaults = [None] * (len(positional) - len(fn.args.defaults)) + list(fn.args.defaults)
    return list(zip(positional, defaults, strict=True)) + list(zip(fn.args.kwonlyargs, fn.args.kw_defaults, strict=True))


def _key_of(node: ast.AST, name: str) -> tuple[str, ast.AST] | None:
    """('email', node) for ``name.get("email", ...)`` or ``name["email"]``."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == name
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        return node.args[0].value, node
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == name
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value, node
    return None


def _reads(fn: ast.AST, name: str, allowed: frozenset[str]) -> list[tuple[str, int]]:
    """Keys outside ``allowed`` read off ``name`` in ``fn``, minus fallback
    chains that also read the id this principal really carries."""
    rescued: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            keys = [_key_of(v, name) for v in node.values]
            if any(k and k[0] in IDENTITY_KEYS & allowed for k in keys):
                rescued.update(id(k[1]) for k in keys if k)
    out = []
    for node in ast.walk(fn):
        hit = _key_of(node, name)
        if hit and hit[0] not in allowed and id(hit[1]) not in rescued:
            out.append((hit[0], node.lineno))
    return out


def _is_state(node: ast.AST) -> bool:
    """``request.state`` or ``getattr(request, "state", ...)``."""
    if isinstance(node, ast.Attribute) and node.attr == "state":
        return True
    return (
        isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "state"
    )


def _stashed_keys(node: ast.AST) -> frozenset[str] | None:
    """The keys a stashed principal may hold, or None if ``node`` isn't one.

    ``state.user`` has one writer, ``finalize_login_jwt`` — the login dict.
    ``state.current_user`` holds the raw JWT claims ``require_role`` stashed.
    ``state.user or state.current_user`` (``audit._request_user_dict``) may be
    either, so only the keys BOTH carry are safe; ``{}``/``None`` fallbacks in
    the chain add nothing."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        shapes = [k for k in (_stashed_keys(v) for v in node.values) if k is not None]
        return frozenset.intersection(*shapes) if shapes else None
    attr = None
    if isinstance(node, ast.Attribute) and _is_state(node.value):
        attr = node.attr
    elif (
        isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "getattr"
        and len(node.args) >= 2
        and _is_state(node.args[0])
        and isinstance(node.args[1], ast.Constant)
    ):
        attr = node.args[1].value
    return {"user": LOGIN_KEYS, "current_user": RAW_CLAIM_KEYS}.get(attr)


def _stash_key_of(node: ast.AST) -> tuple[str, frozenset[str]] | None:
    """(key, allowed) for ``<stash>.get("k")`` or ``<stash>["k"]``."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ):
        keys = _stashed_keys(node.func.value)
        return (node.args[0].value, keys) if keys is not None else None
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        keys = _stashed_keys(node.value)
        return (node.slice.value, keys) if keys is not None else None
    return None


def _inline_stash_reads(fn: ast.AST) -> list[tuple[str, int]]:
    rescued: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            hits = [(v, _stash_key_of(v)) for v in node.values]
            # `.get("user_id") or "-"` on a raw-claims stash IS the bug; only a
            # read of the id the stash really carries rescues the chain.
            if any(h and h[0] in IDENTITY_KEYS & h[1] for _, h in hits):
                rescued.update(id(v) for v, h in hits if h)
    out = []
    for node in ast.walk(fn):
        hit = _stash_key_of(node)
        if hit and hit[0] not in hit[1] and id(node) not in rescued:
            out.append((hit[0], node.lineno))
    return out


def _scan_module(rel: str, tree: ast.Module) -> list[Finding]:
    aliases = _login_aliases(tree)
    functions = {
        fn.name: fn for fn in tree.body if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    # Same-module dependencies that hand the login dict straight back.
    passthrough: set[str] = set()
    for name, fn in functions.items():
        bound = {a.arg for a, d in _params(fn) if _depends_target(d) in aliases}
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and isinstance(n.value, ast.Name)]
        if bound and returns and all(r.value.id in bound for r in returns):
            passthrough.add(name)

    # (function, name, allowed keys) triples holding a principal, grown
    # through aliases and same-module calls that pass it on.
    seeds: list[tuple[ast.AST, str, frozenset[str]]] = []
    findings_inline: list[Finding] = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for a, d in _params(fn):
                target = _depends_target(d)
                if target in aliases or target in passthrough:
                    seeds.append((fn, a.arg, LOGIN_KEYS))
            # `getattr(request.state, "current_user", {}).get("user_id")` — a
            # read straight off a stash, no name in between (data_access_logger).
            findings_inline.extend(Finding(rel, line, fn.name, key) for key, line in _inline_stash_reads(fn))
            # `x = getattr(request.state, "user", None)` — a stashed copy.
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                    value = node.value
                    if isinstance(value, ast.IfExp):  # `getattr(...) if request else None`
                        value = value.body
                    keys = _stashed_keys(value)
                    if keys is not None:
                        seeds.append((fn, node.targets[0].id, keys))

    seen: set[tuple[int, str, frozenset[str]]] = set()
    findings: list[Finding] = list(findings_inline)
    while seeds:
        fn, pname, keys = seeds.pop()
        if (id(fn), pname, keys) in seen:
            continue
        seen.add((id(fn), pname, keys))
        for key, line in _reads(fn, pname, keys):
            findings.append(Finding(rel, line, fn.name, key))
        # `user = current_user or {}` (mobile_chat) — an alias is the same dict.
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                value = node.value
                if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
                    value = value.values[0]
                if isinstance(value, ast.Name) and value.id == pname:
                    seeds.append((fn, node.targets[0].id, keys))
        for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call)):
            callee = functions.get(getattr(call.func, "id", None))
            if callee is None or callee is fn:
                continue
            params = [a.arg for a, _ in _params(callee)]
            for i, arg in enumerate(call.args):
                if isinstance(arg, ast.Name) and arg.id == pname and i < len(params):
                    seeds.append((callee, params[i], keys))
            for kw in call.keywords:
                if isinstance(kw.value, ast.Name) and kw.value.id == pname and kw.arg in params:
                    seeds.append((callee, kw.arg, keys))
    return findings


def scan(root: Path = PACKAGE_ROOT) -> list[Finding]:
    found: list[Finding] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if any(part in rel for part in SKIP_PARTS):
            continue
        try:
            tree = ast.parse(path.read_text(errors="replace"))
        except SyntaxError:
            continue
        found.extend(_scan_module(rel, tree))
    return sorted(set(found), key=lambda f: (f.path, f.line, f.key))


def unallowed(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if (f.path, f.function, f.key) not in ALLOWED]


def stale_allowances(findings: list[Finding]) -> list[tuple[str, str, str]]:
    hit = {(f.path, f.function, f.key) for f in findings}
    return sorted(k for k in ALLOWED if k not in hit)


def main(argv: list[str] | None = None) -> int:
    findings = scan()
    bad, stale = unallowed(findings), stale_allowances(findings)
    for f in bad:
        print(f)
    for entry in stale:
        print(f"STALE ALLOWED entry — matches nothing now, remove it: {entry}")
    print(
        f"{len(findings)} read(s) of a claim the login dict never carries; "
        f"{len(findings) - len(bad)} allowed, {len(bad)} not; {len(stale)} stale allowance(s)",
        file=sys.stderr,
    )
    return 1 if bad or stale else 0


if __name__ == "__main__":
    sys.exit(main())
