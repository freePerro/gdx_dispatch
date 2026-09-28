"""The audit trail must record WHO, and CI must notice when it stops.

Measured on the live tenant before this landed: 1909 of 2251 non-auth audit
rows in 30 days carried the literal string "system" as the actor — 85% of the
audit trail with no attribution at all. Cause: a batch of generated audit
blocks resolved the actor with

    locals().get('user') or locals().get('current_user') or {}

inside handlers whose auth dependency is bound to ``_``. Both lookups missed,
every time, and the fallback was "system". Nothing failed. Nothing logged.

Two layers of test here:
  1. unit tests for resolve_audit_actor, which now handles every shape a
     handler actually binds (claim dict, User row, CustomerUser row, nothing)
  2. a source sweep asserting every audit-writing handler can still resolve an
     actor — the gate that fails the build if a new generated block
     reintroduces the bug
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from gdx_dispatch.core.audit import resolve_audit_actor

_PKG = pathlib.Path(__file__).resolve().parents[1]

#: Every tree that writes audit rows. Scoping this to routers/*.py alone —
#: which an earlier version of this gate did — left routers/auth/, api/ and
#: the whole modules/ tree unguarded, i.e. most of the surface it claims to
#: cover.
AUDIT_SOURCE_ROOTS = (_PKG / "routers", _PKG / "api", _PKG / "modules")


def _python_sources():
    for root in AUDIT_SOURCE_ROOTS:
        if root.exists():
            yield from sorted(root.rglob("*.py"))


# ---------------------------------------------------------------------------
# resolve_audit_actor
# ---------------------------------------------------------------------------


class _FakeState:
    def __init__(self, user=None):
        self.user = user


class _FakeRequest:
    def __init__(self, user=None):
        self.state = _FakeState(user)


class _OrmUser:
    """Stands in for a User / CustomerUser row."""

    def __init__(self, id_):
        self.id = id_


def test_resolves_a_jwt_claim_dict():
    assert resolve_audit_actor({"sub": "u-1", "role": "admin"}) == "u-1"


def test_resolves_the_legacy_user_id_claim():
    assert resolve_audit_actor({"user_id": "u-2"}) == "u-2"


def test_resolves_an_orm_row():
    """The portal endpoints bind a CustomerUser ORM object, not a dict.

    The old block called .get('sub') on it, which raises AttributeError inside
    the block's own try/except — so the audit row was never written at all.
    On the payment endpoints. Silently.
    """
    assert resolve_audit_actor(_OrmUser("cu-9")) == "cu-9"


def test_falls_back_to_the_authenticated_principal_on_the_request():
    # routers/auth/core.py stashes request.state.user "so audit helpers see it
    # without per-route plumbing" — the generated blocks never used it.
    req = _FakeRequest({"sub": "u-3"})
    assert resolve_audit_actor(None, req) == "u-3"


def test_explicit_candidate_beats_the_request():
    req = _FakeRequest({"sub": "u-request"})
    assert resolve_audit_actor({"sub": "u-explicit"}, req) == "u-explicit"


def test_genuinely_unauthenticated_work_stays_system():
    # Celery beat, webhook receivers, CLI tools: no request, no principal.
    assert resolve_audit_actor(None, None) == "system"
    assert resolve_audit_actor({}, _FakeRequest(None)) == "system"


def test_empty_claim_values_do_not_become_the_string_none():
    assert resolve_audit_actor({"sub": None, "user_id": ""}) == "system"


# ---------------------------------------------------------------------------
# Source sweep — the regression gate
# ---------------------------------------------------------------------------


def _names_the_audit_helper(expr) -> bool:
    """True if `expr` is *code* naming log_audit_event — never a string.

    Selection used to be ``"log_audit_event" in ast.dump(node)``, and
    ``ast.dump`` of a FunctionDef includes its docstring as a Constant, so
    prose counted as a call. A docstring in api/public_router.py that named
    ``log_audit_event_sync`` to say the helper "audits nothing" was therefore
    scanned as an audit writer and reported as an offender for not holding a
    principal (GDXA-177). Matching identifiers instead of a dump makes the
    gate read code only.

    Deliberately an identifier check and not an ``ast.Call`` check: a handler
    that hands the helper to something else — ``add_task(log_audit_event, ...)``
    — still writes rows and must still be scanned. No such site exists today;
    the check is shaped so one would not slip past.

    What this gate still cannot see, stated so nobody reads the class as
    closed: a handler that audits only through the ``core.audit`` wrappers
    ``audit_or_rollback`` / ``audit_best_effort`` never names this helper, so
    it is not selected at all. 25 functions under routers/, api/ and modules/
    are in that position (measured 2026-09-27), including
    ``api/public_router.py:_audit_public_write`` — the public API's entire
    audit path. None of them would fail the actor check today, so this is a
    coverage gap and not a live defect, but the gate cannot fail for the
    wrapper shape. ``tools/audit_after_commit_scan.py`` already resolves the
    full writer set (``AUDIT_WRITERS``, with a transitive callee closure) and
    is the shape to copy if this is ever widened.
    """
    if isinstance(expr, ast.Name):
        return "log_audit_event" in expr.id
    if isinstance(expr, ast.Attribute):
        return "log_audit_event" in expr.attr
    return False


def _writes_audit_rows(node) -> bool:
    """True if anything in this function's code names the audit helper."""
    return any(_names_the_audit_helper(sub) for sub in ast.walk(node))


