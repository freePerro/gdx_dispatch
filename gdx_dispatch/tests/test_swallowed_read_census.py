"""Contract tests for the swallowed-read census.

The instrument is the deliverable, so these tests are the deliverable too. The
recipe this tool replaces lived as prose in a commit message and was retyped
three times; a census whose own tests cannot redden is the same failure in a
new file. Every test below is one of the falsifiers named in GDXA-169, and the
two corrections are pinned EXECUTABLY — the fix-(a) test reconstructs the old
ancestor-only containment check and asserts it disagrees, and the fix-(b) test
runs the recorded method set and asserts it goes blind. Neither can pass by
accident against the implementation it replaced.

The real-tree tests at the bottom are the half that catches the census going
quietly blind over actual code. They are written to stay green as the nine
domain lanes land their fixes — a recorded site is allowed to disappear ONLY
from a file that now carries a containment token — so they measure the
instrument and never the backlog.
"""
from __future__ import annotations

import ast
import subprocess
import textwrap
from pathlib import Path
from shutil import which

import pytest

from gdx_dispatch.tools.swallowed_read_census import (
    BASE_METHODS,
    EXTENDED_METHODS,
    Finding,
    _with_opens_savepoint,
    detect_repo_root,
    injected_annotation_aliases,
    iter_python_files,
    render,
    scan_source,
    scan_source_with_exempt,
    scan_tree,
    session_params,
    to_json,
)

# The prose recipe's method set: `get` is the one this tool adds (correction b).
RECORDED_METHODS = frozenset(
    {"execute", "flush", "commit", "add", "add_all", "merge", "delete"}
)


def _scan(body: str, methods: frozenset[str] = BASE_METHODS) -> list[Finding]:
    return scan_source(textwrap.dedent(body).lstrip("\n"), "pkg/mod.py", methods)


def _keys(findings: list[Finding]) -> list[str]:
    return [f.key for f in findings]


# ── it sees the bare shape ─────────────────────────────────────────────────


def test_a_bare_swallowed_execute_is_one_finding():
    findings = _scan(
        """
        def resolve(db, key):
            try:
                return db.execute(select(X)).scalar_one_or_none()
            except Exception:
                log.exception("resolve_failed")
                return None
        """
    )
    assert _keys(findings) == ["pkg/mod.py::resolve::execute"]
    assert findings[0].call_line == 3
    assert findings[0].try_line == 2
    assert findings[0].session == "db"
    assert findings[0].extended is False


@pytest.mark.parametrize("name", sorted({"db", "session", "tenant_db", "sess", "s", "tdb", "control_db", "conn"}))
def test_every_recognised_session_parameter_name_is_seen(name):
    """All eight names from the recorded predicate, so dropping one reddens."""
    findings = _scan(
        f"""
        def resolve({name}):
            try:
                return {name}.execute(q)
            except Exception:
                return None
        """
    )
    assert len(findings) == 1, f"{name} stopped being recognised as a session"
    assert findings[0].session == name


def test_a_session_parameter_under_another_name_is_a_blind_spot():
    """Asserts the LIMIT on purpose: `database` is not in the eight names.

    Stated in the tool's own header as a blind spot. Pinned here so widening
    the set is a deliberate act with a number attached, not a silent one.
    """
    assert (
        _scan(
            """
            def resolve(database):
                try:
                    return database.execute(q)
                except Exception:
                    return None
            """
        )
        == []
    )


def test_a_session_reached_through_an_attribute_is_a_blind_spot():
    """The other declared blind spot: `self.db`. Also asserted, also a limit."""
    assert (
        _scan(
            """
            class Svc:
                def resolve(self, db):
                    try:
                        return self.db.execute(q)
                    except Exception:
                        return None
            """
        )
        == []
    )


# ── correction (a): containment is per-call and may sit inside the try ──────


CONTAINED_INSIDE_THE_TRY = """
    def resolve(db, key):
        try:
            with contained_read(db):
                return db.execute(select(X)).scalar_one_or_none()
        except Exception:
            log.exception("resolve_failed")
            return None
    """


def test_contained_read_inside_the_try_is_not_a_finding():
    """Correction (a). This is the landed idiom — `contained_read`'s rule 1 puts
    the savepoint INSIDE the `try`, because around it the `except` never runs and
    a degraded read becomes a 500."""
    assert _scan(CONTAINED_INSIDE_THE_TRY) == []


