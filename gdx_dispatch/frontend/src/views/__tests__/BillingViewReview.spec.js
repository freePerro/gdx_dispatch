/**
 * BillingView — "Review" button on Ready-for-Billing card.
 *
 * Doug 2026-05-10: completed jobs need a chance to have parts/labor added
 * before the office one-clicks "Create Invoice" and ships a wrong total.
 *
 * Pinned:
 *  1. The Ready-for-Billing card renders a Review button per row.
 *  2. Clicking Review pushes to /jobs/{id}.
 *  3. Clicking Create Invoice still works (didn't break the existing path).
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(
  join(__dirname, '..', 'BillingView.vue'),
  'utf8',
);

describe('BillingView Ready-for-Billing — Review button', () => {
  it('Review button is present in the Ready-for-Billing column', () => {
    expect(SRC).toMatch(/data-testid="review-job-before-billing"/);
    expect(SRC).toMatch(/label="Review"/);
  });

  it('Create Invoice button is preserved', () => {
    expect(SRC).toMatch(/data-testid="create-invoice-for-job"/);
    expect(SRC).toMatch(/label="Create Invoice"/);
  });

  it('reviewJob handler navigates to /jobs/{id}', () => {
    // Function must use router.push to /jobs/{id}. Bracket-walk from the
    // declaration so we don't accidentally match an unrelated push.
    const start = SRC.indexOf('function reviewJob');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 400);
    expect(span).toMatch(/router\.push\(\s*`\/jobs\/\$\{[^}]+\}`/);
  });

  it('Both buttons sit in the same Action column (not split apart)', () => {
    // The Action column header should still exist; both buttons should be
    // inside the same column body. Use a coarse-grained string check.
    const actionColumnIdx = SRC.indexOf('header="Action"');
    expect(actionColumnIdx).toBeGreaterThan(-1);
    // Find the next </Column> after the Action header.
    const after = SRC.slice(actionColumnIdx);
    const closeIdx = after.indexOf('</Column>');
    expect(closeIdx).toBeGreaterThan(-1);
    const actionColumnBody = after.slice(0, closeIdx);
    expect(actionColumnBody).toMatch(/review-job-before-billing/);
    expect(actionColumnBody).toMatch(/create-invoice-for-job/);
  });
});

// ---------------------------------------------------------------------------
// Cross-page sort — Doug 2026-05-11.
//
// Before this fix, the DataTable received `paginatedInvoices` (a 20-row slice
// of the filtered set), so clicking the Status column header only sorted the
// visible 20 rows — leaving Paid/Overdue scattered across pages 2+. Fix is
// controlled-sort: DataTable runs in :sortField/:sortOrder mode, @sort updates
// refs, and `sortedInvoices` applies the sort to filteredInvoices BEFORE
// pagination slices it.
// ---------------------------------------------------------------------------
describe('BillingView — cross-page sort contract', () => {
  it('declares sortField + sortOrder refs', () => {
    expect(SRC).toMatch(/const\s+sortField\s*=\s*ref\(/);
    expect(SRC).toMatch(/const\s+sortOrder\s*=\s*ref\(/);
  });

  it('has an onSort handler that updates the sort refs', () => {
    const start = SRC.indexOf('function onSort');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 300);
    expect(span).toMatch(/sortField\.value\s*=/);
    expect(span).toMatch(/sortOrder\.value\s*=/);
  });

  it('defines a sortedInvoices computed that sorts filteredInvoices', () => {
    const start = SRC.indexOf('const sortedInvoices = computed');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 800);
    // Sort works on the full filtered set, not the paginated slice.
    expect(span).toMatch(/filteredInvoices\.value/);
    expect(span).not.toMatch(/paginatedInvoices/);
  });

  it('paginatedInvoices slices sortedInvoices (not filteredInvoices directly)', () => {
    const start = SRC.indexOf('const paginatedInvoices = computed');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 300);
    expect(span).toMatch(/sortedInvoices\.value\.slice/);
  });

  it('DataTable runs in controlled-sort mode with sort props + @sort wired', () => {
    // The Invoice DataTable (the only one with :value="paginatedInvoices") must
    // pass :sortField, :sortOrder, and listen for @sort.
    const tableStart = SRC.indexOf(':value="paginatedInvoices"');
    expect(tableStart).toBeGreaterThan(-1);
    // Walk to the closing `>` of the opening tag.
    const tagEnd = SRC.indexOf('>', tableStart);
    const tag = SRC.slice(tableStart, tagEnd);
    expect(tag).toMatch(/:sortField="sortField"/);
    expect(tag).toMatch(/:sortOrder="sortOrder"/);
    expect(tag).toMatch(/@sort="onSort"/);
  });
});