def _audit_writing_functions():
    """(file, function, node) for every handler that writes an audit row."""
    for path in _python_sources():
        src = path.read_text()
        if "log_audit_event" not in src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:  # pragma: no cover - would be a hard failure
            pytest.fail(f"{path.name} does not parse: {exc}")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not _writes_audit_rows(node):
                continue
            yield path, node


def _can_resolve_an_actor(node) -> bool:
    """A handler can attribute its audit rows if it holds the principal or the
    request that carries it."""
    args = node.args
    names = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    if "current_user" in names or "user" in names:
        return True
    if "request" in names:
        return True
    # Service-layer helpers do not have a request. They take the actor from
    # their caller instead — `actor: str = SYSTEM_ACTOR` — which is the correct
    # shape for code that can be driven by either a user or a scheduler.
    if any(n == "actor" or n.startswith("actor_") for n in names):
        return True
    # An explicit user_id=... counts: the handler sourced the actor some other
    # way (from the entity it just wrote, or a deliberate SYSTEM_ACTOR for work
    # no human performs). A bare "system" string literal does NOT count — that
    # is indistinguishable from the bug this gate exists to catch.
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and _names_the_audit_helper(sub.func):
            for kw in sub.keywords:
                if kw.arg not in {"user_id", "actor_id"}:
                    continue
                if isinstance(kw.value, ast.Name):
                    # `_audit_user` is the generated block's own variable. It
                    # resolves via resolve_audit_actor(), so it is only as good
                    # as the handler's signature — counting it as "explicit"
                    # would make this gate assert nothing at all, which is
                    # exactly the trap the original bug came from.
                    if kw.value.id == "_audit_user":
                        continue
                    return True
                if isinstance(kw.value, ast.Constant) and kw.value.value in (None, "", "system"):
                    continue
                return True
    return False


def test_every_audit_writing_handler_can_resolve_an_actor():
    """The gate. A handler that writes audit rows must be able to say who did
    it — by holding the principal, or the request that carries it."""
    offenders = [
        f"{path.name}:{node.name}"
        for path, node in _audit_writing_functions()
        if not _can_resolve_an_actor(node)
    ]
    assert not offenders, (
        "These handlers write audit rows but cannot resolve an actor, so every "
        "row they write is attributed to 'system':\n  "
        + "\n  ".join(offenders)
        + "\n\nGive the handler `current_user: dict = Depends(get_current_user)` "
        "or `request: Request` (core.audit reads request.state.user)."
    )


# ---------------------------------------------------------------------------
# The gate's own guard: name the input that still turns it red
# ---------------------------------------------------------------------------
#
# A green scanner proves nothing unless it can fail for the defect it claims to
# catch, and the selection step above was narrowed (GDXA-177) to stop matching
# docstrings. These pin what "narrowed correctly" means: the first three fix the
# selector's behaviour on hand-built handlers, and the fourth is the one that
# notices a narrowing which empties the scan surface instead — without it, the
# whole gate passes while scanning nothing at all.


def _fn(src: str):
    return ast.parse(src).body[0]


