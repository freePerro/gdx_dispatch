"""Guard: every table named in a raw SQL string must be a real ORM table.

The 2026-07-15 full-app walk found three endpoints whose raw ``text("... FROM
<table> ...")`` referenced tables that don't exist — ``timeclock_entries``
(→ ``timeclocks``), ``parts_needed`` (→ ``job_parts_needed``), and
``recurring_jobs`` (→ ``recurring_job_schedules``). Each was wrapped in a
try/except that swallowed the ``UndefinedTable`` and returned an empty/zero
fallback, so the bug was invisible: the tech's mobile day-summary silently
reported 0 hours and 0 parts, and every customer showed "No recurring jobs".

Raw SQL bypasses the ORM, so a typo'd table name compiles fine and only
fails at query time — exactly the blind spot unit tests on SQLite miss when
the query is defensively caught. This static scan closes the worst of it: it
reads the package sources for ``FROM``/``JOIN`` targets inside
``sqlalchemy.text(...)`` literals and asserts each resolves to a table
registered on ``TenantBase.metadata``.

**2026-09-27 — why this file was rewritten (GDXA-173).** Until then the
literals were pulled out with a regex whose alternation tried ``"([^"]*)"``
before the triple-quote branch, so a triple-quoted ``text(...)`` matched the
empty string between the first two quote characters. It returned **72 blank
literals out of 171** across 23 router files — 42% of the SQL it existed to
police — and the ``assert len(...) > 5`` sanity check passed happily on
those blanks. It could not see ``performance.py``'s ``FROM
timeclock_entries`` (GDXA-172: a live production defect, hours always 0), so
the guard was green for precisely the class of bug it was written to catch.
It also had no left word boundary, so ``_?text\\(`` matched *inside*
``_export_context(``, ``splitext(`` and ``decrypt_if_ciphertext(``, reading
their strings as SQL and inflating the count with non-SQL.

So extraction is now done with ``ast`` instead of a regex: the parser cannot
be confused by a quote style, it merges adjacent string literals for us, and
it yields the resolved callee so a name that merely ends in "text(" is not
mistaken for SQL.

Aliases are resolved per file from the import statements. Be accurate about
what that buys: in the scanned directories the bound names are ``text`` (51
files), ``_text`` (19), ``_sa_text`` and ``_sql_text`` (1 each) — all of
which the old boundary-less regex did match. The aliases it could NOT have
matched (``_t``, ``_sql``, ``sa_text``) appear only under ``tests/``, which
this scan excludes, so alias resolution closes **zero** live sites today. It
is here as future-proofing and to get the callee name exactly right, not
because it fixed a measured gap.

**Scope, honestly.** This is a NAME-EXISTENCE smoke check, not a semantic
validator. Known, measured boundaries — each one is a place this guard
cannot fail, so read them before citing it as evidence:

* Only ``FROM`` / ``JOIN`` targets are checked. ``INSERT INTO`` and
  ``UPDATE`` targets are deliberately NOT: upsert and DDL syntax
  (``DO UPDATE SET``, ``BEFORE UPDATE ON audit_logs``) yields keyword
  false positives that would need a SQL-keyword denylist to suppress.
  Measured 2026-09-27 over the scan dirs: adding them found 2 false
  positives (``set``, ``on``) and 0 real tables.
* A table named only in a comma-join's second position with an alias
  (``FROM a x, b y``) is missed — ``_TABLE_RX`` stops at the alias.
* Columns are not checked, nor is the table the *right* one: a real-but-empty
  table passes (the timeclocks/time_entries case in the same walk).
* SQL assembled into a local variable before the call (``text(sql)``) is not
  statically readable. 24 of 391 call sites, measured 2026-09-27.
* Of the 367 literals it CAN read, only 224 name a table at all — the rest
  are ``INSERT``/``UPDATE``/DDL/``SELECT 1``, read but policed of nothing.
  Both numbers have floors asserted below, so neither can rot silently.

Column/target correctness still needs a behavioral test with seeded data.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

# Import the full model registry so metadata is populated.
import gdx_dispatch.models  # noqa: F401  (side-effect registration)
from gdx_dispatch.core.audit import TenantBase

PACKAGE_DIR = Path(__file__).resolve().parents[1]
ROUTERS_DIR = PACKAGE_DIR / "routers"

# Directories scanned. Widened beyond routers/ on 2026-09-27: raw SQL in
# core/ and modules/ was outside the guard entirely, and the boundary was
# undocumented. Deliberately excluded, with reasons:
#   tests/       — fixtures create their own throwaway DDL, and some name a
#                  missing table ON PURPOSE (test_contained_read.py:41 is
#                  `text("SELECT 1 FROM table_that_does_not_exist")`). This
#                  exclusion is load-bearing, not tidiness.
#   migrations/  — each references the schema as it stood at that revision;
#                  a table dropped since is correct there, not a defect.
#   models/      — ORM definitions; raw SQL there is DDL/reconcile plumbing.
#   plugin_host/ — separate container with its own database and tables,
#                  none of them on TenantBase.
#
# The floors are per directory ON PURPOSE. ``Path.rglob`` on a directory that
# does not exist raises nothing and yields nothing, so a renamed or moved
# package would silently empty part of the scan while a single whole-tree
# count stayed comfortably above its floor: dropping ``modules`` (92 sites)
# leaves 299, which sails past any total-based floor. That is the same
# "cannot fail for its own defect" shape this file was rewritten to remove.
# Counts measured 2026-09-27; floors set well below to absorb churn.
# ``services`` genuinely holds no raw SQL today — it is scanned so that new
# SQL there is covered, and its floor is 0 to say so out loud.
_SCAN_DIR_FLOORS = {
    "routers": 120,   # 192 today
    "services": 0,    # 0 today — scanned for future SQL, nothing to police yet
    "core": 60,       # 95 today
    "tasks": 6,       # 12 today
    "modules": 55,    # 92 today
}
_SCAN_DIRS = tuple(_SCAN_DIR_FLOORS)

# Tables that live outside TenantBase metadata but genuinely EXIST.
# Each entry cites the code that creates it — "I checked the DB once" rots,
# a file:line does not.
_KNOWN_EXTERNAL: set[str] = {
    "tenants",          # control plane (ControlBase)
    "plugin_registry",  # plugin-host managed (raw DDL, not ORM)
    "plugin_artifact",  # plugin-host managed (raw DDL, not ORM)
    "tenant_settings",  # raw-DDL table (exists in DB, not ORM), session_policy
    # Added 2026-09-27 (GDXA-173) when the scan widened past routers/ and the
    # blank-literal bug was fixed. All three are created by raw DDL, so they
    # exist in every database but will never appear in TenantBase.metadata.
    "qb_accounts",      # core/quickbooks.py:652, modules/quickbooks/sync.py
    "server_errors",    # migrations/versions/001_squashed_baseline.py
    "plugin_consent",   # core/plugin_consent.py:151
}
# NOT auto-discovered by grepping the tree for ``CREATE TABLE <name>``, which
# was considered and rejected: a typo'd table name that happened to appear in
# unrelated DDL would whitelist itself, and a guard that can silently go green
# is the exact failure this file was rewritten to remove.

# Stands in for an f-string interpolation or a non-literal operand. A table
# name containing it is skipped rather than reported: ``FROM {tbl}`` is
# unknowable statically, and ``FROM job{suffix}`` must not be read as "job".
_EXPR = "\x00"

# Matches the table(s) after FROM / JOIN: the first name, plus any
# comma-joined follow-ons (FROM a, b). Schema qualifier stripped in the
# scan. information_schema/pg_catalog system views are skipped there.
_NAME = r"[a-zA-Z_][\w.\x00]*"
_TABLE_RX = re.compile(
    rf"\b(?:FROM|JOIN)\s+({_NAME}(?:\s*,\s*{_NAME})*)", re.IGNORECASE
)
# SQL comments are stripped before the table scan: `-- ... the technicians
# join below` in tech_efficiency.py otherwise reports a table named "below".
_SQL_COMMENT_RX = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
# CTE names are local to their statement, not tables (tech_efficiency.py's
# `closed_in_window` and `lead_for_job`).
_CTE_RX = re.compile(
    r"(?:\bWITH\s+(?:RECURSIVE\s+)?|,\s*)([a-zA-Z_]\w*)\s+AS\s*\(", re.IGNORECASE
)
# `EXTRACT(DOW FROM created_at)` and friends use FROM as an argument
# separator, not a table clause (core/ai_router.py:682).
_FROM_AS_SEPARATOR_RX = re.compile(
    r"\b(?:EXTRACT|SUBSTRING|TRIM|OVERLAY|POSITION)\s*\(\s*[^()]*?\bFROM\b",
    re.IGNORECASE,
)


def _sqlalchemy_text_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Return (direct names, module aliases) bound to ``sqlalchemy.text`` here.

    Resolved per file from the import statements rather than matched by name,
    so ``from sqlalchemy import text as _t`` is found without this file
    carrying a list of the aliases in fashion. ``ast.walk`` reaches imports
    nested inside a function, which is how several routers do it.
    """
    direct: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "sqlalchemy":
                for alias in node.names:
                    if alias.name == "text":
                        direct.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "sqlalchemy":
                    modules.add(alias.asname or alias.name.split(".")[0])
    return direct, modules


