"""Every server-produced deep link lands on a real SPA route.

Recommendations, next actions and Web Push rows all ship a path the client is
meant to navigate to. Ten of them pointed at paths the router has never had
(`git log -S` on `router/index.js` finds no commit that ever added one) —
`/invoices/<id>`, `/customers/<id>/schedule`, `/jobs/<id>/invoice/new`,
`/team`, `/settings/pricing`, `/reports/revenue`, `/customers/<id>/message`.

None of the ten could be tapped. DashboardView is the single renderer of an
`action_url` (`@click="a.action_url && router.push(a.action_url)"`); it is fed
by `/api/next-actions` and drops every `auto:` row, which is all three
`next_action.py` literals. `/api/recommendations` has no frontend caller at
all. So this was a **latent** class, not a live 404 — which is exactly why the
net matters more than the ten edits: the eleventh rule, or the day either
surface gets a renderer, is when it would have bitten.

The class is *a server-produced deep link whose path is not in the SPA route
table* — not the field name `action_url`. `send_push(url=...)` is the same
defect in a different spelling, so it is discovered here too.

The links are **discovered, not listed**: anyone adding an eleventh rule is
covered without touching this file. The route matcher is the one already used
by the #657 chat-push guard — a second matcher would be a second thing to be
wrong.

What this does NOT cover, and where the same class still lives:
  * a deep link in a **Jinja template** or in **client-side code**. This guard
    reads Python. The class — a produced path that is not in the route table —
    is identical there, and when this file was written two instances were named
    as LIVE, both in the same dead-`/settings/<child>` family as
    `/settings/pricing` above:
      - `templates/onboarding.html:266` `href="/settings/stripe-connect"`,
        the primary CTA on the onboarding Stripe step.
        **Corrected 2026-09-24 (GDXA-24): this one was never live.** The page it
        sat on was rendered only by `core/onboarding.py`'s `ui_router`, which
        `app.py` imported and never mounted — no request could reach it. Calling
        it live overstated a real finding, which is the failure mode worth
        remembering here: the dead-link *shape* was right, the reachability
        claim was asserted rather than checked against the route table. The
        wizard's Python half — router, three routes, validators, the Jinja2
        instance — was deleted on the maintainer's ruling, so nothing renders
        the page and the link is unreachable by construction. The template FILE
        is a separate owner's and is removed under GDXA-25; expect it to still
        be in the tree if you are reading this before that lands. Absence of the
        renderer is held by `test_onboarding_jinja_wizard_retired.py`.
      - `frontend/src/views/BankFeedsView.vue:94`
        `$router.push('/settings/integrations')`, the SimpleFIN "Re-link in
        Settings" button. This one IS live — the button renders on a
        nav-reachable page whenever a SimpleFIN connection is unhealthy — and is
        counted as deferred in the sweep block of the commit that added this
        file. It does not belong to this guard's owner. If you are extending
        this net to `.vue` files, it is the reason to.

What this does NOT prove, so do not read a pass as more than it is:
  * that a path this guard calls dead really is. The SPA router is not the
    whole app: `/docs`, `/pay/<token>` and `/sign/<token>` are server-rendered,
    and `_route_for` returns None for every one. That is a false RED, not a
    false green, and no current link is non-SPA; but if you are sent here to
    "fix" a link that works, check whether the server renders it before
    repointing it. (`/onboarding/<step>` used to head that list as a served
    Jinja wizard. It never was one — the router was unmounted — and as of
    GDXA-24 the wizard is deleted outright, so the path is SPA-only like any
    other. Check the live route table, not this list, which is prose.)
  * that a link the code *assembles* is alive. Discovery reads literals, so a
    path built by `+`, `%`, `.format`, `join` or handed over in a variable is
    invisible — `mobile_chat` ships exactly that shape (`url=tech_url`), and
    it is the #657 test that covers those, at runtime, by monkeypatching
    `send_push`. Pinned as a known hole by
    `test_a_url_the_code_assembles_is_invisible_to_this_guard`;
  * that the receiving view *consumes* the query a link carries — that is the
    #657 class, and it is pinned unevenly: `?job_id=` has a spec
    (InvoiceCreateView.spec.js:146), while `?new=1&customer_id=`, which three
    of these links now depend on, has none. What it does have is a shipping
    caller — `CustomerDetailView.vue:24` pushes that exact pair — so the
    contract is reused rather than invented, but no test holds JobsView to it;
  * that the recipient's role can actually get there. `_route_for` reads the
    path table only; it cannot see `meta.requiresPermission`, so a link to
    `/billing/new` (requires `invoices.write`) passes here and still bounces
    that user to `/access-denied`;
  * that the destination is a page rather than a redirect stub — `/invoices`
    resolves to itself and redirects to `/billing`;
  * that a *static* target is the one being matched. `/billing/new` is shadowed
    by `/billing/:id`, so deleting the static route leaves this green while the
    link opens an invoice whose id is the string "new". The targets this fix
    chose are therefore pinned by name below;
  * that the router's own guards leave the link intact. `router/index.js:525`
    rewrites a technician's `/jobs` to `/mobile/jobs` WITHOUT `query:` — unlike
    the `/jobs/:id` branch at :534 — so `?new=1&customer_id=` is
    silently dropped for that role. Latent while DashboardView is the only
    renderer and drops `auto:` rows; live the day a second one appears.
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import re
from collections import Counter

import pytest

# The matcher, not a copy of it. `_route_for` parses frontend/src/router/index.js
# and returns the route a path resolves to, or None for the catch-all 404.
from test_notification_links_resolve_657 import _route_for, _route_patterns

from gdx_dispatch.core.push_subscriptions import send_push

PKG = pathlib.Path(__file__).resolve().parents[1]

# Discovery reads the parsed module, not its text. A regex over source finds
# the spellings that ship a link —
#   action_url="/x"             kwarg          (core/recommendations.py)
#   "action_url": f"/x"         dict key       (core/next_action.py)
#   existing.action_url = "/x"  attribute      (tasks/billing_followup.py)
#   row["action_url"] = "/x"    dict subscript
#   send_push(..., url="/x")    push deep link (routers/parts_needed.py)
# — and also finds them inside a COMMENT or a docstring, which in a repo this
# comment-heavy means a sentence can redden the gate. `ast` cannot see prose
# at all, gives the line number for free, and covers every spelling by shape
# instead of by another alternation each time one is missed. What stays
# invisible either way: a URL the code assembles rather than writes as a
# literal — no rule does today, and
# `test_a_url_the_code_assembles_is_invisible_to_this_guard` is where that
# limit is recorded rather than left to be rediscovered.

# An f-string keeps its placeholders as `{expr}`; give each one a concrete
# segment before matching the result against the route table.
_PLACEHOLDER = re.compile(r"\{[^}]*\}")
_CONCRETE = "00000000-0000-0000-0000-000000000000"


def _concrete(url: str) -> str:
    return _PLACEHOLDER.sub(_CONCRETE, url)


def _paths_of(node: ast.expr) -> list[str]:
    """Every "/…" string this expression can evaluate to, as a template."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value] if node.value.startswith("/") else []
    if isinstance(node, ast.JoinedStr):
        text = "".join(
            v.value if isinstance(v, ast.Constant) else "{" + ast.unparse(v.value) + "}"
            for v in node.values
        )
        return [text] if text.startswith("/") else []
    if isinstance(node, ast.IfExp):          # action_url=a if cond else "/x"
        return _paths_of(node.body) + _paths_of(node.orelse)
    return []