def test_the_old_ancestor_only_check_gets_that_wrong(monkeypatch):
    """Proves the previous paragraph is a CORRECTION, not a preference.

    Reconstructs the recipe's ancestor-only containment — a savepoint credited
    only when it encloses the whole `try` — and asserts it reports the landed
    idiom as broken. That reading is why the ledger read 73 on both refs before
    and after the fix merged. Without this test, the one above would pass
    against the very implementation it exists to replace.
    """
    import gdx_dispatch.tools.swallowed_read_census as census

    def ancestor_only(call, parents, try_node, session):
        current = parents.get(try_node)
        while current is not None:
            if isinstance(current, (ast.With, ast.AsyncWith)) and _with_opens_savepoint(
                current, session, 10**9
            ):
                return True
            current = parents.get(current)
        return False

    monkeypatch.setattr(census, "_is_contained", ancestor_only)
    assert _keys(_scan(CONTAINED_INSIDE_THE_TRY)) == ["pkg/mod.py::resolve::execute"]


def test_begin_nested_inside_the_try_is_not_a_finding():
    """The hand-written savepoint that predates `contained_read` — the shape in
    `modules/quickbooks/sync.py` (#760) and `core/job_photos.py` (#482), worth
    24 and 2 falsely-reported calls respectively under the old reading."""
    assert (
        _scan(
            """
            def pull(db, rows):
                try:
                    for row in rows:
                        with db.begin_nested():
                            db.execute(upsert(row))
                except Exception:
                    log.exception("pull_failed")
                    return 0
            """
        )
        == []
    )


def test_exitstack_enter_context_is_not_a_finding():
    """`ExitStack.enter_context(contained_read(db))` — the form a
    `with contained_read(` text scan misses (GDXA-151). Unconditional, which is
    the only form credited; see the test below."""
    assert (
        _scan(
            """
            def resolve(db, key):
                try:
                    with ExitStack() as stack:
                        stack.enter_context(contained_read(db))
                        return db.execute(select(X)).scalar_one_or_none()
                except Exception:
                    return None
            """
        )
        == []
    )


def test_a_conditional_enter_context_is_still_a_finding():
    """`if guard: stack.enter_context(contained_read(db))` leaves the read naked
    whenever `guard` is false, and which way it goes is not knowable here. A site
    that is only sometimes contained is still a hazard, so it is reported.

    Crediting it was the first draft's bug, and it was invisible because no call
    site in the repo uses the ExitStack form at all — the branch had no real
    input to get wrong, so only a synthetic test could find it.
    """
    assert len(
        _scan(
            """
            def resolve(db, key, guard):
                try:
                    with ExitStack() as stack:
                        if guard:
                            stack.enter_context(contained_read(db))
                        return db.execute(select(X)).scalar_one_or_none()
                except Exception:
                    return None
            """
        )
    ) == 1


def test_an_enter_context_below_the_call_does_not_contain_it():
    """A savepoint entered AFTER the read cannot protect it. Ordering matters,
    so the ExitStack credit above cannot be claimed by any mention in the block.
    """
    assert len(
        _scan(
            """
            def resolve(db, key):
                try:
                    with ExitStack() as stack:
                        row = db.execute(select(X)).scalar_one_or_none()
                        stack.enter_context(contained_read(db))
                        return row
                except Exception:
                    return None
            """
        )
    ) == 1


def test_a_savepoint_wrapping_the_whole_try_is_still_a_finding():
    """The deliberate narrowing, and the reason it is not an oversight.

    That block exits CLEANLY — the `except` ate the error — so the context
    manager issues RELEASE SAVEPOINT on an aborted transaction, which raises
    25P02 out of the `with` itself while the caller's `commit()` stays dead:
    strictly worse than not wrapping. Pinned on real Postgres by
    `test_contained_read.py::test_pg_a_callee_that_swallows_its_own_failure_is_not_contained`.
    The recorded recipe credited these sites; this tool reports them.
    """
    assert len(
        _scan(
            """
            def resolve(db, key):
                with contained_read(db):
                    try:
                        return db.execute(select(X)).scalar_one_or_none()
                    except Exception:
                        return None
            """
        )
    ) == 1


def test_containment_must_name_the_same_session():
    """A savepoint on another session contains nothing about this one."""
    assert len(
        _scan(
            """
            def resolve(db, control_db):
                try:
                    with contained_read(control_db):
                        return db.execute(select(X)).scalar_one_or_none()
                except Exception:
                    return None
            """
        )
    ) == 1


# ── correction (b): `get` is in the method set ─────────────────────────────


