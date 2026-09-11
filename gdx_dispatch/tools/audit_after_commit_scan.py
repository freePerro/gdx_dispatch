"""Audit-after-commit scan — flags an audit write that no commit follows.

Bug class this catches (#700)
-----------------------------
``log_audit_event[_sync]`` only ``add``s and ``flush``es its row, and
``core.database.get_db()`` closes the session WITHOUT committing. So a handler
that commits its change and THEN writes the audit row loses the row at the end
of every request: the change is durable, its record never is, and nothing
errors or logs. Measured on prod 2026-09-10: 7 support tickets, 8 holding
areas and 49 job assignments with no audit trail at all. That breaks
invariant #1 (who did it, what changed, when).

The fix is to stage the row before the single commit, so the change and its
record land or roll back together. Where the change was committed by a helper
the handler cannot reach into (a sync, a service that commits), the row gets a
commit of its own right after.

What it flags
-------------
For every audit write in a function — a direct ``log_audit_event``,
``log_audit_event_sync`` or ``audit_or_rollback`` call, an ``AuditLog(...)``
built by hand (``modules/ledger``), or a call to a helper that can return with
a row still pending (found transitively, whether or not the helper commits
somewhere: ``core/payments._audit_money_event`` primes the guard — a commit —
and then leaves its row for the caller) — the scan asks whether a commit can
run AFTER it on its path to the function exit:

* ``after-commit``: a commit runs before the audit write and none after it.
  This is the #700 shape.
* ``no-commit``: a route handler whose audit write has no commit on its path
  at all, so the row (and possibly the change) never lands.

"After it" is path-aware: the rest of the audit's own block, then the blocks
enclosing it, stopping at a ``return`` or ``raise``. ``continue``/``break`` end
only the current block — the statements after the loop still run. So an
early-return branch that audits after a commit is flagged even when a later,
unrelated commit exists further down the function.

Only a commit that MUST run lands the row:

* ``.commit()`` itself, or a call to a function whose own body commits on every
  path (``forecast_service.update_settings`` ends in ``db.commit()``). A callee
  that commits only sometimes does not count: ``routers/budgets.py`` looked
  safe because a revenue helper three calls down commits, but only when it has
  to create a settings row, which on prod it never does;
* on every branch — an ``if`` lands only when its body AND its ``else`` do; a
  loop never does (it may run zero times). ``routers/parts_needed.py`` committed
  after its audit row only inside ``if urgency == "critical":``, so every
  ordinary part lost its row.

Counterfactuals (what would make a finding wrong)
-------------------------------------------------
* A caller commits after this function returns. Helpers like that are fine
  and belong in ``ALLOWED`` with the caller named.

What it cannot see (each checked against the tree 2026-09-11: no live instance)
-------------------------------------------------------------------------------
* An exit on ONE branch between the audit write and the commit that lands it:
  ``audit(...); if not changed: return; db.commit()`` reads as landing, because
  the later commit is taken to run on every path.
* A commit inside a ``try`` whose earlier statement can raise into a
  swallowing ``except``: ``try: notify(); db.commit() except: pass`` reads as
  landing.
* An audit row written as raw SQL (``INSERT INTO audit_logs`` in
  ``routers/mobile.py``) — only the writers above are recognized.
* Which session a ``.commit()`` belongs to. Any ``x.commit()`` counts, so
  ``db.commit(); audit(db); other_db.commit()`` reads as landing.
* Any other call it cannot resolve (``obj.method()``) is neither an audit write
  nor a commit. The only method that writes an audit row is
  ``AuditMiddleware.dispatch``, and it commits itself.

Usage
-----
    python -m gdx_dispatch.tools.audit_after_commit_scan

Exits 1 when an unallowed finding exists or an ``ALLOWED`` entry no longer
matches anything (a stale exception is itself a defect). Pinned by
``tests/test_audit_after_commit_scan.py::test_repo_scan_is_clean``.
"""
from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = ("tests/", "migrations/", "frontend/", "node_modules/")
# `AuditLog` counts as a writer when it is CALLED — a row built by hand, as the
# ledger does — not when it is merely selected from.
AUDIT_WRITERS = frozenset({"log_audit_event", "log_audit_event_sync", "audit_or_rollback", "AuditLog"})
ROUTE_DECORATORS = frozenset({"get", "post", "put", "patch", "delete", "api_route", "websocket"})
COMMIT = "<commit>"

