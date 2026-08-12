#!/usr/bin/env python3
"""Comment-drift scanner — find comments the code contradicts.

Comments rot silently: the code moves, the comment stays, and the next
reader trusts a claim that stopped being true months ago. This scanner
only reports *falsifiable* claims — a comment that names a symbol, path,
module, route or parameter that can be checked against the tree. It
deliberately says nothing about prose it cannot verify.

The index it checks against is built from CODE ONLY (comments and
docstrings stripped) so a stale claim can never vouch for itself.

DETECTORS
---------

  **D1 dangling-symbol** — a comment names ``foo()`` / ``SomeClass`` /
    ``obj.attr`` that appears nowhere in any code in the repo.

  **D2 ghost-param** — a docstring's ``Args:``/``:param x:`` block
    documents a parameter the signature does not have. (Catches renamed
    and removed parameters.)

  **D3 route-drift** — a handler docstring states an HTTP method/path
    that contradicts its own router decorator.

  **X1 modpath-rot** — a dotted ``gdx_dispatch.a.b.c`` reference that no
    longer resolves to a module or a top-level symbol in one.

  **X2 filepath-rot** — a ``path/to/file.py`` reference (optionally
    ``:LINE``) that does not exist, or whose line number is past EOF.

KNOWN NON-FINDINGS
------------------
Historical narrative is not drift. "X was removed", "used to live in Y",
"port of Z" are accurate records of things that are *supposed* to be
absent, and the scanner's ``--prose`` heuristics skip the common forms.
Review anything it does report — a pointer that reads as live but
resolves to nothing is the actual bug class here.

USAGE
-----
    python3 gdx_dispatch/tools/comment_drift_scan.py                 # full scan
    python3 gdx_dispatch/tools/comment_drift_scan.py --det D1,X1     # some detectors
    python3 gdx_dispatch/tools/comment_drift_scan.py --path core/    # one section
    python3 gdx_dispatch/tools/comment_drift_scan.py --include-tests
    python3 gdx_dispatch/tools/comment_drift_scan.py --json /tmp/c.json
"""
from __future__ import annotations

import argparse
import ast
import importlib
import io
import json
import re
import subprocess
import sys
import tokenize
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

ALL_DETECTORS = ("D1", "D2", "D3", "X1", "X2")

# Phrases that mark a reference as a historical record rather than a live
# pointer. A dangling name in one of these is correct, not drift.
_HISTORICAL = re.compile(
    r"\b(was|were|used to|no longer|since[- ]removed|now deleted|deleted|removed|"
    r"retired|dropped|replaced|replaces|superseded|legacy|port of|pre-\d|"
    r"never (?:built|written|existed|shipped)|gone|gone in|gone with|"
    r"(?:don't|doesn't|do not|does not|no longer) exist|exists? (?:any more|anymore)|"
    r"surfaced (?:by|in)|this module does not|unbuilt|planned|roadmap)\b",
    re.I,
)

# Vocabulary that legitimately appears in comments but is defined outside this
# repo: third-party API entities, library/driver exception names, Python
# builtins, protocol and infrastructure concepts, vendor product names.
# Without this, D1 is ~95% noise and the detector gets ignored.
_EXTERNAL_VOCAB = set(
    """
    AccountSubTypeEnum AccountTypes CreditCardCredit DepositLineDetail DiscountLine
    ExchangeRate InvoiceLines LongTermLiabilities OverrideDeltaAmount SalesItemLine
    ShipAddr ShippingLine SubTotal SubTotalLine TxnTaxDetail TaxCodeRef
    DataError DatatypeMismatch InFailedSqlTransaction InsufficientPrivilege
    InvalidTextRepresentation MultipleResultsFound RowMapping StringDataRightTruncation
    UndefinedColumn UndefinedTable MutableDict TypeDecorators FieldInfo
    MemoryError NoneType TypeErrors UnboundLocalError UnicodeEncodeError
    ZoneInfoNotFoundError ForeignKeys BadRequest
    CamelCase TitleCase SameSite HttpOnly ContextVars CustomEvents WebAudio
    CloudEvents CrashLoopBackOff PgBouncer CloudFlare OneDrive
    WeasyPrint TaxJar SendGrid ServiceTitan FusionAuth RubyMoney PhoneCom
    PaymentIntents SetupIntents RequestBroker ParseUri
    """.split()  # noqa: SIM905 — a prose word list, not a literal sequence
)