def test_a_swallowed_db_get_is_a_finding():
    """Correction (b). `core/payments.py::card_surcharge_rate` is this shape and
    is absent from the recorded 73-row table for exactly this reason."""
    findings = _scan(
        """
        def card_surcharge_rate(db, tenant_id):
            try:
                row = db.get(TenantSettings, tenant_id)
            except Exception:
                log.exception("read_failed")
                return 0
            return row
        """
    )
    assert _keys(findings) == ["pkg/mod.py::card_surcharge_rate::get"]


def test_the_recorded_method_set_goes_blind_on_db_get():
    """Proves the previous test is a CORRECTION: the same source, scanned with
    the prose recipe's seven methods, reports nothing."""
    findings = _scan(
        """
        def card_surcharge_rate(db, tenant_id):
            try:
                row = db.get(TenantSettings, tenant_id)
            except Exception:
                return 0
        """,
        methods=RECORDED_METHODS,
    )
    assert findings == []


def test_extended_methods_are_off_by_default_and_flagged_when_on():
    """`scalar`/`scalars`/`refresh`/`query`/`get_one` are a separate delta, so
    the headline number stays comparable with the recorded census."""
    src = """
        def resolve(db, key):
            try:
                return db.scalar(select(X))
            except Exception:
                return None
        """
    assert _scan(src) == []
    findings = _scan(src, methods=BASE_METHODS | EXTENDED_METHODS)
    assert _keys(findings) == ["pkg/mod.py::resolve::scalar"]
    assert findings[0].extended is True


# ── handlers that do not swallow ───────────────────────────────────────────


def test_a_handler_that_raises_is_not_a_finding():
    assert (
        _scan(
            """
            def resolve(db, key):
                try:
                    return db.execute(q)
                except Exception:
                    log.exception("resolve_failed")
                    raise
            """
        )
        == []
    )


def test_a_handler_that_rolls_the_session_back_is_exempt_but_counted():
    """The recorded predicate's exemption — and it is NOT a safety argument.

    `contained_read` rule 4 (`core/database.py`): a full rollback "expires every
    object the caller is holding", so this handler discards the caller's work
    SILENTLY rather than failing as 25P02. It is a different defect, not a cure,
    and on `d3bde561` it covers 106 more calls than the headline number — 46 of
    them pure `execute` reads. So the site is kept OUT of the headline (to stay
    comparable with the recorded census) and counted in `rollback_exempt`, which
    the header prints. Dropping it silently is what this test exists to prevent:
    a lane could then close a finding by adding `db.rollback()` and the number
    would fall while the caller's identity map was expired mid-request.
    """
    src = """
        def resolve(db, key):
            try:
                return db.execute(q)
            except Exception:
                db.rollback()
                return None
        """
    findings, exempt = scan_source_with_exempt(textwrap.dedent(src).lstrip("\n"), "pkg/mod.py")
    assert findings == []
    assert _keys(exempt) == ["pkg/mod.py::resolve::execute"]


def test_include_rollback_handlers_folds_the_exemption_in(tmp_path):
    """The flag that makes the exemption countable rather than merely stated."""
    _write(tmp_path, "pkg/live.py", """
        def resolve(db, key):
            try:
                return db.execute(q)
            except Exception:
                db.rollback()
                return None
        """)
    off = scan_tree(tmp_path)
    assert off.calls == 0
    assert len(off.rollback_exempt) == 1
    on = scan_tree(tmp_path, include_rollback_handlers=True)
    assert on.calls == 1
    assert _keys(on.findings) == ["pkg/live.py::resolve::execute"]


def test_the_rollback_exemption_travels_with_the_number(tmp_path):
    """A count whose largest exemption is invisible is the defect this tool was
    written to end. It must be in the human header AND the machine output."""
    _write(tmp_path, "pkg/live.py", """
        def resolve(db, key):
            try:
                return db.execute(q)
            except Exception:
                db.rollback()
                return None
        """)
    census = scan_tree(tmp_path)
    header = render(census)
    assert "ROLLBACK-HANDLER EXEMPTION: 1 further" in header
    assert "rule 4" in header
    assert "NOT counted above" in header
    report = to_json(census)["rollback_handler_exemption"]
    assert report["calls"] == 1
    assert report["by_method"] == {"execute": 1}
    assert report["included_in_totals"] is False
    assert report["keys"] == ["pkg/live.py::resolve::execute"]
    assert "rule 4" in report["why_that_is_not_safe"]


def test_a_rollback_handler_consumes_the_error_so_the_outer_try_is_not_blamed():
    """A handler that rolls back also SWALLOWS, so the `try` outside it never
    runs. Attributing the call outward anyway would report a site whose failure
    the outer handler can never see."""
    findings, exempt = scan_source_with_exempt(
        textwrap.dedent(
            """
            def resolve(db, key):
                try:
                    try:
                        return db.execute(q)
                    except Exception:
                        db.rollback()
                        return None
                except Exception:
                    return {}
            """
        ).lstrip("\n"),
        "pkg/mod.py",
    )
    assert findings == []
    assert len(exempt) == 1


