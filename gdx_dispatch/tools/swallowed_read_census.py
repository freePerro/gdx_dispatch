#!/usr/bin/env python3
"""Census of session operations swallowed inside a ``try`` on a caller-owned Session.

THE CLASS BEING MEASURED
------------------------
A helper takes a ``Session`` it does not own, operates on it inside a ``try``,
and its ``except`` returns a degraded value instead of raising. On Postgres a
failed statement aborts the whole transaction, so the caller's next
``commit()`` dies with ``InFailedSqlTransaction`` (25P02) and the work the
helper was protecting is LOST. ``gdx_dispatch.core.database.contained_read``
is the cure (``a78db59b``, PR #807); this tool is the instrument that sizes the
class and — the point — lets the number go DOWN as the cure lands.

WHY THIS FILE EXISTS
--------------------
The census existed only as prose in PR #740's commit message (``5a758c80``) and
was retyped from that paragraph at least three times. Two defects in the prose
recipe are corrected here, and a third that the recipe never specified at all:

(a) **Containment is per-call and may sit INSIDE the ``try``.** The landed idiom
    puts the savepoint inside the ``try`` (rule 1 of ``contained_read``'s own
    docstring: outside it, the ``except`` never runs and a degraded read becomes
    a 500). The prose recipe only accepted a savepoint as an ANCESTOR of the
    ``try``, so it counted every landed fix as still broken and the ledger read
    73 forever. Measured 2026-09-27 on ``efb031a2`` under the recorded 7-method
    set: 35 call sites across 9 files were reported broken while contained — six
    files' worth of merged fixes, plus hand-written ``begin_nested`` savepoints
    that predate ``contained_read`` (24 calls in ``modules/quickbooks/sync.py``
    #760, 2 in ``core/job_photos.py`` #482).

(b) **``get`` belongs in the method set.** The prose set was
    ``execute/flush/commit/add/add_all/merge/delete``. Without ``get``,
    ``core/payments.py``'s ``card_surcharge_rate`` — a read on the public,
    unauthenticated pay page — is invisible, which is why it is absent from the
    recorded 73-row table.

(c) **The counting unit was never stated.** Per-``try`` hides the second and
    third unprotected call in the same ``try``, and those are separate fixes.
    This tool counts ONE FINDING PER SESSION CALL and prints all three
    denominators (calls / sites / tries) in its own header, so a number lifted
    out of it cannot lose its unit.

ANCESTOR SAVEPOINTS ARE DELIBERATELY NOT CONTAINMENT
----------------------------------------------------
A savepoint wrapping the whole ``try`` is not a fix and is reported. Such a
block exits CLEANLY (the ``except`` ate the error), so the context manager
issues ``RELEASE SAVEPOINT`` on an already-aborted transaction, which raises
25P02 out of the ``with`` itself while the caller's ``commit()`` stays dead —
strictly worse than not wrapping. Pinned on real Postgres by
``tests/test_contained_read.py::test_pg_a_callee_that_swallows_its_own_failure_is_not_contained``.
The prose recipe excluded these sites; this tool keeps them.

THE NUMBER IS A LOWER BOUND
---------------------------
Named blind spots, each of which can hide a real instance:

* a session reached through an attribute (``self.db``) — not seen;
* a session parameter under any name outside `SESSION_PARAM_NAMES` — not seen;
* a read one frame down in a helper (``mobile_chat.send_job_chat`` is the
  recorded example) — not seen, the call in the ``try`` is not on the session;
* a session captured by closure instead of taken as a parameter — not seen;
* exception TYPES are not analysed. A narrow ``except (ValueError, TypeError)``
  counts as swallowing, because the DB error propagates past it to whatever
  swallows next (``modules/workflows/engine.py:90`` was confirmed real by
  exactly this route, GDXA-152); and a handler that re-raises conditionally
  counts as raising, as does one whose only ``raise`` sits inside a nested
  ``def`` that the handler never calls. Both directions are approximations;
* containment is LEXICAL. A savepoint opened by the caller, or through an
  ``ExitStack`` held in a variable outside a ``with``, is not seen;
* a framework-injected session is excluded — see `session_params`.

THE ONE EXEMPTION THAT IS NOT A BLIND SPOT, AND IS THE BIGGEST
--------------------------------------------------------------
The recorded predicate excludes a ``try`` whose handler calls
``<session>.rollback()``, and that exclusion is **not** a safety argument.
``contained_read``'s rule 4 says a full rollback "expires every object the
caller is holding", so such a handler discards the caller's work SILENTLY
instead of failing loudly as 25P02 — a different defect, not a cure.

It is also larger than the number it hides behind: measured on ``d3bde561``,
106 further calls match in every other respect, 46 of them pure ``execute``
reads. So this tool counts them, prints the count and the per-method breakdown
in its own header, and takes ``--include-rollback-handlers`` to fold them in.
The count is computed on every run and never written down here, because a
hand-maintained number is the disease this file was written to cure.

A lane can therefore close a census finding the cheap way — add
``db.rollback()`` to the handler — and the headline number falls while the
caller's identity map is expired mid-request. The header is what makes that
visible, so do not quote the headline without it.

A file that fails to parse is NAMED in the header, never silently counted as
zero. That is the input that turns this instrument red without changing a
single finding.

DELIBERATE DEVIATION FROM THE RECORDED PREDICATE
------------------------------------------------
The prose recipe excluded a session parameter defaulted to ``Depends(...)`` but
not the equivalent ``Annotated[Session, Depends(get_db)]`` form, which is the
same framework-injected ownership. All three spellings are excluded here — the
default, the inline ``Annotated``, and a module-level alias for it
(``TenantDB = Annotated[Session, Depends(get_db)]``, live in ``core/gdpr.py``
and ``core/ai_quote.py``; without alias resolution the next endpoint written
that way is a false positive indistinguishable from a real defect). Measured
cost on ``efb031a2``: it drops ``api/public_router.py::list_public_listings``,
which IS in the recorded 73-row table — the one recorded site this tool refuses
on purpose rather than by accident.

USAGE
-----
    python -m gdx_dispatch.tools.swallowed_read_census
    python -m gdx_dispatch.tools.swallowed_read_census --root /tmp/archive-of-a-ref
    python -m gdx_dispatch.tools.swallowed_read_census --json
    python -m gdx_dispatch.tools.swallowed_read_census --extended-methods

There is NO frozen baseline and this is not a ratchet: nine domain lanes are
editing these files concurrently and any frozen count would be stale before it
merged. The guard for this instrument is ``tests/test_swallowed_read_census.py``.
Machine output is keyed on ``file::qualname::method`` and never on a line
number, because a line-keyed artifact reddens when a line is added above a
finding (``.tenant_plane_redundant_filter_baseline``, a documented sharp edge in
``CLAUDE.md``). Lines are printed for humans and carried inside each record.

EXIT CODES
    0 — scan completed (the default; a census is a measurement, not a gate)
    1 — findings present, and only when ``--fail-on-findings`` was passed
    2 — usage / setup error
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# The eight names the recorded predicate recognises as a caller-owned session.
SESSION_PARAM_NAMES = frozenset(
    {"db", "session", "tenant_db", "sess", "s", "tdb", "control_db", "conn"}
)

# The recorded method set plus `get` — correction (b).
BASE_METHODS = frozenset(
    {"execute", "flush", "commit", "add", "add_all", "merge", "delete", "get"}
)

# Evaluated separately so the headline number stays comparable with the
# recorded 73. Reported as a delta, never folded into the base count.
EXTENDED_METHODS = frozenset({"scalar", "scalars", "refresh", "query", "get_one"})

# `contained_read(db)` is the cure; `db.begin_nested()` is the hand-written
# savepoint that predates it.
CONTAINMENT_HELPERS = frozenset({"contained_read"})
CONTAINMENT_METHODS = frozenset({"begin_nested"})

PACKAGE_DIR = "gdx_dispatch"

# `tests/` wrap on purpose; `migrations/` hold no caller-owned ORM session (a
# migration's `conn` comes from `op.get_bind()`, never from a parameter).
# Excluding exactly these three reproduces the 596-file scan surface the
# recorded 73 was measured over.
SKIP_DIR_NAMES = frozenset(
    {
        "tests",
        "migrations",
        "frontend",
        "node_modules",
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "build",
        "dist",
    }
)

BLIND_SPOTS = (
    "a session reached through an attribute (self.db)",
    f"a session parameter named outside {sorted(SESSION_PARAM_NAMES)}",
    "a read one frame down in a helper (mobile_chat.send_job_chat)",
    "a session captured by closure rather than taken as a parameter",
    "exception types are not analysed (a narrow handler counts as swallowing, "
    "and any `raise` in a handler excuses it — even one inside a nested def)",
    "containment is lexical (a caller-opened savepoint is not seen)",
    "a framework-injected session (Depends) is excluded by design",
)


def detect_repo_root(start: Path | None = None) -> Path:
    """The repo root, by looking for ``gdx_dispatch/tools/``.

    Deliberately not the root finder ``silent_failure_scanner`` used until
    GDXA-404, which also required an ``ai-queue/`` directory — absent from a
    fresh clone and from the ``git archive`` extract this tool is meant to be
    pointed at.
    """
    here = (start or Path(__file__)).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / PACKAGE_DIR / "tools").is_dir():
            return candidate
    return Path.cwd()


@dataclass(frozen=True)
class Finding:
    """One swallowed session CALL. The unit is the call, not the ``try``."""

    file: str
    qualname: str
    method: str
    session: str
    call_line: int
    try_line: int
    extended: bool

    @property
    def key(self) -> str:
        """Stable machine key: survives every line moving."""
        return f"{self.file}::{self.qualname}::{self.method}"


# ── predicate parts ────────────────────────────────────────────────────────


def _is_depends_call(node: ast.AST | None) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = getattr(func, "id", None) or getattr(func, "attr", None)
    return name == "Depends"


def _annotation_injects(
    annotation: ast.AST | None, aliases: frozenset[str] = frozenset()
) -> bool:
    """True for ``Annotated[Session, Depends(get_db)]`` — framework-injected.

    ``aliases`` carries the module's own names for that annotation, because
    ``TenantDB = Annotated[Session, Depends(get_db)]`` (``core/gdpr.py:22``,
    ``core/ai_quote.py:781``) hides the ``Depends`` behind a Name and would
    otherwise read as a caller-owned session.
    """
    if annotation is None:
        return False
    return any(
        _is_depends_call(node)
        or (isinstance(node, ast.Name) and node.id in aliases)
        for node in ast.walk(annotation)
    )


def injected_annotation_aliases(tree: ast.Module) -> frozenset[str]:
    """Module-level names bound to an annotation that contains ``Depends(...)``."""
    aliases: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            _is_depends_call(inner) for inner in ast.walk(node.value)
        ):
            aliases.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
    return frozenset(aliases)


def session_params(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    injected_aliases: frozenset[str] = frozenset(),
) -> set[str]:
    """Parameter names that look like a session the function does not own.

    A parameter injected by FastAPI — ``db: Session = Depends(get_db)`` or
    ``db: Annotated[Session, Depends(get_db)]`` — is excluded: the framework
    owns that session and poisoning it cannot reach past the request. The
    recorded prose recipe excluded only the first form; see the module
    docstring for what excluding both costs.
    """
    args = fn.args
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = list(args.defaults)
    padding: list[ast.expr | None] = [None] * (len(positional) - len(defaults))
    # Defaults align to the TAIL of the positional list; kw_defaults are 1:1.
    pairs = list(zip(positional, padding + defaults, strict=True))
    pairs += list(zip(args.kwonlyargs, args.kw_defaults, strict=True))

    found: set[str] = set()
    for arg, default in pairs:
        if arg.arg not in SESSION_PARAM_NAMES:
            continue
        if _is_depends_call(default) or _annotation_injects(
            arg.annotation, injected_aliases
        ):
            continue
        found.add(arg.arg)
    return found


def _session_call(node: ast.AST, sessions: set[str], methods: frozenset[str]):
    """``(session, method)`` when ``node`` is ``<session>.<method>(...)``."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in methods:
        return None
    if not isinstance(func.value, ast.Name) or func.value.id not in sessions:
        return None
    return func.value.id, func.attr