#: The original defect, in miniature: a real call, no principal in the
#: signature. This is what must still be reported.
_OFFENDER = '''
async def create_thing(payload: dict, db: Session = Depends(get_db)):
    """Creates a thing."""
    thing = Thing(**payload)
    db.add(thing)
    await log_audit_event(db=db, action="thing_created", entity_type="thing")
'''

#: The same handler with the call removed and the helper named only in prose —
#: what api/public_router.py:_write_errors_as_500 looks like.
_PROSE_ONLY = '''
async def create_thing(payload: dict, db: Session = Depends(get_db)):
    """Stages the row. Auditing is the caller's job via log_audit_event_sync."""
    db.add(Thing(**payload))
'''

#: Both at once. A docstring that mentions the helper must not *excuse* a
#: handler that also calls it — the narrowing has to drop prose, not functions.
_BOTH = '''
async def create_thing(payload: dict, db: Session = Depends(get_db)):
    """Audits via log_audit_event, which this docstring also happens to name."""
    db.add(Thing(**payload))
    await log_audit_event(db=db, action="thing_created", entity_type="thing")
'''

#: The qualified form. Nothing in the tree calls the helper this way today, so
#: without this the ast.Attribute branch of _names_the_audit_helper could be
#: deleted with the whole file still green — i.e. unguarded coverage.
_ATTRIBUTE_CALL = '''
async def create_thing(payload: dict, db: Session = Depends(get_db)):
    db.add(Thing(**payload))
    await audit.log_audit_event(db=db, action="thing_created", entity_type="thing")
'''


def test_the_gate_still_reports_a_handler_that_really_writes_audit_rows():
    node = _fn(_OFFENDER)
    assert _writes_audit_rows(node), "a real call must still be selected"
    assert not _can_resolve_an_actor(node), "no principal in scope — it is an offender"


def test_the_gate_ignores_a_handler_that_only_names_the_helper_in_prose():
    assert not _writes_audit_rows(_fn(_PROSE_ONLY))


def test_a_docstring_mention_does_not_excuse_a_handler_that_also_calls_it():
    node = _fn(_BOTH)
    assert _writes_audit_rows(node)
    assert not _can_resolve_an_actor(node)


def test_a_qualified_call_is_selected_and_judged_like_a_bare_one():
    node = _fn(_ATTRIBUTE_CALL)
    assert _writes_audit_rows(node), "audit.log_audit_event(...) must be selected"
    assert not _can_resolve_an_actor(node), "and judged on its signature like any other"


def test_the_gate_actually_scans_something():
    """The narrowing must drop prose, not the scan surface.

    Every other test in this section builds its own AST, so all of them stay
    green if `_audit_writing_functions` yields nothing — and so does the gate
    itself, which is the failure mode a narrowing introduces. Measured 373
    functions before GDXA-177 and 371 after (the two dropped named the helper
    only in prose: `api/public_router.py:_write_errors_as_500` and
    `routers/timeclock.py:_auto_close_stale_shift`, which audits via
    `audit_or_rollback`). The floor is loose on purpose — it is here to catch a
    selector that collapses, not to pin a number that legitimately drifts.
    """
    assert len(list(_audit_writing_functions())) > 300


def test_no_source_duck_types_the_actor_as_a_dict():
    """The specific broken idiom: `(obj or {}).get('sub')`.

    It looked defensive and did two bad things — returned 'system' whenever the
    locals lookup missed (54 handlers), and raised AttributeError against the
    portal's CustomerUser ORM row, which the surrounding try/except swallowed
    so the audit row was never written at all.

    An earlier version of this test checked for `.get('sub') or` AND
    `_audit_user_obj` in the same file. The fix deleted the first substring, so
    the condition could never match again and the test asserted nothing. Check
    for the idiom itself.
    """
    # Narrow deliberately. `(current_user or {}).get("sub")` on a value that is
    # always a claim dict is fine. The defect is duck-typing a THROWAWAY — `_`
    # or the generated block's `_audit_user_obj` — because those are exactly the
    # values that are empty (-> "system") or an ORM row (-> AttributeError).
    needles = ("(_ or {}).get(", "(_audit_user_obj or {}).get(", "_audit_user_obj or {})")
    offenders = []
    for path in _python_sources():
        src = path.read_text()
        if any(n in src for n in needles):
            offenders.append(str(path.relative_to(_PKG)))
    assert not offenders, (
        "These files still resolve the audit actor by duck-typing a dict, which "
        f"breaks on ORM principals: {offenders}. Use "
        "core.audit.resolve_audit_actor."
    )