def test_a_rollback_on_a_different_session_does_not_excuse_it():
    """Rolling back some other session leaves this one aborted."""
    assert len(
        _scan(
            """
            def resolve(db, control_db):
                try:
                    return db.execute(q)
                except Exception:
                    control_db.rollback()
                    return None
            """
        )
    ) == 1


def test_a_try_with_no_handler_at_all_is_not_a_finding():
    """`try/finally` swallows nothing — the error still reaches the caller."""
    assert (
        _scan(
            """
            def resolve(db, key):
                try:
                    return db.execute(q)
                finally:
                    log.info("done")
            """
        )
        == []
    )


def test_a_narrow_handler_still_counts_because_the_error_propagates():
    """`modules/workflows/engine.py:90` was confirmed real by exactly this
    route: its `except (ValueError, TypeError)` lets the DB error through to an
    outer `except Exception` that then writes on the dead session (GDXA-152).
    Exception types are not analysed — declared as a blind spot in both
    directions."""
    assert len(
        _scan(
            """
            def resolve(db, key):
                try:
                    return db.execute(q)
                except (ValueError, TypeError):
                    return None
            """
        )
    ) == 1


# ── framework-injected sessions ────────────────────────────────────────────


def test_a_session_defaulted_to_depends_is_not_a_finding():
    assert (
        _scan(
            """
            def endpoint(key, db = Depends(get_db)):
                try:
                    return db.execute(q)
                except Exception:
                    return None
            """
        )
        == []
    )


def test_an_annotated_depends_session_is_not_a_finding():
    """The documented deviation from the recorded predicate, which excluded the
    default form and not this one even though the ownership is identical. It
    costs one recorded site — `api/public_router.py::list_public_listings` —
    which this tool refuses on purpose rather than by accident."""
    assert (
        _scan(
            """
            def endpoint(db: Annotated[Session, Depends(get_db)], key: str):
                try:
                    return db.execute(q)
                except Exception:
                    return None
            """
        )
        == []
    )
    fn = ast.parse(
        "def f(db: Annotated[Session, Depends(get_db)]): pass"
    ).body[0]
    assert session_params(fn) == set()


def test_a_module_level_annotated_depends_alias_is_not_a_finding():
    """The third spelling of the same framework-injected ownership.

    `TenantDB = Annotated[Session, Depends(get_db)]` is live in `core/gdpr.py:22`
    and `core/ai_quote.py:781`. Without resolving the alias the `Depends` hides
    behind a Name, and the next endpoint written that way is a false positive
    indistinguishable from a real defect. Nothing in the tree hits it today, so
    only a synthetic test can hold the line.
    """
    src = """
        TenantDB = Annotated[Session, Depends(get_db)]

        def endpoint(db: TenantDB, key: str):
            try:
                return db.execute(q)
            except Exception:
                return None
        """
    assert _scan(src) == []
    tree = ast.parse(textwrap.dedent(src).lstrip("\n"))
    assert injected_annotation_aliases(tree) == frozenset({"TenantDB"})
    # …and an alias that is NOT a Depends annotation must not exempt anything.
    assert len(
        _scan(
            """
            PlainSession = Session

            def helper(db: PlainSession, key: str):
                try:
                    return db.execute(q)
                except Exception:
                    return None
            """
        )
    ) == 1


def test_a_keyword_only_session_is_seen_and_a_depends_one_is_not():
    """Defaults align to the tail of the positional list and 1:1 for kwonly —
    an off-by-one there would silently exempt or include the wrong parameter."""
    fn = ast.parse("def f(a, b=1, *, db, tdb=Depends(get_db)): pass").body[0]
    assert session_params(fn) == {"db"}


# ── the counting unit ──────────────────────────────────────────────────────


def test_one_finding_per_call_not_per_try():
    """Correction (c). Per-`try` hides the second and third unprotected read in
    the same `try`, and those are separate fixes."""
    findings = _scan(
        """
        def summary(db, key):
            try:
                a = db.execute(q1)
                b = db.execute(q2)
                c = db.get(X, key)
            except Exception:
                return {}
        """
    )
    assert len(findings) == 3
    assert {f.try_line for f in findings} == {2}
    assert _keys(findings).count("pkg/mod.py::summary::execute") == 2