RAISES, ROLLS_BACK, SWALLOWS = "raises", "rolls_back", "swallows"


def _handler_verdict(try_node: ast.Try, session: str) -> str:
    """What this ``try``'s handlers do with a failure on ``session``.

    Three outcomes, and the middle one is the exemption the recorded predicate
    carries without disclosing:

    * ``RAISES``     — a handler re-raises, so the error travels outward and is
      whatever encloses this ``try``'s problem, not this one's.
    * ``ROLLS_BACK`` — a handler calls ``<session>.rollback()``. Excluded from
      the count by the recorded predicate, and NOT because it is safe:
      ``contained_read``'s rule 4 (``core/database.py``) says a full rollback
      "expires every object the caller is holding", so it discards the caller's
      work SILENTLY instead of as a 25P02. It is a different defect, not a
      cure. Counted and reported separately rather than dropped — see
      ``Census.rollback_exempt`` and ``--include-rollback-handlers``.
    * ``SWALLOWS``   — neither. This is the class being censused.
    """
    if not try_node.handlers:
        return RAISES
    verdict = SWALLOWS
    for handler in try_node.handlers:
        for node in _walk_statements(handler.body):
            if isinstance(node, ast.Raise):
                return RAISES
            if _session_call(node, {session}, frozenset({"rollback"})):
                verdict = ROLLS_BACK
    return verdict