# (path relative to the package, function name) -> why it is not a defect.
# Keyed by function, not line, so an unrelated edit above it never trips the gate.
ALLOWED: dict[tuple[str, str], str] = {
    ("core/audit.py", "_log_audit_event_impl"): (
        "the writer itself: it primes the guard (which can commit) and then builds "
        "the row; its caller's commit is what lands it"
    ),
    ("core/payments.py", "_audit_money_event"): (
        "helper: both callers stage it inside _mark_invoice_paid, whose single "
        "db.commit() lands the alert with the payment (#661)"
    ),
    ("core/quickbooks.py", "pull_accounts"): (
        "no caller: the live pull is modules/quickbooks/sync.pull_accounts; "
        "this legacy SDK copy is dead code (#700 close-out)"
    ),
    ("core/quickbooks.py", "pull_bank_transactions"): (
        "no caller: the live pull is modules/quickbooks/sync.pull_bank_transactions; "
        "dead code (#700 close-out)"
    ),
    ("routers/commission.py", "set_rules"): "commission is leaving core for a plugin; not fixed in core (#700)",
    ("routers/commission.py", "update_rule"): "commission is leaving core for a plugin; not fixed in core (#700)",
    ("routers/commission.py", "calculate_commission"): (
        "commission is leaving core for a plugin; not fixed in core (#700)"
    ),
    ("routers/ui_compat.py", "run_admin_op"): (
        "not this class: the audit call is disabled outright (`if False`) on a "
        "stub that returns success without doing anything — raised separately (#700 close-out)"
    ),
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    function: str
    kind: str  # "after-commit" | "no-commit"

    def __str__(self) -> str:
        return f"{self.path}:{self.line}\t{self.function}\t{self.kind}"


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.With, ast.AsyncWith, ast.Match)
_EXITS = (ast.Return, ast.Raise)
_LOOP_JUMPS = (ast.Continue, ast.Break)


def _own_calls(node: ast.AST) -> list[ast.Call]:
    """Calls inside ``node``, not descending into nested defs, lambdas or classes."""
    out: list[ast.Call] = [node] if isinstance(node, ast.Call) else []

    def visit(n: ast.AST) -> None:
        for child in ast.iter_child_nodes(n):
            if isinstance(child, _SCOPES):
                continue
            if isinstance(child, ast.Call):
                out.append(child)
            visit(child)

    visit(node)
    return out


def _module_name(rel: str) -> str:
    return "gdx_dispatch." + rel[:-3].replace("/", ".").removesuffix(".__init__")