# Third-party names that read as filenames but are libraries, not repo files.
_EXTERNAL_FILES = {"Stripe.js", "dinero.js", "test_a.py", "test_b.py"}

# English words that look like identifiers in prose.
_ENGLISH = set(
    """a an the and or but for with without this that these those from into onto to of
    in on at by as is are was were be been being has have had do does did not no yes
    it its we our us you your they their there here when where what which who why how
    all any both each few more most other some such only own same so than too very can
    will just should now then once during before after above below up down out off over
    under again further because while about against between through if else return
    returns use used uses using make makes made get gets set sets new old one two three
    first second last next prev like unlike still yet already ever never always often
    sometimes usually note notes todo fixme hack warning caution important etc vs aka
    ok okay true false none null nan inf via per may might must need needs would could
    shall let lets keep keeps kept take takes taken give gives given see called call
    calls calling run runs running add adds added remove removes removed
    """.split()  # noqa: SIM905 — a prose word list, not a literal sequence
)

_STDLIB_VOCAB_MODULES = ("builtins", "types", "zoneinfo", "decimal", "uuid", "datetime")


def _external_vocabulary() -> set[str]:
    """The static floor above, UNION what the repo can tell us itself.

    That literal list is a floor, not the source of truth. 45 of its 60
    entries turned out to be derivable — `weasyprint`, `stripe`,
    `psycopg2-binary` and `sqlalchemy` are all declared in requirements.txt,
    and `NoneType`/`MemoryError`/`UnboundLocalError` are just the standard
    library. A hand-typed list starts rotting the moment a dependency
    changes, and the failure mode is a false positive nobody reads.

    Derivation needs the packages importable — true in the docker image,
    often not on the host — so this unions rather than replaces. The scanner
    behaves the same either way, with fewer false positives where it can.
    """
    vocab = set(_EXTERNAL_VOCAB)

    for module in _STDLIB_VOCAB_MODULES:
        try:
            mod = importlib.import_module(module)
        except ImportError:
            continue
        vocab |= {n for n in dir(mod) if n and n[0].isupper() and not n.startswith("_")}

    req = REPO_ROOT / "gdx_dispatch" / "requirements.txt"
    if req.is_file():
        for raw in _read(req).split("\n"):
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            m = re.match(r"^([A-Za-z0-9._-]+)", line)
            if not m:
                continue
            dist = m.group(1).lower()
            parts = re.split(r"[-_.]", dist)
            vocab |= {
                dist,
                dist.replace("-", "").replace("_", ""),
                dist.capitalize(),
                "".join(p.capitalize() for p in parts if p),
            }
    return vocab


RE_CALL = re.compile(r"\b([a-z_][a-z0-9_]{3,})\s*\(\s*\)")
RE_CLASS = re.compile(r"\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+)\b")
RE_MODPATH = re.compile(r"\bgdx_dispatch(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b")
# The trailing (?![A-Za-z0-9]) matters: without it ".json" matches as ".js"
# and every `--json /tmp/out.json` usage line reports as a missing file.
RE_FILEPATH = re.compile(
    r"\b((?:[A-Za-z0-9_\-./]+/)?[A-Za-z0-9_\-.]+\.(?:py|vue|ts|js|sql)(?![A-Za-z0-9]))"
    r"(?::(\d+))?"
)
RE_ROUTE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[A-Za-z0-9_\-{}/.:*]+)")
RE_PARAM_GOOGLE = re.compile(r"^(\s+)([A-Za-z_][A-Za-z0-9_]*)\s*(\([^)]*\))?\s*:\s+\S")
RE_PARAM_SPHINX = re.compile(r":param\s+(?:[^:]+\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:")
RE_DECORATOR = re.compile(
    r"@\w+\.(get|post|put|patch|delete)\(\s*[\"']([^\"']*)[\"']", re.I
)