def _walk_statements(body: list[ast.stmt]):
    for statement in body:
        yield from ast.walk(statement)


def _is_savepoint(expr: ast.AST, session: str) -> bool:
    """``contained_read(<session>)`` or ``<session>.begin_nested()``."""
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    helper = getattr(func, "id", None) or getattr(func, "attr", None)
    if helper in CONTAINMENT_HELPERS:
        first = expr.args[0] if expr.args else None
        return isinstance(first, ast.Name) and first.id == session
    return (
        isinstance(func, ast.Attribute)
        and func.attr in CONTAINMENT_METHODS
        and isinstance(func.value, ast.Name)
        and func.value.id == session
    )


def _with_opens_savepoint(
    with_node: ast.With | ast.AsyncWith, session: str, before_line: int
) -> bool:
    """True when this ``with`` puts ``session`` inside a savepoint.

    Covers both the direct item and ``stack.enter_context(contained_read(db))``
    inside the block — the form a ``with contained_read(`` text scan misses
    (GDXA-151).

    Two conditions on the ``enter_context`` form, and both are there to stop it
    crediting a savepoint that may not exist at runtime:

    * it must appear ABOVE the call it is claimed to protect, or a later one
      would contain a read it cannot reach;
    * it must be an UNCONDITIONAL statement of this block. Only the block's own
      statement list is searched, never a nested ``if``/loop/``try``: under
      ``if guard: stack.enter_context(...)`` the read is naked whenever ``guard``
      is false, and a site that is only sometimes contained is still a hazard.
    """
    for item in with_node.items:
        if _is_savepoint(item.context_expr, session):
            return True
    for statement in with_node.body:
        if statement.lineno >= before_line:
            break
        value = getattr(statement, "value", None)
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Attribute)
            and value.func.attr == "enter_context"
            and any(_is_savepoint(arg, session) for arg in value.args)
        ):
            return True
    return False


