/**
 * SegmentsView — cross-page sort contract on the customers table.
 *
 * Doug 2026-05-11 (sibling to BillingView fix in commit cccb5639): pre-fix
 * SegmentsView passed `paginatedCustomers` (a 25-row slice) to a sortable
 * DataTable, so clicking Name only sorted the visible 25 of N rows.
 *
 * Fix: controlled-sort via :sortField/:sortOrder/@sort, with sortedCustomers
 * applying the sort to the full customers list BEFORE pagination slices it.
 * The sort refs are prefixed `customerSortField` / `customerSortOrder` to
 * distinguish from the segments-list sort PrimeVue already handles
 * (segments DataTable isn't paginated, so its native sort is fine).
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(
  join(__dirname, '..', 'SegmentsView.vue'),
  'utf8',
);

describe('SegmentsView — cross-page customer sort contract', () => {
  it('declares customerSortField + customerSortOrder refs', () => {
    expect(SRC).toMatch(/const\s+customerSortField\s*=\s*ref\(/);
    expect(SRC).toMatch(/const\s+customerSortOrder\s*=\s*ref\(/);
  });

  it('has an onCustomerSort handler that updates the sort refs', () => {
    const start = SRC.indexOf('function onCustomerSort');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 300);
    expect(span).toMatch(/customerSortField\.value\s*=/);
    expect(span).toMatch(/customerSortOrder\.value\s*=/);
  });

  it('defines a sortedCustomers computed that sorts the customers ref', () => {
    const start = SRC.indexOf('const sortedCustomers = computed');
    expect(start).toBeGreaterThan(-1);
    // Bound the window to this computed's body — stop at the next `const `
    // so we don't bleed into the paginatedCustomers declaration.
    const tail = SRC.slice(start);
    const next = tail.indexOf('\nconst ', 1);
    const span = next > 0 ? tail.slice(0, next) : tail.slice(0, 800);
    expect(span).toMatch(/customers\.value/);
    expect(span).not.toMatch(/paginatedCustomers/);
  });

  it('paginatedCustomers slices sortedCustomers', () => {
    const start = SRC.indexOf('const paginatedCustomers = computed');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 300);
    expect(span).toMatch(/sortedCustomers\.value\.slice/);
  });

  it('Customers DataTable runs in controlled-sort mode', () => {
    const tableStart = SRC.indexOf(':value="paginatedCustomers"');
    expect(tableStart).toBeGreaterThan(-1);
    const tagEnd = SRC.indexOf('>', tableStart);
    const tag = SRC.slice(tableStart, tagEnd);
    expect(tag).toMatch(/:sortField="customerSortField"/);
    expect(tag).toMatch(/:sortOrder="customerSortOrder"/);
    expect(tag).toMatch(/@sort="onCustomerSort"/);
  });
});