def _is_action_url_target(node: ast.expr) -> bool:
    return (
        (isinstance(node, ast.Name) and node.id == "action_url")
        or (isinstance(node, ast.Attribute) and node.attr == "action_url")
        or (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "action_url"
        )
    )


def _callee_is(call: ast.Call, name: str) -> bool:
    func = call.func
    return (isinstance(func, ast.Name) and func.id == name) or (
        isinstance(func, ast.Attribute) and func.attr == name
    )


def _urls_in(source: str) -> list[tuple[int, str]]:
    """(line, url) for every deep-link literal in this module's code."""
    found: list[tuple[int, str]] = []

    def take(value: ast.expr) -> None:
        found.extend((value.lineno, u) for u in _paths_of(value))

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.keyword) and node.arg == "action_url":
            take(node.value)
        # The class is a server-produced DEEP LINK, not the field name
        # `action_url`. A Web Push ships one as `send_push(url=...)`, and the
        # service worker navigates to it — `frontend/public/sw.js:33,57` reads
        # `payload.url || '/dashboard'` into `clients.openWindow`. (NOT the
        # NotificationsDrawer, which never reads a server-supplied url: it
        # switches on `n.category` through its own client-side map.) Same
        # defect, different spelling. Matched at the call site,
        # not on a bare `url=` kwarg: `app.py` passes `docs_url="/docs"` and
        # `redoc_url="/redoc"` to FastAPI, which are server config, not links
        # anyone navigates the SPA to.
        elif isinstance(node, ast.Call) and _callee_is(node, "send_push"):
            for kw in node.keywords:
                if kw.arg == "url":
                    take(kw.value)
        elif isinstance(node, ast.Dict):
            # ast.Dict always pairs keys with values (a `**spread` key is None).
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "action_url":
                    take(value)
        # `x.action_url = "/a"` / `row["action_url"] = "/a"` (Assign) and
        # `action_url: str = "/a"` (AnnAssign) — same shape, one branch.
        elif (
            isinstance(node, ast.Assign)
            and any(_is_action_url_target(t) for t in node.targets)
        ) or (
            isinstance(node, ast.AnnAssign)
            and node.value is not None
            and _is_action_url_target(node.target)
        ):
            take(node.value)

    return sorted(found)  # ast.walk is breadth-first; ids must be stable