# ── the walk ───────────────────────────────────────────────────────────────


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _own_nodes(fn: ast.FunctionDef | ast.AsyncFunctionDef):
    """Every node in ``fn``'s body, stopping at a nested function or class.

    A nested definition is visited in its own right, with its own parameters,
    so descending into it here would double-count and would attribute its
    calls to the wrong qualname.
    """
    stack: list[ast.AST] = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef),
            ):
                continue
            stack.append(child)


def _attributed_try(
    call: ast.Call,
    parents: dict[ast.AST, ast.AST],
    fn: ast.AST,
    session: str,
) -> tuple[ast.Try | None, str]:
    """The innermost enclosing ``try`` that CATCHES a failure on this call.

    Returns the ``try`` and its verdict. Walks OUTWARD only past a ``try`` that
    re-raises, because that is the only handler an exception travels through — a
    handler that swallows or rolls back consumes it, so the ``try`` outside it
    never runs and must not be credited or blamed for this call. Only a call in
    the ``try``'s BODY counts; one in a handler belongs to whatever encloses the
    handler, which also stops the same call being counted twice.
    """
    child: ast.AST = call
    current = parents.get(call)
    while current is not None:
        if isinstance(current, ast.Try) and any(child is stmt for stmt in current.body):
            verdict = _handler_verdict(current, session)
            if verdict is not RAISES:
                return current, verdict
        if current is fn:
            break
        child = current
        current = parents.get(current)
    return None, RAISES


