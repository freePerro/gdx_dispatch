// Outstanding-by-age breakdown for the Billing page's Total Outstanding card.
//
// Pure functions over the already-loaded invoice list (GET /api/invoices is
// unpaginated), so the office can re-cut the same number with their own day
// ranges without a round trip. Every receivable passed in lands in exactly one
// row, so the rows add back up to the sum of what the caller passed.
import { stampTime } from "../composables/useFormatters";

export const DEFAULT_BREAKPOINTS = [30, 60, 90];

const DAY_MS = 24 * 60 * 60 * 1000;

function toNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

export function balanceOf(inv) {
  return toNum(inv.balance_due ?? inv.total);
}

// Same predicate as the card's client-side fallback and the server's
// /api/invoices/summary: not Paid/Draft/Void, money still owed.
export function isReceivable(inv) {
  return !["Paid", "Draft", "Void"].includes(inv.status) && balanceOf(inv) > 0;
}

// "30, 60, 90" → [30, 60, 90]. Only whole positive day counts survive: junk,
// zero, negatives ("-5"), decimals ("1.5") and repeats are dropped, so a
// half-typed entry never produces an overlapping or empty-forever row.
export function parseBreakpoints(text) {
  const nums = String(text ?? "")
    .split(/[\s,;]+/)
    .filter((tok) => /^\d+$/.test(tok))
    .map((tok) => parseInt(tok, 10))
    .filter((n) => n > 0 && n < 100000);
  return [...new Set(nums)].sort((a, b) => a - b);
}

function localMidnight(t) {
  const d = new Date(t);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

// Whole days from the basis date to `today`. Negative = not yet reached
// (only possible for a due date). null = the invoice has no such date.
// `today` is a Date or the shop's 'YYYY-MM-DD' — pass the shop's day (#444)
// so ages don't shift with the browser's timezone.
export function ageInDays(inv, basis, today = new Date()) {
  const stamp = basis === "due"
    ? inv.due_date
    : (inv.invoice_date || inv.created_at);
  const t = stampTime(stamp);
  if (t === null) return null;
  const todayT = typeof today === "string" ? stampTime(today) : today.getTime();
  return Math.round((localMidnight(todayT) - localMidnight(t)) / DAY_MS);
}

function rangeLabel(min, max) {
  if (max === null) return `${min}+ days`;
  return `${min}–${max} days`;
}

/**
 * Split outstanding receivables into age rows.
 *
 * breakpoints [30, 60, 90] → 0–30, 31–60, 61–90, 91+. Each age row also
 * carries `cumulative`: everything from 0 days through its upper bound
 * (the "0–60 days" view). Invoices whose basis date hasn't passed get their
 * own row ahead of the age rows; invoices missing the basis date get a row
 * at the end — neither is dropped, so the total always reconciles.
 *
 * With basis "due", an invoice due TODAY is not yet due: the server's Overdue
 * is `due_date < today`, so the age rows on this basis sum to exactly the
 * Overdue card. (Collections' aging counts due-today as 0–30; this view
 * deliberately matches the card the office is looking at instead.)
 */
export function bucketOutstanding(invoices, { breakpoints = DEFAULT_BREAKPOINTS, basis = "invoice", today = new Date() } = {}) {
  const edges = breakpoints.length ? breakpoints : DEFAULT_BREAKPOINTS;
  const ageRows = [];
  // Due basis: day 0 (due today) is "Not yet due", so aged rows start at 1.
  const firstDay = basis === "due" ? 1 : 0;
  let lo = firstDay;
  for (const edge of edges) {
    ageRows.push({ key: `d${lo}_${edge}`, label: rangeLabel(lo, edge), min: lo, max: edge });
    lo = edge + 1;
  }
  ageRows.push({ key: `d${lo}_plus`, label: rangeLabel(lo, null), min: lo, max: null });
  for (const r of ageRows) Object.assign(r, { count: 0, total: 0, invoices: [] });

  const notYetDue = {
    key: "not_yet_due",
    label: basis === "due" ? "Not yet due" : "Dated in the future",
    count: 0, total: 0, invoices: [],
  };
  const noDate = {
    key: "no_date",
    label: basis === "due" ? "No due date" : "No invoice date",
    count: 0, total: 0, invoices: [],
  };

  let total = 0;
  let count = 0;
  for (const inv of invoices || []) {
    if (!isReceivable(inv)) continue;
    const amount = balanceOf(inv);
    const age = ageInDays(inv, basis, today);
    let row;
    if (age === null) row = noDate;
    else if (age < 0 || (basis === "due" && age === 0)) row = notYetDue;
    else row = ageRows.find((r) => age >= r.min && (r.max === null || age <= r.max));
    row.count += 1;
    row.total += amount;
    row.invoices.push({ ...inv, age_days: age });
    total += amount;
    count += 1;
  }

  let running = 0;
  for (const r of ageRows) {
    running += r.total;
    r.cumulative = running;
    r.cumulativeLabel = r.max === null
      ? (basis === "due" ? "All overdue" : "All aged")
      : `${firstDay}–${r.max} days`;
  }
  for (const r of [notYetDue, noDate, ...ageRows]) {
    r.invoices.sort((a, b) => (b.age_days ?? -1) - (a.age_days ?? -1));
  }

  const rows = [
    ...(notYetDue.count ? [notYetDue] : []),
    ...ageRows,
    ...(noDate.count ? [noDate] : []),
  ];
  return { rows, total, count };
}