class Finding(dict):
    """A single reported drift. dict so it serialises straight to JSON."""


_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".claude", "uploads", "backups",
}


def _git_tracked(root: Path, *patterns: str) -> list[str]:
    """Tracked files, via git when available, else a filtered filesystem walk.

    The container image that runs the test suite has no git binary, so the
    walk fallback is a normal path, not an error case.
    """
    try:
        out: list[str] = []
        for pat in patterns:
            res = subprocess.run(
                ["git", "ls-files", pat],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            )
            out += [line for line in res.stdout.split("\n") if line.strip()]
        if out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass

    walked: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        walked.append(path.relative_to(root).as_posix())
    return walked


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _docstring_node(node: ast.AST) -> ast.Expr | None:
    body = getattr(node, "body", None)
    if not body:
        return None
    first = body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return first
    return None


def _strip_comments_and_docstrings(src: str) -> str:
    """Return only executable code text — the index must not read comments."""
    doc_lines: set[int] = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            ds = _docstring_node(node)
            if ds is not None:
                for ln in range(ds.lineno, (ds.end_lineno or ds.lineno) + 1):
                    doc_lines.add(ln)
    parts: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and tok.start[0] in doc_lines:
                continue
            parts.append(tok.string)
    except (tokenize.TokenError, IndentationError):
        return src
    return " ".join(parts)


