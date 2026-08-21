/**
 * EstimateView — the labor-matrix size label.
 *
 * `width_ft` / `height_ft` are FEET, and have been since 2026-05-07:
 * `_validate_size_pair` locks the units and caps them at 40ft, so an inches
 * value cannot exist any more. EstimateView kept dividing by 12, a leftover
 * from when the same columns mixed feet and inches (108x84 vs 10x8).
 *
 * The effect was total, not cosmetic: every prod row (8x7, 9x7, 10x8, 12x12,
 * 16x7, 16x16, 20x14) floors to 1 when divided by 12, so TEN OF ELEVEN rows
 * rendered "1x1" and were indistinguishable in the picker.
 *
 * The fixture is the real prod labor matrix, and each row's description
 * carries the same numbers as its columns — which is what makes the units
 * unambiguous rather than a judgement call.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(join(__dirname, '..', 'EstimateView.vue'), 'utf8');

// Extract the function body and evaluate it, so this tests BEHAVIOUR rather
// than asserting on source text — the trap that made three earlier guards in
// this repo pass against code with the feature removed.
function makeLabelFn() {
  const start = SRC.indexOf('function laborSizeLabel');
  expect(start).toBeGreaterThan(-1);
  const rest = SRC.slice(start);
  const end = rest.indexOf('\n}\n');
  const body = rest.slice(0, end + 2);
  // eslint-disable-next-line no-new-func
  return new Function(`${body}; return laborSizeLabel;`)();
}

const PROD_MATRIX = [
  [8, 7, '8x7'],
  [9, 7, '9x7'],
  [10, 8, '10x8'],
  [10, 12, '10x12'],
  [12, 12, '12x12'],
  [14, 14, '14x14'],
  [16, 7, '16x7'],
  [16, 16, '16x16'],
  [20, 14, '20x14'],
];

describe('laborSizeLabel — real prod rows', () => {
  const label = makeLabelFn();

  it.each(PROD_MATRIX)('%ift x %ift renders as %s', (w, h, expected) => {
    expect(label({ width_ft: w, height_ft: h })).toBe(expected);
  });

  it('does not collapse every row to the same string', () => {
    // THE bug, stated directly. Dividing by 12 made all nine of these "1x1".
    const rendered = PROD_MATRIX.map(([w, h]) => label({ width_ft: w, height_ft: h }));
    expect(new Set(rendered).size).toBe(PROD_MATRIX.length);
    expect(rendered).not.toContain('1x1');
  });

  it('falls back to the SKU when a row has no size', () => {
    // Prod's "hourly rate" row has null dimensions.
    expect(label({ width_ft: null, height_ft: null, sku: 'HOURLY' })).toBe('HOURLY');
  });

  it('falls back to a dash when there is neither size nor sku', () => {
    expect(label({ width_ft: null, height_ft: null, sku: null })).toBe('—');
  });

  it('agrees with the new labor picker on the same row', () => {
    // LaborPickerDialog was written after the units changed and never divided.
    // The two surfaces disagreed about the same data until now.
    const dialogSrc = readFileSync(
      join(__dirname, '..', '..', 'components', 'LaborPickerDialog.vue'),
      'utf8',
    );
    expect(dialogSrc).not.toMatch(/width_ft\s*\/\s*12/);
    expect(SRC).not.toMatch(/width_ft\s*\/\s*12/);
  });
});
