/**
 * The QuickBooks sync line shown on invoice and customer detail.
 *
 * Extracted from two identical copies in InvoiceDetailView and
 * CustomerDetailView (2026-08-21). One place, because the wording changes
 * again when QuickBooks is fully retired and two copies is how the pricing
 * category list ended up with four.
 *
 * WHY THE WORDING CHANGED. The dirty state used to render
 * `Synced <when> · changes pending` with `warn` severity. That promised a push
 * that will never happen: QuickBooks is being phased out, the connection has
 * read `auth_state = needs_reconnect` since 2026-08-18, money pulls are paused,
 * and 88 of 355 live invoices carry `qb_dirty = true`. Nothing is pending for
 * any of them.
 *
 * So the dirty state now states a fact — the record changed after its last
 * sync — and drops to `secondary`, because a warning the operator cannot act
 * on is noise. `qb_dirty` defaults TRUE for rows that were never pushed, which
 * is why this is detail-view only; a list-wide chip would be a wall of it.
 */

/**
 * @param {object} row            invoice or customer, as serialized by the API
 * @param {(v: string) => string} formatWhen  view's own timestamp formatter
 * @returns {{label: string, severity: string}}
 */
export function qbSyncLabel(row, formatWhen) {
  const r = row || {};

  // qb_in_quickbooks (from QBEntityMap) is the authoritative "in QB" signal.
  // qb_synced_at is stamped only by the selective-push path and is
  // un-backfilled, so it cannot be read as "not in QB" — a legacy, imported or
  // manually entered record is in QB with a NULL timestamp.
  const inQb = r.qb_in_quickbooks === true;
  const syncedAt = r.qb_synced_at;
  const changedSince = r.qb_dirty !== false; // default-true when absent

  if (!inQb && !syncedAt) {
    return { label: "Never synced", severity: "secondary" };
  }

  if (syncedAt) {
    const when = formatWhen(syncedAt);
    return changedSince
      ? { label: `Last synced ${when} · changed since`, severity: "secondary" }
      : { label: `Synced ${when}`, severity: "success" };
  }

  // In QB but no local push timestamp (legacy sync / import / manual entry).
  return { label: "Synced", severity: "success" };
}