class Index:
    """Everything the scanner checks claims against, built from code only."""

    def __init__(self, root: Path):
        self.root = root
        self.tracked = _git_tracked(root, "*")
        self.tracked_set = set(self.tracked)
        self.basenames: dict[str, list[str]] = defaultdict(list)
        for rel in self.tracked:
            self.basenames[rel.rsplit("/", 1)[-1]].append(rel)

        self.external_vocab: set[str] = _external_vocabulary()
        self.code_words: set[str] = set()
        self.modmap: dict[str, str] = {}
        self.module_symbols: dict[str, set[str]] = defaultdict(set)

        py_rels = [r for r in self.tracked if r.endswith(".py")]
        for rel in py_rels:
            mod = rel[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            self.modmap[mod] = rel

        for rel in py_rels:
            src = _read(root / rel)
            if not src:
                continue
            self.code_words.update(
                re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _strip_comments_and_docstrings(src))
            )
            mod = rel[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            self._collect_module_symbols(mod, tree.body)

        for rel in self.tracked:
            stem = rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if stem:
                self.code_words.add(stem)
        self._index_web_and_config()

    def _collect_module_symbols(self, mod: str, body: list[ast.stmt]) -> None:
        """Record top-level names, descending into module-level try/if blocks.

        ``_FERNET = derive_fernet(...)`` inside a ``try:`` is still a
        module-level symbol; treating only ``tree.body`` as top level
        reported those as missing.
        """
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.module_symbols[mod].add(node.name)
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        self.module_symbols[mod].add(tgt.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                self.module_symbols[mod].add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                # re-exports count as top-level symbols
                for alias in node.names:
                    self.module_symbols[mod].add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, (ast.Try, ast.If)):
                self._collect_module_symbols(mod, node.body)
                self._collect_module_symbols(mod, node.orelse)
                self._collect_module_symbols(mod, getattr(node, "finalbody", []))
                for handler in getattr(node, "handlers", []):
                    self._collect_module_symbols(mod, handler.body)

    def _index_web_and_config(self) -> None:
        root = self.root
        web_comment = re.compile(r"//[^\n]*|/\*.*?\*/|<!--.*?-->", re.S)
        for rel in self.tracked:
            if rel.endswith((".vue", ".ts", ".js", ".html")):
                self.code_words.update(
                    re.findall(
                        r"[A-Za-z_$][A-Za-z0-9_$]*", web_comment.sub(" ", _read(root / rel))
                    )
                )
            elif rel.endswith((".sql", ".yml", ".yaml", ".json", ".toml", ".ini", ".sh")):
                self.code_words.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", _read(root / rel)))


def iter_comments(src: str) -> Iterator[tuple[int, str]]:
    """Yield (line, text) per comment, with text widened to its whole block.

    A run of consecutive ``#`` lines is one thought. "Removed." on the
    third line explains the dangling name on the first, so each line is
    reported with its block's full text attached for the prose check.
    """
    try:
        toks = [
            (t.start[0], t.string)
            for t in tokenize.generate_tokens(io.StringIO(src).readline)
            if t.type == tokenize.COMMENT
        ]
    except (tokenize.TokenError, IndentationError):
        return
    blocks: list[list[tuple[int, str]]] = []
    for lineno, text in toks:
        if blocks and lineno == blocks[-1][-1][0] + 1:
            blocks[-1].append((lineno, text))
        else:
            blocks.append([(lineno, text)])
    for block in blocks:
        joined = " ".join(t for _, t in block)
        for lineno, text in block:
            # text stays the individual line (for display); the block text is
            # appended out of band so the historical check sees the whole thought
            yield lineno, text + "\x00" + joined


def iter_docstring_lines(src: str) -> Iterator[tuple[int, str]]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        ds = _docstring_node(node)
        if ds is None:
            continue
        # Same block-context rule as comments: a docstring is one thought,
        # so "the since-removed X" three lines up still explains this line.
        whole = " ".join(ds.value.value.split())
        for offset, line in enumerate(ds.value.value.split("\n")):
            if line.strip():
                yield ds.lineno + offset, line + "\x00" + whole


def _finding(det: str, rel: str, line: int, text: str, detail: str) -> Finding:
    return Finding(det=det, file=rel, line=line, text=_line_of(text)[:240], detail=detail)


def _line_of(text: str) -> str:
    """The displayable comment line (drops any attached block context)."""
    return text.split("\x00", 1)[0].strip()


def _context_of(text: str) -> str:
    """The full comment block, for judging whether a reference is historical."""
    return text.replace("\x00", " ")


def _is_historical(text: str, ref: str) -> bool:
    """Is THIS reference historical, judged by its own sentence?

    Block scope was too coarse and it hid real drift. One "was"/"removed"/
    "legacy" anywhere in a comment exempted every name in it, so a paragraph
    that opened with a live pointer and closed with an unrelated "X was
    removed" went unchecked. Measured: block scope reported 9 findings where
    sentence scope reports 30-odd, and three of the extras were genuine dead
    cross-references.

    Worse, it was self-defeating. Corrected comments are themselves written as
    history ("this used to point at Y"), so under block scope every comment
    this scanner was built to protect became permanently exempt from it —
    including one that shipped a fabricated migration number.

    So: split the block into sentences and ask whether the sentence carrying
    the reference is narrating the past. "gdx_dispatch.tasks.qb_sync ... was a
    no-op stub. Removed." still exempts, because that is one sentence-ish
    span. A live pointer in a neighbouring sentence no longer rides along.
    """
    block = _context_of(text)
    # Sentence-ish: terminal punctuation, em-dash clauses and line breaks all
    # separate thoughts in these comments.
    for sentence in re.split(r"(?<=[.;!?])\s+|\n{2,}", block):
        if ref in sentence:
            return bool(_HISTORICAL.search(sentence))
    # Reference not located in any sentence (unusual splitting) — fall back to
    # the block so behaviour degrades to the old, safer-for-noise default.
    return bool(_HISTORICAL.search(block))


def scan_symbols(idx: Index, rel: str, line: int, text: str) -> list[Finding]:
    """D1 — names in a comment that exist in no code anywhere."""
    out: list[Finding] = []
    text_line = _line_of(text)
    for m in RE_CALL.finditer(text_line):
        name = m.group(1)
        if name in _ENGLISH or name in idx.external_vocab or name in idx.code_words:
            continue
        if _is_historical(text, m.group(0)):
            continue
        out.append(_finding("D1", rel, line, text, f"`{name}()` appears in no code"))
    for m in RE_CLASS.finditer(text_line):
        name = m.group(1)
        if name in idx.external_vocab or name in idx.code_words:
            continue
        if _is_historical(text, name):
            continue
        out.append(_finding("D1", rel, line, text, f"`{name}` appears in no code"))
    return out


def scan_modpaths(idx: Index, rel: str, line: int, text: str) -> list[Finding]:
    """X1 — dotted gdx_dispatch.* references that no longer resolve."""
    out: list[Finding] = []
    text_line = _line_of(text)
    for m in RE_MODPATH.finditer(text_line):
        dotted = m.group(0)
        if _is_historical(text, dotted):
            continue
        if dotted in idx.modmap:
            continue
        parent, _, attr = dotted.rpartition(".")
        if parent in idx.modmap:
            if attr not in idx.module_symbols.get(parent, set()):
                out.append(
                    _finding("X1", rel, line, text, f"`{dotted}` — `{attr}` not in {parent}")
                )
            continue
        grand, _, cls = parent.rpartition(".")
        if grand in idx.modmap and cls in idx.module_symbols.get(grand, set()):
            continue
        parts = dotted.split(".")
        best = 0
        for i in range(len(parts), 0, -1):
            if ".".join(parts[:i]) in idx.modmap:
                best = i
                break
        if best == 0:
            out.append(_finding("X1", rel, line, text, f"`{dotted}` — no such module"))
        elif best < len(parts) - 1:
            resolved = ".".join(parts[:best])
            out.append(
                _finding("X1", rel, line, text, f"`{dotted}` — resolves only to `{resolved}`")
            )
    return out


def scan_filepaths(idx: Index, rel: str, line: int, text: str) -> list[Finding]:
    """X2 — file references that do not exist / line numbers past EOF."""
    out: list[Finding] = []
    text_line = _line_of(text)
    # Usage examples, templates and paths outside the repo are not pointers:
    # `/tmp/<dbname>_data.sql`, `this_file.py`, `~/.claude/hooks/x.py`.
    if re.search(r"[<>{}$*~]|\bthis_file\b|/tmp/|\.claude/", text_line):
        return out
    for m in RE_FILEPATH.finditer(text_line):
        ref, lineno = m.group(1), m.group(2)
        if _is_historical(text, ref):
            continue
        # a bare or partial path that suffixes a real tracked path is shorthand
        if ref in idx.tracked_set or any(
            t.endswith("/" + ref) for t in idx.basenames.get(ref.rsplit("/", 1)[-1], [])
        ):
            if lineno and ref in idx.tracked_set:
                total = len(_read(idx.root / ref).split("\n"))
                if int(lineno) > total:
                    out.append(
                        _finding("X2", rel, line, text, f"`{ref}:{lineno}` — file has {total} lines")
                    )
            continue
        base = ref.rsplit("/", 1)[-1]
        if base in idx.basenames or base in _EXTERNAL_FILES:
            continue  # exists elsewhere, or is a third-party library name
        out.append(_finding("X2", rel, line, text, f"`{ref}` — no such file in repo"))
    return out


def scan_params(src: str, rel: str) -> list[Finding]:
    """D2 — documented parameters the signature does not have."""
    out: list[Finding] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node, clean=False)
        if not doc:
            continue
        args = node.args
        real: set[str] = set()
        for group in (args.posonlyargs, args.args, args.kwonlyargs):
            real.update(a.arg for a in group)
        if args.vararg:
            real.add(args.vararg.arg)
        if args.kwarg:
            real.add(args.kwarg.arg)

        documented: set[str] = set()
        in_args = False
        args_indent = 0
        for raw in doc.split("\n"):
            stripped = raw.strip()
            if re.match(r"^(Args|Arguments|Params|Parameters)\s*:\s*$", stripped):
                in_args = True
                args_indent = len(raw) - len(raw.lstrip())
                continue
            if in_args:
                if not stripped:
                    continue
                indent = len(raw) - len(raw.lstrip())
                # any line at or left of the "Args:" indent closes the block —
                # this is what keeps prose sections ("Precedence: ...") out
                if indent <= args_indent:
                    in_args = False
                else:
                    m = RE_PARAM_GOOGLE.match(raw)
                    if m:
                        documented.add(m.group(2))
            for m in RE_PARAM_SPHINX.finditer(raw):
                documented.add(m.group(1))

        ghosts = documented - real - {"self", "cls"}
        if ghosts:
            out.append(
                _finding(
                    "D2",
                    rel,
                    node.lineno,
                    f"def {node.name}({', '.join(sorted(real))})",
                    f"docstring documents absent param(s): {sorted(ghosts)}",
                )
            )
    return out