# Directories with no server Python in them. `frontend/` is skipped for cost
# (node_modules is ~223 MB and holds 0 `.py` today — but it is one `npm i`
# away from holding thousands, and this walk runs on every suite invocation).
_SKIP_DIRS = {"tests", "frontend", "node_modules", ".venv"}


def _discover() -> tuple[list[tuple[str, str]], list[str]]:
    """(where, raw_url) for every deep-link literal the server produces.

    `tests/` is excluded on purpose: fixture rows there are test data, not
    links any user is ever handed.

    A file that will not parse is collected rather than raised: this runs at
    import time, so one unparseable `.py` ANYWHERE under the package — a
    git-ignored local scratch file, the hazard CLAUDE.md already records for
    `docker/demo/` — would otherwise turn all of these tests into a single
    collection error reporting a SyntaxError in an unrelated file. Reported by
    `test_every_source_file_could_be_read`, so it is loud but diagnosable.
    """
    found: list[tuple[str, str]] = []
    unreadable: list[str] = []
    for path in sorted(PKG.rglob("*.py")):
        parts = path.relative_to(PKG).parts
        if _SKIP_DIRS.intersection(parts):
            continue
        rel = path.relative_to(PKG.parent)
        try:
            source = path.read_text()
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append(f"{rel}: {type(exc).__name__}")
            continue
        try:
            urls = _urls_in(source)
        except SyntaxError as exc:
            unreadable.append(f"{rel}: SyntaxError line {exc.lineno}")
            continue
        found.extend((f"{rel}:{line}", url) for line, url in urls)
    return found, unreadable


DISCOVERED, UNREADABLE = _discover()


def test_every_source_file_could_be_read() -> None:
    """Discovery that silently skipped a file would be a guard with a hole."""
    assert UNREADABLE == [], (
        "these files were skipped by discovery, so any dead link in them is "
        f"invisible to this guard: {UNREADABLE}"
    )


# ── the discovery itself has to be right, or the guard is theatre ───────────

def test_discovery_catches_every_spelling_that_ships_a_literal_link() -> None:
    src = (
        'f(action_url="/kwarg")\n'
        'd = {"action_url": f"/dict/{job.id}"}\n'
        'existing.action_url = "/attribute"\n'
        'row["action_url"] = "/subscript"\n'
        'action_url: str = "/annotated"\n'
        'action_url = (\n    f"/wrapped/{x}"\n)\n'
        'f(action_url=a if b else "/ternary")\n'
        'send_push(db, user_id=u, title=t, body=b, url="/push")\n'
        'push_subscriptions.send_push(db, url=f"/push/{job.id}")\n'
    )
    assert [u for _, u in _urls_in(src)] == [
        "/kwarg", "/dict/{job.id}", "/attribute", "/subscript",
        "/annotated", "/wrapped/{x}", "/ternary",
        "/push", "/push/{job.id}",
    ]