def _is_contained(
    call: ast.Call,
    parents: dict[ast.AST, ast.AST],
    try_node: ast.Try,
    session: str,
) -> bool:
    """Correction (a): containment between the call and its ``try``, inclusive.

    Stops AT ``try_node``. A savepoint wrapping the ``try`` from outside is not
    containment — see the module docstring.
    """
    current = parents.get(call)
    while current is not None:
        if isinstance(current, (ast.With, ast.AsyncWith)) and _with_opens_savepoint(
            current, session, call.lineno
        ):
            return True
        if current is try_node:
            return False
        current = parents.get(current)
    return False


class _ModuleCensus(ast.NodeVisitor):
    def __init__(
        self,
        rel: str,
        methods: frozenset[str],
        parents: dict[ast.AST, ast.AST],
        injected_aliases: frozenset[str] = frozenset(),
    ):
        self.rel = rel
        self.methods = methods
        self.parents = parents
        self.injected_aliases = injected_aliases
        self.findings: list[Finding] = []
        # Calls that match in every respect except that a handler rolls the
        # session back. The recorded predicate drops these; this tool counts
        # them so the exemption travels with the number instead of hiding in it.
        self.rollback_exempt: list[Finding] = []
        self._scope: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope.append(node.name)
        sessions = session_params(node, self.injected_aliases)
        if sessions:
            self._collect(node, sessions, ".".join(self._scope))
        self.generic_visit(node)
        self._scope.pop()

    def _collect(
        self,
        fn: ast.FunctionDef | ast.AsyncFunctionDef,
        sessions: set[str],
        qualname: str,
    ) -> None:
        for node in _own_nodes(fn):
            hit = _session_call(node, sessions, self.methods)
            if hit is None:
                continue
            session, method = hit
            call = node
            assert isinstance(call, ast.Call)
            try_node, verdict = _attributed_try(call, self.parents, fn, session)
            if try_node is None:
                continue
            if _is_contained(call, self.parents, try_node, session):
                continue
            finding = Finding(
                file=self.rel,
                qualname=qualname,
                method=method,
                session=session,
                call_line=call.lineno,
                try_line=try_node.lineno,
                extended=method in EXTENDED_METHODS,
            )
            if verdict is ROLLS_BACK:
                self.rollback_exempt.append(finding)
            else:
                self.findings.append(finding)


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.call_line, f.qualname, f.method))


def scan_source(
    src: str, rel: str, methods: frozenset[str] = BASE_METHODS
) -> list[Finding]:
    """Census one module's source. Raises ``SyntaxError`` on unparseable input."""
    return scan_source_with_exempt(src, rel, methods)[0]


def scan_source_with_exempt(
    src: str, rel: str, methods: frozenset[str] = BASE_METHODS
) -> tuple[list[Finding], list[Finding]]:
    """``(findings, rollback_exempt)`` — the count and the exemption beside it."""
    tree = ast.parse(src)
    census = _ModuleCensus(
        rel, methods, _parent_map(tree), injected_annotation_aliases(tree)
    )
    census.visit(tree)
    return _sorted(census.findings), _sorted(census.rollback_exempt)


# ── tree scan ──────────────────────────────────────────────────────────────


def iter_python_files(root: Path) -> list[Path]:
    """Every candidate ``.py`` under ``root``, from the FILESYSTEM.

    Not from the git index, deliberately: a call site in a brand-new module
    reads as zero under a tracked-only scan until someone runs ``git add``
    (measured in the GDXA-151 audit).
    """
    base = root / PACKAGE_DIR if (root / PACKAGE_DIR).is_dir() else root
    files = [
        path
        for path in base.rglob("*.py")
        if not SKIP_DIR_NAMES & set(path.relative_to(base).parts)
    ]
    return sorted(files)


@dataclass
class Census:
    root: Path
    methods: frozenset[str]
    findings: list[Finding]
    files_scanned: int
    unparsed: list[str]
    rollback_exempt: list[Finding]

    @property
    def calls(self) -> int:
        return len(self.findings)

    @property
    def unique_keys(self) -> int:
        """Distinct ``file::qualname::method``. NOT a subset of ``tries``: one
        function with two swallowing tries on the same method is one key and two
        tries, which is why every denominator is printed with its own name."""
        return len({f.key for f in self.findings})

    @property
    def tries(self) -> int:
        return len({(f.file, f.try_line) for f in self.findings})

    @property
    def files(self) -> int:
        return len({f.file for f in self.findings})

    def by_file(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.file] = counts.get(finding.file, 0) + 1
        return counts

    def keys(self) -> set[str]:
        return {f.key for f in self.findings}