def test_a_call_inside_a_handler_is_not_counted_twice():
    """A call in a handler belongs to whatever encloses the handler, not to its
    own `try` — otherwise one call is counted by both."""
    assert (
        _scan(
            """
            def resolve(db, key):
                try:
                    return compute()
                except Exception:
                    db.execute(log_it)
                    return None
            """
        )
        == []
    )


def test_an_inner_try_that_reraises_still_lands_on_the_outer_swallow():
    """Attribution walks OUTWARD: the call really does end in a degraded
    return, so it is reported against the `try` that swallows."""
    findings = _scan(
        """
        def resolve(db, key):
            try:
                try:
                    return db.execute(q)
                except TimeoutError:
                    raise
            except Exception:
                return None
        """
    )
    assert len(findings) == 1
    assert findings[0].try_line == 2


def test_the_key_survives_every_line_moving():
    """Requirement 4: nine lanes edit these files concurrently, so a line-keyed
    artifact would be pure conflict — `.tenant_plane_redundant_filter_baseline`
    is the documented sharp edge this avoids."""
    body = """
        def resolve(db, key):
            try:
                return db.execute(q)
            except Exception:
                return None
        """
    before = _scan(body)
    after = _scan("# a new comment\n" * 12 + textwrap.dedent(body).lstrip("\n"))
    assert _keys(before) == _keys(after)
    assert before[0].call_line != after[0].call_line


def test_a_method_gets_a_dotted_qualname():
    findings = _scan(
        """
        class Queue:
            def drain(self, db):
                try:
                    return db.execute(q)
                except Exception:
                    return None
        """
    )
    assert _keys(findings) == ["pkg/mod.py::Queue.drain::execute"]


def test_a_nested_function_is_scanned_on_its_own_parameters():
    """Each definition is scanned with its own signature, so a nested helper's
    call is attributed to the nested helper and counted once."""
    findings = _scan(
        """
        def outer(payload):
            def inner(db):
                try:
                    return db.execute(q)
                except Exception:
                    return None
            return inner
        """
    )
    assert _keys(findings) == ["pkg/mod.py::outer.inner::execute"]


def test_an_async_helper_is_scanned():
    findings = _scan(
        """
        async def resolve(db, key):
            try:
                return db.execute(q)
            except Exception:
                return None
        """
    )
    assert _keys(findings) == ["pkg/mod.py::resolve::execute"]


# ── the tree scan ──────────────────────────────────────────────────────────


def _write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip("\n"))
    return path


SWALLOWED = """
    def resolve(db, key):
        try:
            return db.execute(q)
        except Exception:
            return None
    """


def test_a_file_that_cannot_be_parsed_is_named_never_silently_zero(tmp_path):
    """The input that turns this instrument red without changing a finding.

    `silent_failure_scanner` swallows `SyntaxError` and moves on, so a file it
    cannot read counts as clean. A census that does that is unfalsifiable.
    """
    _write(tmp_path, "pkg/good.py", SWALLOWED)
    _write(tmp_path, "pkg/broken.py", "def resolve(db:\n")
    census = scan_tree(tmp_path)
    assert census.calls == 1
    assert len(census.unparsed) == 1
    assert census.unparsed[0].startswith("pkg/broken.py (SyntaxError")
    assert "!! not scanned: pkg/broken.py" in render(census)
    assert to_json(census)["unparsed_files"] == census.unparsed


def test_skipped_directories_are_not_scanned(tmp_path):
    """`tests/` wrap on purpose and `migrations/` hold no caller-owned ORM
    session. Excluding exactly these reproduces the 597-file scan surface."""
    _write(tmp_path, "pkg/live.py", SWALLOWED)
    for skipped in ("tests", "migrations", "frontend", "node_modules"):
        _write(tmp_path, f"{skipped}/thing.py", SWALLOWED)
    census = scan_tree(tmp_path)
    assert [f.file for f in census.findings] == ["pkg/live.py"]
    assert census.files_scanned == 1


def test_no_tracked_set_machinery_reaches_the_file_walk():
    """The always-running half of the claim below.

    The git test that follows it SKIPS in the docker image, which has no `git`
    binary — and a guard that does not run in the gate is a silent no-op. This
    one runs everywhere: it asserts the ABSENCE of every route to the git index,
    which is the direction an assertion on source text is worth anything in.
    """
    source = (
        REPO_ROOT / "gdx_dispatch" / "tools" / "swallowed_read_census.py"
    ).read_text(encoding="utf-8")
    walk = ""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == "iter_python_files":
            body = node.body[1:] if ast.get_docstring(node) else node.body
            walk = "\n".join(ast.unparse(stmt) for stmt in body)
    assert walk, "iter_python_files is gone — this guard is pointing at nothing"
    for route in ("ls-files", "tracked_files", "subprocess", "git"):
        assert route not in walk, (
            f"iter_python_files reaches the git index via {route!r}; a call site "
            "in a brand-new module then reads as zero until `git add` (GDXA-151)"
        )