def scan_routes(src: str, rel: str) -> list[Finding]:
    """D3 — a handler docstring whose stated method/path fights its decorator."""
    out: list[Finding] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        method = path = None
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr in ("get", "post", "put", "patch", "delete")
            ):
                method = dec.func.attr.upper()
                if dec.args and isinstance(dec.args[0], ast.Constant):
                    path = dec.args[0].value
        if not method or not path:
            continue
        doc = ast.get_docstring(node, clean=False)
        if not doc:
            continue
        for m in RE_ROUTE.finditer(doc):
            claimed_method, claimed_path = m.group(1), m.group(2)
            # "an SPA can't GET /start directly" explains the POST; it does
            # not claim this handler is a GET.
            line_start = doc.rfind("\n", 0, m.start()) + 1
            line_end = doc.find("\n", m.end())
            doc_line = doc[line_start: line_end if line_end != -1 else len(doc)]
            if re.search(r"\b(can't|cannot|can not|won't|instead of|rather than|"
                         r"not a|isn't|is not|no longer)\b", doc_line, re.I):
                continue
            if claimed_path.rstrip("/") != path.rstrip("/"):
                continue  # talking about some other endpoint
            if claimed_method != method:
                out.append(
                    _finding(
                        "D3",
                        rel,
                        node.lineno,
                        m.group(0),
                        f"decorator is {method} {path}",
                    )
                )
    return out


