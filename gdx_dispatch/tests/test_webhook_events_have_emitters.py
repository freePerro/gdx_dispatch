"""Every event in WEBHOOK_EVENTS has an emitter, or a reason it has none (GDXA-968).

The webhook UI offers WEBHOOK_EVENTS as the list to subscribe to. A name in it
that nothing emits is a fake success: the subscription saves, and no delivery
ever comes. `customer.updated` was one until GDXA-968.

The scan reads application source (tests excluded) for a call that passes the
event name as a string literal to `emit_domain_event` or to a function whose
body calls it: `emit_domain_event(db, "invoice.paid", ...)` and the per-router
helpers (`_emit_job_event(db, job, "job.cancelled", tid)`) both count. An event
built at runtime from a variable would not be seen; none is today.

UNEMITTED is the debt, each with its reason. It ratchets both ways: a new
catalogue entry with no emitter is red, and so is an allowlisted entry that has
gained an emitter (delete its line).
"""
from __future__ import annotations

import ast
from pathlib import Path

from gdx_dispatch.routers.webhooks import WEBHOOK_EVENTS

PKG = Path(__file__).resolve().parents[1]

# Catalogued, not emitted. Each belongs to another domain's choke point; the
# reason says where the emit would go.
UNEMITTED = {
    "job.updated": "jobs-dispatch: no emit on the job PATCH paths.",
    "invoice.created": "money-billing: no emit where invoices are created.",
    "invoice.sent": "money-billing: transition_invoice_status emits invoice.paid only.",
    "invoice.overdue": "money-billing: nothing marks an invoice overdue as an event.",
    "payment.succeeded": "money-billing: record_payment and the Stripe webhook emit invoice.paid, not this.",
    "payment.failed": "money-billing: no emit on a failed payment.",
    "appointment.scheduled": "jobs-dispatch: no emit on scheduling.",
    "appointment.confirmed": "jobs-dispatch: no emit on confirmation.",
    "appointment.completed": "jobs-dispatch: job completion emits job.completed, not this.",
}


BUS = "emit_domain_event"


def _callee(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _source_trees(root: Path) -> list[ast.AST]:
    trees = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root).parts
        if rel[0] in {"tests", "frontend", "migrations"} or "node_modules" in rel:
            continue
        try:
            trees.append(ast.parse(path.read_text(encoding="utf-8")))
        except (SyntaxError, UnicodeDecodeError):
            continue
    return trees


def _emitted_event_names(root: Path) -> set[str]:
    trees = _source_trees(root)
    # The bus, plus every function whose own body calls it (`_emit_job_event`).
    # Only those count: the legacy `emit_webhook` writes `webhook_endpoints`,
    # which no subscription reads, so an event passed only to it still never
    # reaches a subscriber.
    emitters = {BUS}
    for tree in trees:
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name != BUS and any(
                isinstance(n, ast.Call) and _callee(n) == BUS for n in ast.walk(fn)
            ):
                emitters.add(fn.name)
    names: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _callee(node) not in emitters:
                continue
            for arg in [*node.args, *(k.value for k in node.keywords)]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    names.add(arg.value)
    return names


def test_every_catalogued_event_has_an_emitter_or_a_reason():
    emitted = _emitted_event_names(PKG)
    missing = [e for e in WEBHOOK_EVENTS if e not in emitted and e not in UNEMITTED]
    assert not missing, (
        f"WEBHOOK_EVENTS offers {missing} but nothing emits them. Add the "
        "emit_domain_event call, or list each in UNEMITTED with its reason."
    )


def test_the_allowlist_holds_only_catalogued_events_still_unemitted():
    emitted = _emitted_event_names(PKG)
    stale = sorted(e for e in UNEMITTED if e in emitted or e not in WEBHOOK_EVENTS)
    assert not stale, f"UNEMITTED lists {stale}, which are emitted now or no longer catalogued: delete them."


def test_customer_updated_is_emitted():
    # The instance GDXA-968 was filed on; read alone, so a later allowlist edit
    # cannot quietly readmit it.
    assert "customer.updated" in _emitted_event_names(PKG)