@pytest.mark.skipif(which("git") is None, reason="git not on PATH")
def test_the_scan_reads_the_filesystem_not_the_git_index(tmp_path):
    """GDXA-151's post-mortem: a tracked-only scan counted a site in a new
    module as zero until someone ran `git add`.

    Skips where `git` is absent (the docker test image), which is why
    `test_no_tracked_set_machinery_reaches_the_file_walk` exists above.
    """
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    _write(tmp_path, "pkg/brand_new.py", SWALLOWED)
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    assert tracked.strip() == "", "fixture invalid — the file must be untracked"
    assert [f.file for f in scan_tree(tmp_path).findings] == ["pkg/brand_new.py"]


def test_root_scans_an_extracted_ref_not_this_worktree(tmp_path):
    """`--root` must work on a `git archive <ref>` extract, whose layout is a
    bare `gdx_dispatch/` under the extract root."""
    _write(tmp_path, "gdx_dispatch/core/thing.py", SWALLOWED)
    _write(tmp_path, "outside_the_package.py", SWALLOWED)
    census = scan_tree(tmp_path)
    assert [f.file for f in census.findings] == ["gdx_dispatch/core/thing.py"]


def test_detect_repo_root_does_not_require_ai_queue(tmp_path):
    """`silent_failure_scanner._detect_repo_root` also demands an `ai-queue/`
    directory, which a fresh clone and a `git archive` extract do not have —
    there it silently falls back to the CWD and scans nothing."""
    tools = tmp_path / "gdx_dispatch" / "tools"
    tools.mkdir(parents=True)
    assert not (tmp_path / "ai-queue").exists()
    assert detect_repo_root(tools / "swallowed_read_census.py") == tmp_path


def test_the_json_report_names_its_unit_and_keys_on_the_triple(tmp_path):
    """A number lifted out of this tool must carry its unit with it — the
    ledger's "73" is meaningless without one."""
    _write(tmp_path, "pkg/live.py", SWALLOWED)
    report = to_json(scan_tree(tmp_path))
    assert report["unit"] == "one finding per session CALL (not per try)"
    assert report["machine_key"] == "file::qualname::method"
    assert report["lower_bound"] is True
    assert report["blind_spots"], "the blind spots must travel with the number"
    assert [r["key"] for r in report["sites_detail"]] == ["pkg/live.py::resolve::execute"]
    assert report["totals"]["calls"] == 1
    # Every denominator is named, because they do not nest: one function with two
    # swallowing tries on the same method is 1 key and 2 tries.
    assert set(report["totals"]) == {
        "calls",
        "unique_keys",
        "tries",
        "files",
        "base_method_calls",
        "extended_method_calls",
    }


# ── the real tree ──────────────────────────────────────────────────────────

REPO_ROOT = detect_repo_root()