def scan(
    root: Path,
    detectors: Iterable[str],
    path_filter: str | None = None,
    include_tests: bool = False,
) -> list[Finding]:
    detectors = set(detectors)
    idx = Index(root)
    findings: list[Finding] = []

    for rel in idx.tracked:
        if not rel.endswith(".py"):
            continue
        if not include_tests and "/tests/" in rel:
            continue
        if path_filter and path_filter not in rel:
            continue
        src = _read(root / rel)
        if not src:
            continue

        if {"D1", "X1", "X2"} & detectors:
            lines = list(iter_comments(src)) + list(iter_docstring_lines(src))
            for line, text in lines:
                if "D1" in detectors:
                    findings += scan_symbols(idx, rel, line, text)
                if "X1" in detectors:
                    findings += scan_modpaths(idx, rel, line, text)
                if "X2" in detectors:
                    findings += scan_filepaths(idx, rel, line, text)
        if "D2" in detectors:
            findings += scan_params(src, rel)
        if "D3" in detectors:
            findings += scan_routes(src, rel)

    findings.sort(key=lambda f: (f["det"], f["file"], f["line"]))
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--det", default=",".join(ALL_DETECTORS),
                    help=f"comma-separated subset of {','.join(ALL_DETECTORS)}")
    ap.add_argument("--path", default=None, help="only scan paths containing this substring")
    ap.add_argument("--include-tests", action="store_true")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--root", default=str(REPO_ROOT))
    args = ap.parse_args(argv)

    dets = [d.strip().upper() for d in args.det.split(",") if d.strip()]
    unknown = set(dets) - set(ALL_DETECTORS)
    if unknown:
        print(f"unknown detector(s): {sorted(unknown)}", file=sys.stderr)
        return 2

    findings = scan(Path(args.root), dets, args.path, args.include_tests)

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(findings, indent=1))

    by_det: dict[str, int] = defaultdict(int)
    for f in findings:
        by_det[f["det"]] += 1
    for f in findings:
        print(f"{f['det']}  {f['file']}:{f['line']}  {f['detail']}")
        print(f"      {f['text']}")
    print()
    print("=== comment-drift summary ===")
    for det in ALL_DETECTORS:
        if det in dets:
            print(f"  {det}: {by_det[det]}")
    print(f"  total: {len(findings)}")
    # Non-zero exit only signals "review these" — historical notes are skipped
    # by the prose heuristics, so a hit is usually worth a look.
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