def scan_tree(
    root: Path,
    methods: frozenset[str] = BASE_METHODS,
    include_rollback_handlers: bool = False,
) -> Census:
    findings: list[Finding] = []
    exempt: list[Finding] = []
    unparsed: list[str] = []
    paths = iter_python_files(root)
    for path in paths:
        rel = path.relative_to(root).as_posix()
        try:
            src = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unparsed.append(f"{rel} ({type(exc).__name__})")
            continue
        try:
            hits, rolled_back = scan_source_with_exempt(src, rel, methods)
        except SyntaxError as exc:
            unparsed.append(f"{rel} (SyntaxError line {exc.lineno})")
            continue
        findings.extend(hits)
        exempt.extend(rolled_back)
    if include_rollback_handlers:
        findings.extend(exempt)
    findings.sort(key=lambda f: (f.file, f.call_line))
    exempt.sort(key=lambda f: (f.file, f.call_line))
    return Census(
        root=root,
        methods=methods,
        findings=findings,
        files_scanned=len(paths),
        unparsed=unparsed,
        rollback_exempt=exempt,
    )


# ── reporting ──────────────────────────────────────────────────────────────


def _records(census: Census) -> list[dict]:
    grouped: dict[str, dict] = {}
    for finding in census.findings:
        record = grouped.setdefault(
            finding.key,
            {
                "key": finding.key,
                "file": finding.file,
                "qualname": finding.qualname,
                "method": finding.method,
                "session": finding.session,
                "extended": finding.extended,
                "calls": 0,
                "call_lines": [],
                "try_lines": [],
            },
        )
        record["calls"] += 1
        record["call_lines"].append(finding.call_line)
        if finding.try_line not in record["try_lines"]:
            record["try_lines"].append(finding.try_line)
    return [grouped[key] for key in sorted(grouped)]


