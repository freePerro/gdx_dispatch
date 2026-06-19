/**
 * EstimatesView — cross-page sort contract.
 *
 * Doug 2026-05-11 (sibling to BillingView fix in commit cccb5639): pre-fix
 * EstimatesView passed `paginatedEstimates` (a 20-row slice) to a sortable
 * DataTable, so clicking Status only sorted the visible 20 of N rows.
 *
 * Fix: controlled-sort via :sortField/:sortOrder/@sort, with sortedEstimates
 * applying the sort to filteredEstimates BEFORE pagination slices it.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(
  join(__dirname, '..', 'EstimatesView.vue'),
  'utf8',
);

describe('EstimatesView — cross-page sort contract', () => {
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

  it('defines a sortedEstimates computed that sorts filteredEstimates', () => {
    const start = SRC.indexOf('const sortedEstimates = computed');
    expect(start).toBeGreaterThan(-1);
    // Bound the window to this computed's body — stop at the next `const `
    // so we don't bleed into the paginatedEstimates declaration.
    const tail = SRC.slice(start);
    const next = tail.indexOf('\nconst ', 1);
    const span = next > 0 ? tail.slice(0, next) : tail.slice(0, 800);
    expect(span).toMatch(/filteredEstimates\.value/);
    expect(span).not.toMatch(/paginatedEstimates/);
  });

  it('paginatedEstimates slices sortedEstimates', () => {
    const start = SRC.indexOf('const paginatedEstimates = computed');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 300);
    expect(span).toMatch(/sortedEstimates\.value\.slice/);
  });

  it('DataTable runs in controlled-sort mode with sort props + @sort wired', () => {
    const tableStart = SRC.indexOf(':value="paginatedEstimates"');
    expect(tableStart).toBeGreaterThan(-1);
    const tagEnd = SRC.indexOf('>', tableStart);
    const tag = SRC.slice(tableStart, tagEnd);
    expect(tag).toMatch(/:sortField="sortField"/);
    expect(tag).toMatch(/:sortOrder="sortOrder"/);
    expect(tag).toMatch(/@sort="onSort"/);
  });
});
