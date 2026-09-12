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