def test_a_url_the_code_assembles_is_invisible_to_this_guard() -> None:
    """The one hole, recorded rather than left to be discovered.

    Discovery reads literals. A path built by concatenation, `%`, `.format`,
    `join`, or via a variable is not one, so an eleventh rule written that way
    ships dead and this file stays green. No rule does today — every one of
    the 22 links is written inline, which is what makes the literal net
    sufficient *now*. This test exists so that stops being an accident: if
    someone makes it pass by teaching `_paths_of` one of these shapes, that is
    an improvement, and they will see the assertion that has to change.
    """
    invisible = (
        'f(action_url="/dead/" + str(x))\n'
        'f(action_url="/dead/%s" % x)\n'
        'f(action_url="/dead/{}".format(x))\n'
        'f(action_url="/".join(["", "dead", x]))\n'
        'target = "/dead"\nf(action_url=target)\n'
        'DEAD = "/dead"\nf(action_url=DEAD)\n'
    )
    assert _urls_in(invisible) == []


def test_discovery_ignores_everything_that_is_not_a_link() -> None:
    """Two ways to be wrong: miss a link, or invent one out of prose.

    A source-text regex does the second — this module's own explanatory
    comments name dead paths, and a comment must never redden the gate.
    """
    src = (
        '"""A docstring naming action_url="/team", the old dead link."""\n'
        '# Historical: this was action_url="/settings/pricing" before the fix.\n'
        "action_url=body.action_url\n"                              # not a literal
        "action_url: str\n"                                         # bare field
        "action_url: str | None = None\n"                           # no literal
        "action_url: Mapped[str] = mapped_column(String(500))\n"    # ORM column
        'f(action_url="relative-not-a-path")\n'                     # no leading /
        'f(other_kwarg="/not-an-action-url")\n'
    )
    assert _urls_in(src) == []


def test_the_sweep_actually_found_the_links() -> None:
    """A guard that matches nothing passes for free."""
    found = {url for _, url in DISCOVERED}
    # The live control: /jobs/:id has always resolved (next_action.py:336).
    assert "/jobs/{job.id}" in found
    # Both files this class was found in are represented.
    assert any("core/next_action.py" in w for w, _ in DISCOVERED)
    assert any("core/recommendations.py" in w for w, _ in DISCOVERED)
    # The push spelling is reached too, or the `send_push` branch is dead code.
    assert any("routers/parts_needed.py" in w for w, _ in DISCOVERED)
    # 22 literals on 2026-09-24 (20 action_url + 2 send_push url=). The floor
    # is what turns a broken discovery pass red instead of letting it slip by
    # finding fewer; raise it when rules are added, and only lower it when a
    # rule is deliberately removed.
    assert len(DISCOVERED) >= 22, DISCOVERED


def test_the_static_targets_these_links_point_at_still_exist() -> None:
    """Resolving is not arriving when a dynamic sibling shadows the target.

    `/billing/new` matches `/billing/:id` too, so the parametrised guard above
    would stay green if the static route were deleted — and the link would open
    InvoiceDetailView for an invoice with the id "new". Pin the exact paths.
    """
    paths = {p for p, _ in _route_patterns()}
    for target in (
        "/billing/new",     # send_estimate, invoice_now
        "/billing/:id",     # call_overdue_invoice
        "/jobs",            # schedule_maintenance, annual_maintenance_followup
        "/customers/:id",   # request_review
        "/dispatch",        # technician_overloaded, unassigned_jobs_alert
        "/labor-matrix",    # review_pricing
        "/reports",         # revenue_trend_alert
        "/parts-to-order",  # parts_needed critical-part push
        "/mobile",          # parts_needed tech push
    ):
        assert target in paths, f"{target} is gone from the SPA route table"