# GDXA-152's recorded table, `origin/main` `8037da11`, as (file, function).
# Line numbers are deliberately dropped: they have already drifted (`jobs.py`
# 275 -> 276, `timeclock.py` 1044 -> 1108) and are not what this pins.
RECORDED_SITES = (
    ("routers/performance.py", "_build_user_stats"),
    ("core/audit_dashboard.py", "_run_integrity_check"),
    ("core/audit_dashboard.py", "get_audit_events"),
    ("core/audit_dashboard.py", "get_audit_summary"),
    ("core/audit_dashboard.py", "export_audit_log"),
    ("core/audit_dashboard.py", "verify_audit_chain"),
    ("routers/mobile.py", "_table_columns"),
    ("routers/mobile.py", "_job_is_billed"),
    ("routers/mobile.py", "_job_not_billable"),
    ("routers/mobile.py", "_job_deposit_summary"),
    ("routers/mobile.py", "_audit_state_change"),
    ("modules/quickbooks/sync.py", "_delete_sync_enabled"),
    ("modules/quickbooks/sync.py", "pull_accounts"),
    ("routers/payroll.py", "_fetch_active_rate"),
    ("routers/payroll.py", "_fetch_tech_hours"),
    ("routers/payroll.py", "_fetch_tech_names"),
    ("core/timesheet_hours.py", "break_minutes_started_on"),
    ("core/timesheet_hours.py", "open_break_in_shift"),
    ("core/timesheet_hours.py", "break_minutes_by_entry"),
    ("core/ai_quote.py", "get_pricing_suggestions"),
    ("core/ai_quote.py", "analyze_pricing_health"),
    ("core/audit_labels.py", "_resolve_staff"),
    ("core/audit_labels.py", "_resolve_customer_users"),
    ("modules/payroll/service.py", "effective_labor_cost"),
    ("modules/proposals/totals.py", "_load_tax_labor_flag"),
    ("modules/proposals/totals.py", "_load_lines"),
    ("modules/quickbooks/banking.py", "pull_deposits"),
    ("modules/quickbooks/banking.py", "pull_transfers"),
    ("routers/budgets.py", "_pnl_last_synced_at"),
    ("routers/budgets.py", "_load_qb_accounts"),
    ("routers/jobs.py", "_holding_area_id_by_name"),
    ("routers/jobs.py", "_display_state_for_jobs"),
    ("routers/timeclock.py", "_export_context"),
    ("routers/timeclock.py", "_tech_names"),
    ("tasks/payroll_timesheet.py", "_audit_says"),
    ("tasks/payroll_timesheet.py", "_names"),
    ("core/billing_lanes.py", "install_labor_line"),
    ("core/customer_views.py", "_recently_recorded"),
    ("core/email_recipients.py", "resolve_recipient"),
    ("core/email_sender.py", "get_email_config"),
    ("core/mcp_tools/email_read.py", "handler"),
    ("core/transactional_email.py", "recently_sent"),
    ("modules/bank_feeds/service.py", "tenant_zoneinfo"),
    ("modules/outlook/bounce_detect.py", "_outbound_row"),
    ("modules/outlook/resend_detect.py", "process_resends"),
    ("modules/proposals/router.py", "_serialize_public_estimate"),
    ("routers/catalog.py", "_list_virtual_catalogs"),
    ("routers/estimates.py", "_holding_area_id_by_name"),
    ("routers/expenses.py", "_annotate_gl_accounts"),
    ("routers/labor.py", "_resolve_hourly_rate"),
    ("routers/mobile_invoicing.py", "_send_invoice_email"),
    ("routers/winback.py", "_query_candidates"),
    ("tasks/planner_digest.py", "_digest_sender_user_id"),
    ("tasks/plugin_email_outbox.py", "_deliver"),
    ("tasks/stale_intent_sweep.py", "_connected_account_for"),
)

# The one recorded site this tool drops on purpose, with the reason.
DELIBERATELY_DROPPED = {
    ("api/public_router.py", "list_public_listings"): (
        "db: Annotated[Session, Depends(get_db)] — framework-injected, the same "
        "ownership the recorded predicate already excluded in its default form"
    ),
}

# Fixes merged to `origin/main` before this tool existed. Each MUST read zero:
# the whole point of correction (a) is that a landed fix makes the number fall.
LANDED_FIXES = (
    ("core/modules.py", "enabled_module_keys", "a78db59b / #807"),
    ("core/settings_flags.py", "qb_money_pull_paused", "a78db59b / #807"),
    ("core/user_display.py", "resolve_author_name", "a78db59b / #807"),
    ("core/webhooks/emit.py", "_emit", "a78db59b / #807"),
    ("modules/workflows/engine.py", "_resolve_rule_customer", "a78db59b / #807"),
    ("core/plugin_consent.py", "any_event_consent", "193395fa / #808"),
    ("core/holding_areas.py", "holding_area_id_by_name", "GDXA-157 #852 / GDXA-158"),
)

CONTAINMENT_TOKENS = ("contained_read", "begin_nested")


@pytest.fixture(scope="module")
def real_census():
    return scan_tree(REPO_ROOT)


@pytest.fixture(scope="module")
def real_functions(real_census):
    """`{(file, last component of qualname)} -> method set` over the real tree."""
    seen: dict[tuple[str, str], set[str]] = {}
    for finding in real_census.findings:
        key = (finding.file.removeprefix("gdx_dispatch/"), finding.qualname.rsplit(".", 1)[-1])
        seen.setdefault(key, set()).add(finding.method)
    return seen


def test_the_real_tree_parses_completely(real_census):
    """A syntax error anywhere would silently shrink every number below it."""
    assert real_census.unparsed == []
    assert real_census.files_scanned > 500, real_census.files_scanned


