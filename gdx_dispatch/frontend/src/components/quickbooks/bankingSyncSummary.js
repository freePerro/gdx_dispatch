/**
 * Build a one-line summary from /api/qb/banking/sync's response.
 *
 * Response shape:
 *   {
 *     accounts:  { created, updated, errors },
 *     purchases: { created, updated, errors },
 *     deposits:  { created, updated, deleted, errors },
 *     transfers: { created, updated, deleted, errors },
 *   }
 *
 * Returns `{ summary, totalErrors }`. Callers pick severity:
 *   totalErrors === 0 → success toast
 *   totalErrors  > 0  → warn toast (per-entity errors listed in audit_logs)
 *
 * Format example:
 *   "Banking since 2026-01-01: purchases +12/~3, deposits +5/-1, transfers +2/2 err"
 *   "+N" = created, "~N" = updated, "-N" = tombstoned, "N err" = upsert errors.
 */
export function buildBankingSyncSummary(result, sinceIso) {
  if (!result || typeof result !== 'object') {
    return { summary: 'Banking synced', totalErrors: 0 };
  }
  const order = [
    ['accounts', 'accounts'],
    ['purchases', 'purchases'],
    ['deposits', 'deposits'],
    ['transfers', 'transfers'],
    ['bill_payments', 'bill pmts'],
    ['sales_receipts', 'sales rcpts'],
    ['refund_receipts', 'refunds'],
    ['journal_entries', 'journals'],
    ['customer_payments', 'cust pmts'],
    ['vendor_credits', 'vendor crs'],
  ];
  const parts = [];
  let totalErrors = 0;
  for (const [key, label] of order) {
    const r = result[key];
    if (!r) continue;
    const c = r.created || 0;
    const u = r.updated || 0;
    const d = r.deleted || 0;
    const e = (r.errors || []).length;
    totalErrors += e;
    if (c + u + d + e === 0) continue;
    const bits = [];
    if (c) bits.push(`+${c}`);
    if (u) bits.push(`~${u}`);
    if (d) bits.push(`-${d}`);
    if (e) bits.push(`${e} err`);
    parts.push(`${label} ${bits.join('/')}`);
  }
  const head = parts.length ? parts.join(', ') : 'no changes';
  const since = sinceIso ? ` since ${sinceIso}` : '';
  return { summary: `Banking${since}: ${head}`, totalErrors };
}