def test_deliberate_system_attribution_is_distinguishable_from_omission():
    """SYSTEM_ACTOR must not simply be the string 'system'.

    If it were, nothing could tell 'no human did this' apart from 'this handler
    failed to look' — neither the request-principal fallback nor this gate.
    """
    from gdx_dispatch.core.audit import SYSTEM_ACTOR

    assert SYSTEM_ACTOR == "system", "the stored value must stay 'system'"
    assert SYSTEM_ACTOR is not "system"  # noqa: F632 - identity IS the point


def test_request_fallback_does_not_override_deliberate_system_attribution():
    from gdx_dispatch.core.audit import SYSTEM_ACTOR

    req = _FakeRequest({"sub": "u-1"})
    # A handler that deliberately declares machine attribution keeps it even
    # though an authenticated principal is present on the request.
    assert resolve_audit_actor(SYSTEM_ACTOR, req) == "system"


def test_service_account_principal_is_found_under_its_own_state_key():
    """core/service_accounts.py sets request.state.current_user, not .user."""

    class _SvcState:
        def __init__(self):
            self.current_user = {"sub": "svc-1", "role": "admin"}

    class _SvcRequest:
        def __init__(self):
            self.state = _SvcState()

    assert resolve_audit_actor(None, _SvcRequest()) == "svc-1"


# ---------------------------------------------------------------------------
# Known debt: audit blocks that can never fire
# ---------------------------------------------------------------------------

#: Handlers whose generated audit block reads `_audit_db = locals().get('db')`
#: while the handler has no `db` parameter. The block is guarded by
#: `if _audit_db is not None:` so it silently never runs — these actions write
#: NO audit row at all, which is worse than writing one attributed to "system".
#:
#: Fixing each needs a database session threaded into the handler and the write
#: verified, which is a separate piece of work from actor attribution. This list
#: is pinned so the debt is visible and cannot grow quietly.
#:
#: Note what is in here: every /api/payments endpoint. Money movement is
#: currently unaudited.
KNOWN_DEAD_AUDIT_BLOCKS = {
    "maps.py:geocode_address",
    "maps.py:reverse_geocode",
    "maps.py:optimize_route",
    "maps.py:drive_time",
    "maps.py:check_service_area",
    "pricing.py:patch_pricing_settings",
    "pricing.py:calculate_markup",
    "pricing.py:import_vendor_prices",
    "pricing.py:lock_estimate_prices",
    "pricing.py:set_seasonal_pricing",
    "pricing.py:create_bundle",
    "pricing.py:calculate_bundle_by_id",
    "pricing.py:calculate_bundle",
    "pricing.py:set_customer_rate",
    "pricing.py:set_approval_rule",
    "pricing.py:check_approval",
}


def _dead_audit_blocks() -> set[str]:
    dead = set()
    for path in _python_sources():
        src = path.read_text()
        if "_audit_db = locals().get('db')" not in src:
            continue
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            segment = ast.get_source_segment(src, node) or ""
            if "_audit_db = locals().get('db')" not in segment:
                continue
            names = [
                a.arg
                for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
            ]
            if "db" not in names:
                dead.add(f"{path.name}:{node.name}")
    return dead


def test_no_new_audit_block_is_born_dead():
    """A guarded audit block with no `db` in scope writes nothing, ever.

    This asserts the debt does not GROW. Shrinking it is welcome — remove the
    entry from KNOWN_DEAD_AUDIT_BLOCKS when you thread a session in.
    """
    new = _dead_audit_blocks() - KNOWN_DEAD_AUDIT_BLOCKS
    assert not new, (
        "These handlers have an audit block that can never execute, because "
        "`_audit_db = locals().get('db')` finds nothing:\n  "
        + "\n  ".join(sorted(new))
        + "\n\nGive the handler `db: Session = Depends(get_db)`."
    )


def test_known_dead_audit_block_list_is_not_stale():
    """If someone fixes one, make them delete it from the list."""
    stale = KNOWN_DEAD_AUDIT_BLOCKS - _dead_audit_blocks()
    assert not stale, (
        "These are listed as dead audit blocks but are no longer dead — remove "
        f"them from KNOWN_DEAD_AUDIT_BLOCKS: {sorted(stale)}"
    )