# The destination each rule was pointed at on 2026-09-24, chosen with that
# rule's own text in hand. Counted, not just listed: a set would let
# `technician_overloaded` be repointed from /dispatch to /billing — both
# already in the set — and stay green. As a multiset that swap takes /dispatch
# from 2 to 1 and reddens. Keyed by URL rather than line number, so ordinary
# edits above a link do not shift it.
_PINNED: dict[str, Counter[str]] = {
    "gdx_dispatch/core/next_action.py": Counter({
        "/jobs/{job.id}": 1,                     # follow_up_estimate (unchanged)
        "/billing/{inv.id}": 1,                  # call_overdue_invoice
        "/jobs?new=1&customer_id={cid}": 1,      # schedule_maintenance
    }),
    "gdx_dispatch/core/recommendations.py": Counter({
        "/billing/new?job_id={job_id}": 2,           # send_estimate, invoice_now
        "/jobs/{job_id}": 1,                         # check_job_status (unchanged)
        "/jobs?new=1&customer_id={customer_id}": 2,  # annual_maintenance_followup,
                                                     # upsell_maintenance_plan
        "/customers/{customer_id}": 1,               # request_review
        "/dispatch": 2,                              # unassigned_jobs_alert (unchanged),
                                                     # technician_overloaded
        "/jobs?stage=estimate": 1,                   # follow_up_estimates (unchanged —
                                                     # see FOUND_NOT_FILED: JobsView
                                                     # reads no ?stage=)
        "/labor-matrix": 1,                          # review_pricing
        "/billing": 1,                               # unbilled_work_alert (unchanged)
        "/reports": 1,                               # revenue_trend_alert
    }),
}


def test_the_destinations_this_fix_chose_are_still_the_destinations() -> None:
    """"Resolves" is a weak pin: every route in the table satisfies it.

    Repointing `request_review` from `/customers/{id}` to `/dispatch` leaves
    the parametrised guard below entirely green. Changing a destination should
    be a decision, which means editing `_PINNED` in the same commit.
    """
    by_file: dict[str, Counter[str]] = {}
    for where, url in DISCOVERED:
        by_file.setdefault(where.split(":")[0], Counter())[url] += 1

    for filename, pinned in _PINNED.items():
        live = by_file.get(filename, Counter())
        missing = pinned - live          # multiset difference
        assert not missing, (
            f"{filename} no longer ships {dict(missing)} — a destination moved. "
            f"If that was deliberate, update _PINNED. It ships {dict(live)}."
        )


def test_send_pushs_default_url_still_resolves() -> None:
    """`url=` is optional, so this path is a link every bare caller ships.

    Read off the signature, not pinned as the string `/dashboard` in the route
    table: that spelling only catches the *route* being deleted and left the
    default itself free to be repointed at a dead path with this file green.
    Verified by mutation — `url: str = "/totally-dead"` passed 29/29 before
    this test existed.
    """
    default = inspect.signature(send_push).parameters["url"].default
    assert isinstance(default, str) and default.startswith("/"), default
    assert _route_for(default) is not None, (
        f"send_push's default url {default!r} matches no SPA route — every "
        "caller that omits url= sends the user to NotFoundView."
    )


def test_the_route_matcher_can_still_fail() -> None:
    """Imported, so prove here that it is the failing kind."""
    assert _route_for(_concrete("/jobs/{job.id}")) == "/jobs/:id"
    assert _route_for("/no/such/surface") is None
    # A relative child must NOT read as dead — the matcher assembles it from
    # its parent. `/phone-com` is the only block in the router that does this.
    assert _route_for("/phone-com/calls") == "/phone-com/calls"


# ── the guard ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("where", "url"),
    DISCOVERED,
    ids=[f"{w}:{u}" for w, u in DISCOVERED],
)
def test_every_server_produced_deep_link_resolves(where: str, url: str) -> None:
    assert _route_for(_concrete(url)) is not None, (
        f"{where} ships action_url {url!r}, which no SPA route matches — "
        "it lands on NotFoundView. Point it at a real route."
    )
