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
            body = ast.dump(node)
            if "log_audit_event" not in body:
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
        if isinstance(sub, ast.Call) and "log_audit_event" in ast.dump(sub.func):
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
    "communications.py:send_sms",
    "communications.py:sms_webhook",
    "communications.py:send_email",
    "communications.py:send_communication",
    "communications.py:add_to_dnc",
    "communications.py:remove_from_dnc",
    "maps.py:geocode_address",
    "maps.py:reverse_geocode",
    "maps.py:optimize_route",
    "maps.py:drive_time",
    "maps.py:check_service_area",
    "payments.py:payment_intent",
    "payments.py:setup_intent",
    "payments.py:ach_setup",
    "payments.py:remove_payment_method",
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
