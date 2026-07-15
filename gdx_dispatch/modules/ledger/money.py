"""Money arithmetic for the ledger — integer cents only (S3, spec §8).

Two operations, verified against the industry references in the spec
(dinero.js/RubyMoney allocate, Fowler Money, Stripe/Avalara/Xero):

- ``to_cents`` — THE Decimal→cents boundary, ROUND_HALF_UP. Everything past
  this function is ``int``.
- ``allocate`` — sum-preserving largest-remainder split. The invariant is
  ``sum(parts) == total`` — always, by construction, so a proration can never
  create or destroy a cent. Residuals that cannot be attributed (caller-level
  rounding gaps, not allocate's) post to 6990 Rounding Differences.

Floats are banned on money paths (S4 adds the CI lint); both functions raise
``TypeError`` on float input rather than guessing what 0.1 + 0.2 meant.
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from fractions import Fraction


def to_cents(amount: Decimal | int | str) -> int:
    """Convert a decimal dollar amount to signed integer cents, ROUND_HALF_UP.

    Accepts Decimal, int (whole dollars), or str. Rejects float loudly —
    binary floats cannot represent most cent values and must never cross the
    money boundary. Non-finite / unquantizable input (NaN, Infinity, 1e30)
    raises ValueError, never a raw decimal exception.
    """
    if isinstance(amount, bool) or isinstance(amount, float):
        raise TypeError(f"to_cents rejects {type(amount).__name__} — use Decimal or str")
    if isinstance(amount, int):
        return amount * 100
    try:
        dec = Decimal(amount)
        return int(dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100)
    except InvalidOperation as exc:
        raise ValueError(f"not a finite decimal amount: {amount!r}") from exc


def allocate(total_cents: int, weights: Sequence[int | Decimal]) -> list[int]:
    """Split ``total_cents`` proportionally to ``weights``, sum-preserving.

    Largest-remainder method: every part gets its floor share, then the
    leftover cents go one each to the largest fractional remainders (ties
    broken by position — deterministic, so replays allocate identically).

    ``sum(result) == total_cents`` always. Negative totals allocate as the
    negated allocation of the absolute value (a refund prorates exactly like
    the charge it unwinds). Weights must be non-negative and sum > 0.
    """
    if isinstance(total_cents, bool) or not isinstance(total_cents, int):
        raise TypeError("total_cents must be int (cents)")
    if not weights:
        raise ValueError("allocate needs at least one weight")

    # Pure rational arithmetic (audit round 1): Decimal division is bounded
    # by its 28-digit context and FAILS SILENTLY past it — Fraction is exact
    # at any magnitude, so the invariant really is by construction.
    norm: list[Fraction] = []
    for w in weights:
        if isinstance(w, (bool, float)):
            raise TypeError(f"allocate rejects {type(w).__name__} weights — use int or Decimal")
        frac = Fraction(w)
        if frac < 0:
            raise ValueError("weights must be non-negative")
        norm.append(frac)

    weight_sum = sum(norm)
    if weight_sum == 0:
        if total_cents == 0:
            return [0] * len(norm)
        raise ValueError("cannot allocate a nonzero total across all-zero weights")

    if total_cents < 0:
        return [-p for p in allocate(-total_cents, weights)]

    exact = [total_cents * w / weight_sum for w in norm]
    parts = [int(e) for e in exact]  # floor (exact is >= 0)
    leftover = total_cents - sum(parts)
    # leftover cents → largest fractional remainders, position-stable order.
    by_remainder = sorted(
        range(len(norm)), key=lambda i: (exact[i] - parts[i], -i), reverse=True
    )
    for i in by_remainder[:leftover]:
        parts[i] += 1
    if sum(parts) != total_cents:  # unreachable with exact arithmetic; loud if not
        raise ArithmeticError(
            f"allocate invariant violated: {sum(parts)} != {total_cents}"
        )
    return parts
