"""The pay page shows an already-settled invoice as SUCCESS, not a red failure.

GDXA-131, the presentation half of GDXA-71/GDXA-84. The money half shipped in
#794: `create-intent` now asks Stripe whether money already settled on this
invoice, records what it finds, and answers 409 with
`core.payments.ALREADY_RECORDED_DETAIL` when the recovery left nothing to
charge. `payment_form.html` then took that sentence — *"Your payment already
went through ... you don't need to pay again"* — straight into
`showError(...)`, which writes `<div id="payment-errors" class="alert error">`.
A customer we had just recovered money from was told in red that their payment
failed, under a Pay button still offering to take it again.

That sentence is not the only one of its shape. `create-intent` has three
distinct 409s that all mean *stop, your money is handled*:

  - the recovered-card sentence above (`_settle_unrecorded_intents`);
  - ``"This invoice has no balance due."`` — the webhook won the race, or the
    office recorded the payment, so `_resolve_public_invoice` refuses first;
  - the two `_refuse_if_ach_processing` sentences (M16), which the page ALREADY
    renders in the green box when they are true at page load — so painting the
    identical words red when the same state is reached mid-session is the page
    contradicting itself.

``"This invoice has been cancelled."`` is deliberately NOT in that set: a void
invoice is also unpayable, but it is not money the customer has paid, and
saying so in green would be a false statement.

The discriminator is the sentence, because a plain-string `detail` is all the
browser gets from these 409s. That is a two-ended contract with a silent
failure mode — reword either end and the page quietly goes back to the red box.
These lock both ends:

  - every sentence the page treats as success is one `core/payments.py`
    actually sends, compared as VALUES after a real Jinja render (so an
    autoescape change that turned the apostrophes into entities inside
    `<script>` would also be caught, which a grep of the template source would
    not);
  - every handler that awaits `createIntent` consults that discriminator
    before it paints a failure — the sweep guard, so a fourth caller added
    later cannot inherit the defect silently;
  - `createIntent` carries the status and the raw detail across the throw,
    which is what makes the check above possible at all. A bare
    `new Error(detail)` loses both, and that is exactly how the bug happened.

These are STATIC assertions over the rendered page: they are the drift alarm
for the string contract, and they do not execute the predicate. The behaviour
was proven separately in real Chrome against the rendered template with only
Stripe and `fetch` stubbed (GDXA-131): pre-fix red box + live Pay button;
post-fix green box, no pay affordance and no stale balance figure left; and a
genuine decline, a 402, and a 409 carrying an out-of-set sentence all still
red.

The durable fix is a structured `detail`: `core/error_handler.py` serializes
`exc.detail` verbatim (only `exc.headers` is dropped), and the repo already
ships `detail={"code": ...}` on this surface — `routers/invoices.py`'s
`DUPLICATE_PAYMENT_CODE` and its `nothing_remaining` 409. That change belongs
in `core/payments.py`, which money-billing owns.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from gdx_dispatch.core.payments import ALREADY_RECORDED_DETAIL

PAYMENTS_SRC = Path(__file__).resolve().parents[1] / "core" / "payments.py"


class _Invoice:
    """Enough of an Invoice for the template; the pay page reads no more."""

    invoice_number = "INV-0001"
    status = "sent"
    balance_due = 200.00
    public_token = "tok_test"


@pytest.fixture(scope="module")
def rendered() -> str:
    """The page as a customer's browser receives it, via the app's own Jinja."""
    from gdx_dispatch.core.payments import templates

    return templates.get_template("payment_form.html").render(
        invoice=_Invoice(),
        ach_processing=None,
        stripe_publishable_key="pk_test",
        company_name="Test Co",
        surcharge_rate=0.0,
        surcharge_percent_label="",
        job_photos=[],
    )


def _js_strings(expr: str) -> list[str]:
    """Split a JS expression of double-quoted literals joined by `+`.

    Each `+`-joined run becomes one value, so `SETTLED_DETAILS`'s wrapped
    entries come back as the whole sentence rather than as fragments.
    """
    out, current = [], []
    for token in re.finditer(r'"((?:[^"\\]|\\.)*)"|([+,\[\]])', expr):
        literal, punct = token.group(1), token.group(2)
        if literal is not None:
            current.append(literal.replace('\\"', '"').replace("\\\\", "\\"))
        elif punct != "+" and current:
            out.append("".join(current))
            current = []
    if current:
        out.append("".join(current))
    return out


def _settled_details(rendered: str) -> list[str]:
    m = re.search(r"const SETTLED_DETAILS\s*=\s*\[(.*?)\n  \];", rendered, re.S)
    assert m, "the pay page no longer declares SETTLED_DETAILS"
    body = m.group(1)
    # The first entry is the constant by name, not a literal.
    assert "ALREADY_RECORDED_DETAIL," in body, (
        "the recovered-card sentence is no longer in the settled set"
    )
    return [ALREADY_RECORDED_DETAIL, *_js_strings(body)]


def test_the_page_pins_the_server_s_recovered_card_sentence(rendered):
    """The one sentence that IS an importable constant, compared as values.

    Fails if `ALREADY_RECORDED_DETAIL` is reworded in `core/payments.py`, if the
    copy in the template is reworded, or if rendering mangles it.
    """
    # Matches the literal-and-`+` sequence only, never up to the first `;` —
    # the sentence itself contains one ("paid in full; you don't need...").
    m = re.search(
        r'const ALREADY_RECORDED_DETAIL\s*=\s*'
        r'((?:\s*"(?:[^"\\]|\\.)*"\s*\+?)+)\s*;',
        rendered,
    )
    assert m, "the pay page no longer pins the already-recorded sentence"
    assert _js_strings(m.group(1)) == [ALREADY_RECORDED_DETAIL]


def test_every_sentence_the_page_calls_success_is_one_the_server_sends(rendered):
    """No phantom strings: each is a literal `core/payments.py` can answer with.

    The other three 409s are inline literals rather than constants, so they are
    pinned against the module's source. Reword one there and this goes red
    instead of the page silently dropping back to the red box for that case.
    """
    src = PAYMENTS_SRC.read_text(encoding="utf-8")
    details = _settled_details(rendered)
    assert len(details) == 4, f"expected 4 settled sentences, got {details}"
    for sentence in details:
        # Wrapped across source lines in payments.py; compare on collapsed
        # whitespace so the pin survives reflowing but not rewording.
        needle = " ".join(sentence.split())
        haystack = " ".join(re.sub(r'"\s*\n\s*"', "", src).split())
        assert needle in haystack, (
            f"the pay page treats this as success but core/payments.py never "
            f"sends it:\n  {sentence!r}"
        )


def test_a_cancelled_invoice_is_not_called_a_success(rendered):
    """The deliberate exclusion, asserted as an ABSENCE.

    A void invoice is unpayable but unpaid; the green box would lie about it.
    """
    details = _settled_details(rendered)
    assert "This invoice has been cancelled." not in details


def _script(rendered: str) -> str:
    blocks = re.findall(r"<script>(.*?)</script>", rendered, re.S)
    assert blocks, "the pay page has no inline script"
    return max(blocks, key=len)


def _handlers_awaiting_create_intent(script: str) -> list[str]:
    """Each `try { ... } catch (err) { ... }` whose try body mints an intent.

    Brace-matched from the `try {`, so a nested block cannot end it early.
    """
    out: list[str] = []
    for start in (m.end() - 1 for m in re.finditer(r"\btry\s*\{", script)):
        depth, i = 0, start
        while i < len(script):
            if script[i] == "{":
                depth += 1
            elif script[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = script[start : i + 1]
        if "await createIntent(" not in body:
            continue
        tail = script[i + 1 :]
        cm = re.match(r"\s*catch\s*\(\s*err\s*\)\s*\{", tail)
        assert cm, "a createIntent try-block with no `catch (err)` — check by hand"
        cstart = cm.end() - 1
        depth, j = 0, cstart
        while j < len(tail):
            if tail[j] == "{":
                depth += 1
            elif tail[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(tail[cstart : j + 1])
    return out


def test_every_create_intent_handler_checks_before_it_paints_a_failure(rendered):
    """The sweep guard: the class is *any* handler that can receive the 409.

    `createIntent` is shared by the one-step card submit, the surcharge step-2
    re-POST and the ACH "Link bank account" step, and the settled 409s can
    answer any of them. A fourth caller added later with a bare
    `showError(err.message)` fails here rather than in front of a customer.
    """
    catches = _handlers_awaiting_create_intent(_script(rendered))
    assert len(catches) == 3, (
        f"expected 3 createIntent handlers (card, surcharge step 2, ACH), found "
        f"{len(catches)} — a caller was added or removed; give it the same guard"
    )
    for body in catches:
        assert "isAlreadySettled(err)" in body, (
            "a createIntent failure is painted as an error without first asking "
            "whether the invoice is in fact already settled:\n" + body
        )
        assert body.index("isAlreadySettled(err)") < body.index("showError("), (
            "the settled check must run BEFORE showError:\n" + body
        )


def test_create_intent_carries_the_status_across_the_throw(rendered):
    """Without this the check above cannot work — 409 is lost at the throw."""
    script = _script(rendered)
    m = re.search(r"async function createIntent\(.*?\n  \}", script, re.S)
    assert m, "createIntent is no longer where this test can read it"
    body = m.group(0)
    assert "error.status = resp.status;" in body
    assert "error.detail = " in body
    assert "throw new Error(" not in body, (
        "a bare `throw new Error(detail)` loses the status and the raw detail — "
        "that is the shape this bug had"
    )


def test_a_settled_invoice_is_not_left_payable(rendered):
    """Green words over a live Pay button would still invite a second charge."""
    script = _script(rendered)
    m = re.search(r"function finishSettled\(message\) \{(.*?)\n  \}", script, re.S)
    assert m, "the pay page has no finishSettled()"
    body = m.group(1)
    assert "finishCard(message)" in body, "no green box / card form left showing"
    assert "finishAch()" in body, "the ACH forms would survive a settled 409"
    assert 'getElementById("invoice-total")' in body, (
        "the balance figure would still sit above 'you don't need to pay again'"
    )


def test_the_balance_figure_has_the_handle_that_path_needs(rendered):
    """The Jinja side of the line above — an id the JS can actually find."""
    assert re.search(
        r'<div class="invoice-total" id="invoice-total">', rendered
    ), "the balance figure lost the id finishSettled() removes it by"