class _Index:
    """Every function in the package, resolved through each module's imports."""

    def __init__(self, root: Path) -> None:
        self.trees: dict[str, ast.Module] = {}
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            if any(part in rel for part in SKIP_PARTS):
                continue
            try:
                self.trees[rel] = ast.parse(path.read_text(errors="replace"))
            except SyntaxError:
                continue

        self.symbols: dict[str, dict[str, str]] = {}
        self.aliases: dict[str, dict[str, str]] = {}
        self.fn_by_name: dict[str, ast.AST] = {}
        self.rel_of: dict[int, str] = {}
        for rel, tree in self.trees.items():
            self._index_module(rel, tree)

        self.calls_of = {
            q: [self.resolve(self.rel_of[id(fn)], c) for c in _own_calls(fn)]
            for q, fn in self.fn_by_name.items()
        }
        self.committers, self.auditors = self._close_over()
        self.always_commits: dict[str, bool] = {}
        self._add_committing_writers()

    def _index_module(self, rel: str, tree: ast.Module) -> None:
        mod = _module_name(rel)
        table: dict[str, str] = {}
        aliases: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                table[node.name] = f"{mod}.{node.name}"
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                base = node.module
                if node.level:
                    base = ".".join(mod.split(".")[: -node.level] + [node.module])
                for a in node.names:
                    table.setdefault(a.asname or a.name, f"{base}.{a.name}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    local = a.asname or a.name.split(".")[0]
                    aliases[local] = a.name if a.asname else a.name.split(".")[0]
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = table.get(fn.name, "")
                if not q.startswith(mod + ".") or self.fn_by_name.get(q) not in (None, fn):
                    q = f"{mod}.<local>.{fn.name}.{fn.lineno}"
                self.fn_by_name[q] = fn
                self.rel_of[id(fn)] = rel
        self.symbols[rel], self.aliases[rel] = table, aliases

    def resolve(self, rel: str, call: ast.Call) -> str:
        f = call.func
        if isinstance(f, ast.Name):
            return self.symbols[rel].get(f.id, f"?.{f.id}")
        if isinstance(f, ast.Attribute):
            if f.attr == "commit":
                return COMMIT
            if isinstance(f.value, ast.Name):
                owner = f.value.id
                if owner in self.aliases[rel]:
                    return f"{self.aliases[rel][owner]}.{f.attr}"
                if owner in self.symbols[rel]:  # `from pkg import module` then module.fn()
                    return f"{self.symbols[rel][owner]}.{f.attr}"
            return f"?attr.{f.attr}"
        return "?"

    @staticmethod
    def is_writer(q: str) -> bool:
        return q.rsplit(".", 1)[-1] in AUDIT_WRITERS

    def _add_committing_writers(self) -> None:
        """A helper that commits AND can return with an audit row still pending
        is an audit writer to its callers. ``_audit_money_event`` primes the
        guard (a commit) and then flushes its row for the caller to commit; a
        caller that commits first and calls it after loses the row. The first
        pass only knew helpers that never commit."""
        changed = True
        while changed:
            changed = False
            for q, fn in self.fn_by_name.items():
                if q in self.auditors or q not in self.committers or self.is_writer(q):
                    continue
                if _FunctionScan(self, self.rel_of[id(fn)], fn).leaves_a_row_pending():
                    self.auditors.add(q)
                    changed = True

    def _close_over(self) -> tuple[set[str], set[str]]:
        committers: set[str] = {COMMIT}
        auditors: set[str] = set()
        changed = True
        while changed:
            changed = False
            for q, calls in self.calls_of.items():
                if q not in committers and not self.is_writer(q) and any(c in committers for c in calls):
                    committers.add(q)
                    changed = True
            for q, calls in self.calls_of.items():
                if q in auditors or q in committers or self.is_writer(q):
                    continue
                if any(self.is_writer(c) or c in auditors for c in calls):
                    auditors.add(q)
                    changed = True
        return committers, auditors


class _FunctionScan:
    def __init__(self, index: _Index, rel: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.index, self.rel, self.fn = index, rel, fn

    def is_audit(self, call: ast.Call) -> bool:
        q = self.index.resolve(self.rel, call)
        return self.index.is_writer(q) or q in self.index.auditors

    def is_commit(self, call: ast.Call) -> bool:
        return self.index.resolve(self.rel, call) in self.index.committers

    def _callee_always_commits(self, q: str) -> bool:
        """True when every path through the callee's own body commits —
        ``forecast_service.update_settings`` ends in ``db.commit()``;
        ``get_or_create_settings`` commits only when it has to create a row."""
        if q not in self.index.committers or q == COMMIT:
            return False
        cache = self.index.always_commits
        if q not in cache:
            cache[q] = False  # a cycle reads as "not guaranteed"
            fn = self.index.fn_by_name.get(q)
            if fn is not None:
                cache[q] = _FunctionScan(self.index, self.index.rel_of[id(fn)], fn)._block_must_commit(fn.body)
        return cache[q]

    def _commits_directly(self, node: ast.AST) -> bool:
        """`.commit()`, or a call to a function that always commits, in ``node``."""
        for call in _own_calls(node):
            q = self.index.resolve(self.rel, call)
            if q == COMMIT or self._callee_always_commits(q):
                return True
        return False

    def _block_must_commit(self, block: list[ast.stmt]) -> bool:
        for st in block:
            if self.lands(st):
                return True
            if isinstance(st, _EXITS + _LOOP_JUMPS):
                return False
        return False

    def lands(self, st: ast.stmt) -> bool:
        """Does running ``st`` always commit? A commit inside one branch of an
        `if`, or inside a loop that may not run, does not land the row
        (`routers/parts_needed.py` committed only for `critical` urgency)."""
        if isinstance(st, ast.If):
            if self._commits_directly(st.test):
                return True
            return self._block_must_commit(st.body) and self._block_must_commit(st.orelse)
        if isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
            heads = [getattr(st, "iter", None), getattr(st, "test", None)]
            return any(h is not None and self._commits_directly(h) for h in heads)
        if isinstance(st, ast.Try):
            return self._block_must_commit(st.body) or self._block_must_commit(st.finalbody)
        if isinstance(st, (ast.With, ast.AsyncWith)):
            heads = [item.context_expr for item in st.items]
            return any(self._commits_directly(h) for h in heads) or self._block_must_commit(st.body)
        if isinstance(st, ast.Match):
            return False
        return self._commits_directly(st)

    def _statements(self):
        def walk(block, ancestors):
            for i, st in enumerate(block):
                yield block, i, st, ancestors
                inner = ancestors + [(block, i, st)]
                for field in ("body", "orelse", "finalbody"):
                    sub = getattr(st, field, None)
                    if isinstance(sub, list) and sub:
                        yield from walk(sub, inner)
                for handler in getattr(st, "handlers", None) or []:
                    yield from walk(handler.body, inner)
                if isinstance(st, ast.Match):
                    for case in st.cases:
                        yield from walk(case.body, inner)

        yield from walk(self.fn.body, [])

    def _lands_later(self, block, i, ancestors) -> bool:
        def rest(stmts) -> bool | None:
            for st in stmts:
                if self.lands(st):
                    return True
                if isinstance(st, _EXITS):
                    return False
                if isinstance(st, _LOOP_JUMPS):
                    return None  # the block ends here; what follows the loop still runs
            return None

        verdict = rest(block[i + 1:])
        if verdict is not None:
            return verdict
        for pblock, pi, pst in reversed(ancestors):
            if isinstance(pst, ast.Try) and self._block_must_commit(pst.finalbody):
                return True
            verdict = rest(pblock[pi + 1:])
            if verdict is not None:
                return verdict
        return False

    def _audit_statements(self):
        """(block, index, statement, ancestors, last audit call) per statement
        that writes an audit row, skipping any that also commits after it."""
        for block, i, st, ancestors in self._statements():
            if isinstance(st, _COMPOUND):
                heads = [getattr(st, "test", None), getattr(st, "iter", None), getattr(st, "subject", None)]
                heads += [item.context_expr for item in getattr(st, "items", None) or []]
                calls = [c for h in heads if h is not None for c in _own_calls(h)]
            else:
                calls = _own_calls(st)
            audits = [c for c in calls if self.is_audit(c)]
            if not audits:
                continue
            last = audits[-1]
            if any(self.is_commit(c) and (c.lineno, c.col_offset) > (last.lineno, last.col_offset) for c in calls):
                continue
            yield block, i, st, ancestors, last

    def leaves_a_row_pending(self) -> bool:
        return any(not self._lands_later(b, i, anc) for b, i, _st, anc, _last in self._audit_statements())

    def findings(self) -> list[Finding]:
        out: list[Finding] = []
        is_route = any(
            isinstance(t := (d.func if isinstance(d, ast.Call) else d), ast.Attribute) and t.attr in ROUTE_DECORATORS
            for d in self.fn.decorator_list
        )
        for block, i, _st, ancestors, last in self._audit_statements():
            if self._lands_later(block, i, ancestors):
                continue
            committed_before = any(self.is_commit(c) and c.lineno < last.lineno for c in _own_calls(self.fn))
            if committed_before:
                out.append(Finding(self.rel, last.lineno, self.fn.name, "after-commit"))
            elif is_route:
                out.append(Finding(self.rel, last.lineno, self.fn.name, "no-commit"))
        return out


def scan(root: Path = PACKAGE_ROOT) -> list[Finding]:
    """Every audit write no commit follows, allowed or not, in source order."""
    index = _Index(root)
    found: list[Finding] = []
    for rel, tree in index.trees.items():
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name not in AUDIT_WRITERS:
                found.extend(_FunctionScan(index, rel, fn).findings())
    return sorted(set(found), key=lambda f: (f.path, f.line))


def unallowed(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if (f.path, f.function) not in ALLOWED]


def stale_allowances(findings: list[Finding]) -> list[tuple[str, str]]:
    hit = {(f.path, f.function) for f in findings}
    return sorted(key for key in ALLOWED if key not in hit)


def main(argv: list[str] | None = None) -> int:
    findings = scan()
    bad, stale = unallowed(findings), stale_allowances(findings)
    for f in bad:
        print(f)
    for path, function in stale:
        print(f"STALE ALLOWED entry — matches nothing now, remove it: {path}::{function}")
    print(
        f"{len(findings)} audit write(s) with no commit after them; "
        f"{len(findings) - len(bad)} allowed, {len(bad)} not; {len(stale)} stale allowance(s)",
        file=sys.stderr,
    )
    return 1 if bad or stale else 0


if __name__ == "__main__":
    sys.exit(main())