def to_json(census: Census) -> dict:
    extended_calls = sum(1 for f in census.findings if f.extended)
    return {
        "tool": "swallowed_read_census",
        "root": str(census.root),
        "unit": "one finding per session CALL (not per try)",
        "machine_key": "file::qualname::method",
        "lower_bound": True,
        "blind_spots": list(BLIND_SPOTS),
        "methods": sorted(census.methods),
        "extended_methods_included": bool(census.methods & EXTENDED_METHODS),
        "files_scanned": census.files_scanned,
        "unparsed_files": census.unparsed,
        "totals": {
            "calls": census.calls,
            "unique_keys": census.unique_keys,
            "tries": census.tries,
            "files": census.files,
            "base_method_calls": census.calls - extended_calls,
            "extended_method_calls": extended_calls,
        },
        "rollback_handler_exemption": {
            "calls": len(census.rollback_exempt),
            "why_excluded": (
                "the recorded predicate excludes a handler containing "
                "<session>.rollback()"
            ),
            "why_that_is_not_safe": (
                "contained_read rule 4 (core/database.py): a full rollback "
                "expires every object the caller is holding, so it discards the "
                "caller's work silently instead of as 25P02 — a different "
                "defect, not a cure"
            ),
            "included_in_totals": any(
                f in census.findings for f in census.rollback_exempt
            ),
            "by_method": {
                method: sum(1 for f in census.rollback_exempt if f.method == method)
                for method in sorted({f.method for f in census.rollback_exempt})
            },
            "keys": sorted({f.key for f in census.rollback_exempt}),
        },
        "by_file": dict(
            sorted(census.by_file().items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "sites_detail": _records(census),
    }


def render(census: Census) -> str:
    extended_calls = sum(1 for f in census.findings if f.extended)
    lines = [
        f"swallowed-read census — {census.root}",
        "",
        "UNIT: one finding per session CALL, not per `try`. Machine key is",
        "      file::qualname::method — never a line number.",
        f"METHODS: {', '.join(sorted(census.methods))}",
        f"FILES SCANNED: {census.files_scanned}"
        + (f"   UNPARSED: {len(census.unparsed)}" if census.unparsed else "   unparsed: 0"),
    ]
    for entry in census.unparsed:
        lines.append(f"      !! not scanned: {entry}")
    lines += [
        "",
        f"FINDINGS: {census.calls} calls / {census.unique_keys} unique "
        f"file::qualname::method keys / {census.tries} tries across "
        f"{census.files} files"
        + (
            f"   ({census.calls - extended_calls} base + {extended_calls} extended)"
            if extended_calls
            else ""
        ),
        "",
        "THIS IS A LOWER BOUND, not the size of the class. It cannot see:",
    ]
    lines += [f"  - {spot}" for spot in BLIND_SPOTS]
    exempt_by_method = {
        method: sum(1 for f in census.rollback_exempt if f.method == method)
        for method in sorted({f.method for f in census.rollback_exempt})
    }
    lines += [
        "",
        "A savepoint wrapping the whole `try` is REPORTED, not credited: that",
        "block exits clean, so RELEASE SAVEPOINT raises 25P02 out of the `with`",
        "while the caller's commit stays dead (tests/test_contained_read.py).",
    ]
    if census.rollback_exempt:
        included = any(f in census.findings for f in census.rollback_exempt)
        lines += [
            "",
            f"ROLLBACK-HANDLER EXEMPTION: {len(census.rollback_exempt)} further "
            f"calls match in every",
            "      respect except that a handler calls <session>.rollback(). The",
            "      recorded predicate excludes them and they are "
            + ("INCLUDED above." if included else "NOT counted above."),
            "      That exclusion is not a safety argument: `contained_read` rule 4",
            "      says a full rollback expires every object the caller is holding,",
            "      so it discards the caller's work SILENTLY instead of as 25P02.",
            f"      By method: {exempt_by_method or '{}'}"
            + ("" if included else "   (--include-rollback-handlers to count them)"),
        ]
    lines.append("")
    if not census.findings:
        lines.append("no findings")
        return "\n".join(lines)

    lines.append("per file (calls):")
    for path, count in sorted(census.by_file().items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {count:>3}  {path}")
    lines += ["", "sites (line printed for humans; the key is the triple):"]
    current = ""
    for record in sorted(
        _records(census), key=lambda r: (r["file"], min(r["call_lines"]))
    ):
        if record["file"] != current:
            current = record["file"]
            lines.append(f"  {current}")
        calls = ", ".join(str(n) for n in sorted(record["call_lines"]))
        tries = ", ".join(str(n) for n in sorted(record["try_lines"]))
        flag = " [extended]" if record["extended"] else ""
        lines.append(
            f"      {record['qualname']}.{record['method']}()"
            f"  call {calls}  try {tries}{flag}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Census swallowed session operations on a caller-owned Session",
    )
    parser.add_argument(
        "--root",
        default=None,
        help="tree to scan (a repo root or a `git archive <ref>` extract); "
        "default: this file's repo",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON on stdout")
    parser.add_argument(
        "--extended-methods",
        action="store_true",
        help=f"also count {sorted(EXTENDED_METHODS)}; reported as a separate delta "
        "so the base number stays comparable with the recorded census",
    )
    parser.add_argument(
        "--include-rollback-handlers",
        action="store_true",
        help="count the calls whose handler rolls the session back. The recorded "
        "predicate excludes them; contained_read rule 4 says a rollback expires "
        "every object the caller is holding, so they are a different defect "
        "rather than a cure. Reported in the header either way.",
    )
    parser.add_argument(
        "--fail-on-findings",
        action="store_true",
        help="exit 1 when findings exist (off by default: this is a census, "
        "not a ratchet)",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else detect_repo_root()
    if not root.is_dir():
        print(f"swallowed_read_census: not a directory: {root}", file=sys.stderr)
        return 2

    methods = BASE_METHODS | EXTENDED_METHODS if args.extended_methods else BASE_METHODS
    census = scan_tree(
        root, methods, include_rollback_handlers=args.include_rollback_handlers
    )

    if args.json:
        print(json.dumps(to_json(census), indent=2))
    else:
        print(render(census))

    return 1 if (args.fail_on_findings and census.findings) else 0


if __name__ == "__main__":
    sys.exit(main())
