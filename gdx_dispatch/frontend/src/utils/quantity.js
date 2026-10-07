/**
 * A line's stored quantity, read as it was recorded (#560).
 *
 * `quantity || 1` turned a stored 0 into 1: a zero-quantity line showed,
 * copied and billed as one unit. The rule is the server's
 * (`core/quantities.recorded_quantity`): a blank quantity is unstated and
 * reads as 1; a recorded 0 is 0.
 *
 * For reading stored lines only. Forms that normalise what someone types for
 * a new line (clamping to at least 1) are a different decision.
 */
export function recordedQuantity(q) {
  // Blank is unstated, so 1. Anything else is returned as the number it is —
  // including 0, and including NaN for a value that is not a number, which
  // shows rather than quietly becoming 1 (the server raises on the same input).
  if (q === null || q === undefined || q === '') return 1;
  return Number(q);
}

/**
 * quantity × unit price in dollars, rounded to the cent exactly as the server
 * rounds it (`_money(Decimal(quantity) * Decimal(unit_price))`, ROUND_HALF_UP).
 *
 * An invoice line's quantity takes two decimals since migration 108, and a
 * float product lands just under a half cent: 2.5 × 33.33 is
 * 83.32499999999999 in JavaScript, so a live preview printed $83.32 for a
 * line the server then billed at $83.33. Both factors are carried as whole
 * hundredths, so the product is an exact integer and the only rounding is the
 * server's. Half rounds away from zero, as ROUND_HALF_UP does on a negative.
 */
export function lineAmount(quantity, unitPrice) {
  const q = Number(quantity);
  const p = Number(unitPrice);
  if (!Number.isFinite(q) || !Number.isFinite(p)) return 0;
  const product = Math.round(q * 100) * Math.round(p * 100); // cents × 100
  const cents = Math.sign(product) * Math.round(Math.abs(product) / 100);
  return cents / 100;
}