def test_card_surcharge_rate_is_visible_in_the_real_tree(real_functions):
    """The named acceptance criterion for correction (b): triage's worst
    instance — a read behind the public, unauthenticated pay page — is
    invisible to the recorded method set.

    Two-sided, so it stays honest after money-billing lands its fix: if the
    function carries a containment token it must be ABSENT instead. It can
    therefore only redden if the census's verdict stops matching the source.
    """
    source = _function_source("core/payments.py", "card_surcharge_rate")
    contained = any(token in source for token in CONTAINMENT_TOKENS)
    methods = real_functions.get(("core/payments.py", "card_surcharge_rate"), set())
    if contained:
        assert methods == set(), (
            "card_surcharge_rate is contained now, but the census still reports "
            f"it via {methods} — correction (a) has regressed"
        )
    else:
        assert "get" in methods, (
            "card_surcharge_rate reads with db.get and has no savepoint, but the "
            "census does not see it — correction (b) has regressed"
        )


def test_every_landed_fix_reports_zero(real_functions):
    """Correction (a) over real code: the six merged fixes must not be counted.

    The old reading reported all six as broken, which is why the ledger read 73
    on both refs. Each function is asserted to still EXIST, so a rename cannot
    turn this green by accident.
    """
    still_reported = []
    for rel, name, provenance in LANDED_FIXES:
        source = _function_source(rel, name)
        assert source is not None, f"{rel}::{name} is gone — update LANDED_FIXES"
        assert any(token in source for token in CONTAINMENT_TOKENS), (
            f"{rel}::{name} lost its savepoint ({provenance} was unwrapped)"
        )
        if real_functions.get((rel, name)):
            still_reported.append(f"{rel}::{name} ({provenance})")
    assert still_reported == [], (
        "these fixes are on origin/main and contained, yet the census still "
        f"reports them: {still_reported}"
    )


def test_ensure_consent_table_is_still_reported(real_functions):
    """The other half of the same file, and the reason a file-level check would
    not do: `193395fa` wrapped `any_event_consent` and left
    `ensure_consent_table` genuinely open (GDXA-161). A census that credits a
    whole file for one fix would lose it."""
    source = _function_source("core/plugin_consent.py", "ensure_consent_table")
    if not any(token in source for token in CONTAINMENT_TOKENS):
        assert real_functions.get(("core/plugin_consent.py", "ensure_consent_table")), (
            "ensure_consent_table has no savepoint and is no longer reported"
        )


def test_recorded_sites_in_untouched_files_are_still_found(real_functions):
    """The regression fixture: GDXA-152's recorded table.

    A recorded site may only disappear from a file that now carries a
    containment token — that is a lane landing a fix, which is the number going
    DOWN as designed. Disappearing from a file nobody has touched means the
    census went blind, and that is what this reddens for. The file-level token
    check is deliberately coarser machinery than the tool's own per-call walk,
    so it cannot excuse a blind spot it shares.
    """
    missing: list[str] = []
    excused: list[str] = []
    for rel, name in RECORDED_SITES:
        path = REPO_ROOT / "gdx_dispatch" / rel
        if not path.exists():
            excused.append(f"{rel}::{name} (file gone)")
            continue
        source = path.read_text(encoding="utf-8")
        if f"def {name}(" not in source:
            excused.append(f"{rel}::{name} (function gone)")
            continue
        if real_functions.get((rel, name)):
            continue
        if any(token in source for token in CONTAINMENT_TOKENS):
            excused.append(f"{rel}::{name} (a lane landed a savepoint in this file)")
            continue
        missing.append(f"{rel}::{name}")
    assert missing == [], (
        "recorded sites vanished from files carrying no savepoint at all — the "
        f"census has gone blind on them: {missing}\n(legitimately excused: {excused})"
    )


def test_the_deliberately_dropped_recorded_site_stays_dropped(real_functions):
    """Absence with a reason attached, so it cannot quietly become a defect."""
    for (rel, name), reason in DELIBERATELY_DROPPED.items():
        assert not real_functions.get((rel, name)), (
            f"{rel}::{name} is reported again; it is excluded because {reason}"
        )


def _function_source(rel: str, name: str) -> str | None:
    """The source of one function in the real tree, by parsing."""
    path = REPO_ROOT / "gdx_dispatch" / rel
    if not path.exists():
        return None
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    return None


def test_the_census_finds_its_own_scan_surface(real_census):
    """The package is scanned, the excluded trees are not — asserted against the
    real tree, because a wrong root reads as a small clean number."""
    files = {f.as_posix() for f in iter_python_files(REPO_ROOT)}
    assert any("gdx_dispatch/core/payments.py" in f for f in files)
    assert not any("/gdx_dispatch/tests/" in f for f in files)
    assert not any("/gdx_dispatch/migrations/" in f for f in files)
    assert real_census.calls > 0