def _is_text_call(call: ast.Call, direct: set[str], modules: set[str]) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in direct
    if isinstance(func, ast.Attribute) and func.attr == "text":
        return isinstance(func.value, ast.Name) and func.value.id in modules
    return False


def _static_sql(node: ast.expr) -> str | None:
    """The SQL of a literal argument, or None when it isn't statically known.

    Handles every quoting style (the parser has already resolved those, and
    already merged implicitly concatenated literals into one Constant),
    f-strings, and ``+`` chains of literals. Interpolated expressions become
    ``_EXPR`` so the surrounding SQL stays readable.
    """
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
            else _EXPR
            for part in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_sql(node.left), _static_sql(node.right)
        if left is None and right is None:
            return None
        return (left or _EXPR) + (right or _EXPR)
    return None


def _text_calls_in_source(src: str, label: str) -> list[tuple[str, int, str | None]]:
    """(label, lineno, sql-or-None) for every ``text(...)`` call in one source."""
    tree = ast.parse(src, filename=label)
    direct, modules = _sqlalchemy_text_names(tree)
    if not direct and not modules:
        return []
    out: list[tuple[str, int, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args and _is_text_call(node, direct, modules):
            out.append((label, node.lineno, _static_sql(node.args[0])))
    return out


def _scan_files():
    for name in _SCAN_DIRS:
        yield from sorted((PACKAGE_DIR / name).rglob("*.py"))


def _text_call_sites() -> list[tuple[str, int, str | None]]:
    """Every ``sqlalchemy.text(...)`` call site in the scanned tree.

    Sites are labelled with the path relative to the package, never the
    basename: 22 basenames repeat across the scanned dirs (20 ``router.py``,
    25 ``service.py``), and ``performance.py`` exists in BOTH ``core/`` and
    ``routers/`` — so a bare basename cannot locate the offending query.
    """
    sites: list[tuple[str, int, str | None]] = []
    for path in _scan_files():
        label = str(path.relative_to(PACKAGE_DIR))
        sites.extend(_text_calls_in_source(path.read_text(errors="replace"), label))
    return sites


def _iter_text_sql():
    """(file, sql) for every call site whose SQL is statically readable."""
    for fname, _lineno, sql in _text_call_sites():
        if sql is not None:
            yield fname, sql


def _tables_in_sql(sql: str) -> set[str]:
    """The FROM/JOIN table names in one SQL string."""
    body = _SQL_COMMENT_RX.sub(" ", sql)
    body = _FROM_AS_SEPARATOR_RX.sub(lambda m: m.group(0)[:-4], body)
    local = {m.group(1).lower() for m in _CTE_RX.finditer(body)}
    found: set[str] = set()
    for tm in _TABLE_RX.finditer(body):
        # tm.group(1) may be "a, b" (comma join) and each may be
        # schema-qualified (public.x) — split and take the last segment.
        for raw in tm.group(1).split(","):
            raw = raw.strip().lower()
            # Skip system catalogs by their FULL (possibly schema-qualified)
            # name before stripping to the last segment.
            if raw.startswith(("information_schema", "pg_")) or _EXPR in raw:
                continue
            name = raw.split(".")[-1]
            if name and name not in local:
                found.add(name)
    return found


def _referenced_tables():
    tables: dict[str, str] = {}  # table -> "file:line: sql-snippet"
    for fname, lineno, sql in _text_call_sites():
        if sql is None:
            continue
        for name in _tables_in_sql(sql):
            snippet = " ".join(sql.split())[:90]
            tables.setdefault(name, f"{fname}:{lineno}: {snippet}...")
    return tables


def test_raw_sql_from_join_tables_are_real_orm_tables():
    real = set(TenantBase.metadata.tables.keys()) | _KNOWN_EXTERNAL
    referenced = _referenced_tables()
    unknown = {t: where for t, where in referenced.items() if t not in real}
    assert not unknown, (
        "raw SQL references table(s) not registered on TenantBase.metadata "
        "(a typo here fails only at query time and is often swallowed by a "
        "try/except → silent wrong data):\n"
        + "\n".join(f"  {t!r} — {where}" for t, where in sorted(unknown.items()))
        + "\n\nFix the query. Do NOT add the name to _KNOWN_EXTERNAL unless "
        "you can cite the file:line of the DDL that creates it — that list is "
        "for tables which exist outside the ORM, not for tables which do not "
        "exist."
    )


# The source below is the regression net for GDXA-173: it names, in one
# place, every literal shape the extractor must see. The four triple-quoted
# forms are the ones the old regex returned "" for.
_EXTRACTOR_SAMPLE = '''
from sqlalchemy import text
from sqlalchemy import text as _t
import sqlalchemy as sa


def queries(db, tid):
    db.execute(text("SELECT 1 FROM alpha"))
    db.execute(text('SELECT 1 FROM bravo'))
    db.execute(text("""SELECT 1 FROM charlie"""))
    db.execute(text(f"""SELECT 1 FROM delta WHERE id = {tid}"""))
    db.execute(_t("""SELECT 1 FROM echo"""))
    db.execute(sa.text("SELECT 1 FROM foxtrot"))
    db.execute(text("SELECT 1 " "FROM golf"))
    db.execute(text("SELECT 1 FROM hotel " + " WHERE x = 1"))
    db.execute(text(\'\'\'SELECT 1 FROM india\'\'\'))
    db.execute(text(f"SELECT 1 FROM {tid}_interpolated"))
    db.execute(text(build_sql_elsewhere()))
    send_text("a note from Bob")
    _export_context("nothing from here either")
'''


def test_extractor_sees_every_literal_shape():
    """Each quoting style must yield its SQL — none may come back blank.

    This is what the pre-2026-09-27 guard could not do: ``charlie``,
    ``delta``, ``echo`` and ``india`` were all invisible, because the regex
    matched the empty string out of the opening triple quote.
    """
    sites = _text_calls_in_source(_EXTRACTOR_SAMPLE, "<sample>")
    readable = [sql for _f, _l, sql in sites if sql is not None]
    assert not [s for s in readable if not s.strip()], (
        "the extractor returned a BLANK literal — this is the GDXA-173 bug, "
        "where 72 of 171 sites came back as the empty string and the guard "
        "went green on them"
    )

    tables: set[str] = set()
    for sql in readable:
        tables |= _tables_in_sql(sql)
    assert tables == {
        "alpha",      # double-quoted
        "bravo",      # single-quoted
        "charlie",    # triple-double-quoted
        "delta",      # triple-double-quoted f-string with an interpolation
        "echo",       # import alias (_t) + triple quotes
        "foxtrot",    # sa.text(...) module-attribute form
        "golf",       # implicitly concatenated adjacent literals
        "hotel",      # literal + literal
        "india",      # triple-single-quoted
    }, f"extractor saw {sorted(tables)}"

    # A wholly interpolated name is unknowable, not a table called "job".
    assert "_interpolated" not in tables
    # text(f(...)) is out of reach, and must be counted as such, not silently
    # treated as "no SQL here".
    assert [s for _f, _l, s in sites if s is None], "dynamic call site not reported"
    # Only real sqlalchemy.text aliases count: send_text/_export_context both
    # end in "text(" and the old regex read their strings as SQL.
    assert "bob" not in tables


def test_scan_sees_the_sql_it_claims_to_police():
    """The sanity check that used to be ``assert len(...) > 5``.

    That passed on 171 blank literals, so it could not fail for the defect
    that had disabled the guard above. These can: a blank literal, a
    collapsed call-site count, or a slide toward unreadable SQL each redden.
    """
    sites = _text_call_sites()
    readable = [(f, ln, sql) for f, ln, sql in sites if sql is not None]

    blank = [(f, ln) for f, ln, sql in readable if not sql.strip()]
    assert not blank, f"blank SQL literal(s) — extractor is broken: {blank[:10]}"

    # Every scanned directory must still exist and still yield its SQL. A
    # missing directory is the silent-empty case rglob cannot report.
    for name, floor in _SCAN_DIR_FLOORS.items():
        assert (PACKAGE_DIR / name).is_dir(), f"scan dir {name}/ is gone"
        found = sum(1 for f, _ln, _sql in sites if f.split("/")[0] == name)
        assert found >= floor, (
            f"{name}/ yielded {found} text() call sites, floor is {floor} — "
            "the scan has stopped seeing a directory it claims to cover"
        )

    # 391 call sites at the 2026-09-27 rewrite, 367 of them statically
    # readable. This ratio measures PARSEABILITY, not coverage: 137 of those
    # readable literals contain no FROM/JOIN at all (INSERT/UPDATE/DDL/
    # `SELECT 1`), so they are read but police nothing.
    assert len(readable) / len(sites) > 0.85, (
        f"only {len(readable)}/{len(sites)} text() literals are readable"
    )
    # The honest coverage number: sites that actually yield a table name this
    # guard then checks. 224 of 391 (57%) at the rewrite.
    policed = [(f, ln) for f, ln, sql in readable if _tables_in_sql(sql)]
    assert len(policed) > 150, (
        f"only {len(policed)} call sites yield a checked table name "
        f"(was 224 of {len(sites)} on 2026-09-27)"
    )
    assert _referenced_tables(), "no FROM/JOIN tables parsed — regex broken"
