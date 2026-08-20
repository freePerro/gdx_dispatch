/**
 * useLineCategories — contract pins.
 *
 * The fixture below is NOT invented. It is every distinct `category` value on a
 * LIVE prod catalog (2026-08-19: 300 items across five catalogs), paired with the
 * `pricing_category` those rows actually carry.
 *
 * This matters because the bug shipped past a test suite that only ever
 * exercised "Springs" — the 25.7% that already worked. A guard that samples the
 * happy value proves nothing about the 74.3% that rendered blank.
 */
import { describe, it, expect } from 'vitest';
import {
  LINE_CATEGORY_OPTIONS,
  normalizeToBucket,
  bucketToOption,
  isRenderableOption,
  displayCategoryFor,
  categoryToPricingCategory,
} from '../useLineCategories';

// [free-form category, pricing_category, expected display]
const LIVE_PROD_CATALOG = [
  ['Springs', 'parts', 'Springs'],
  ['Operators', 'openers', 'Openers'],
  ['Logic 5 Openers', 'openers', 'Openers'],
  ['MAXUM Openers', 'openers', 'Openers'],
  ['Hinges', 'parts', 'Parts'],
  ['Fasteners', 'parts', 'Parts'],
  ['Accessories', 'parts', 'Parts'],
  ['Horizontals', 'parts', 'Parts'],
  ['Rollers', 'parts', 'Parts'],
  ['Drums', 'parts', 'Parts'],
  ['Cable Sets', 'parts', 'Parts'],
  ['Torsion Bars 16ga', 'parts', 'Parts'],
  ['', 'openers', 'Openers'],
  ['Solid Shafts 1" keyways', 'parts', 'Parts'],
  ['2" Struts', 'parts', 'Parts'],
  ['3" Struts', 'parts', 'Parts'],
  ['Punch Angle', 'parts', 'Parts'],
  ['Commercial Track Sets (2"C/A)', 'parts', 'Parts'],
  ['Bottom Fixtures', 'parts', 'Parts'],
  ['Top Fixtures', 'parts', 'Parts'],
  ['Hardware Boxes', 'parts', 'Parts'],
  ['Decorative Hardware', 'parts', 'Parts'],
  ['Verticals', 'parts', 'Parts'],
  ['PVC', 'parts', 'Parts'],
  ['Misc', 'parts', 'Parts'],
  ['Locks', 'parts', 'Parts'],
  ['Seals', 'parts', 'Parts'],
  ['Bearing Plates', 'parts', 'Parts'],
  ['Operator Brackets', 'parts', 'Parts'],
  ['Pusher Springs', 'parts', 'Parts'],
  // Soft-deleted QuickBooks catalog — unreachable today, live again on restore.
  ['NonInventory', 'parts', 'Parts'],
  ['Service', 'parts', 'Parts'],
];

describe('useLineCategories — every live prod category renders', () => {
  it.each(LIVE_PROD_CATALOG)(
    'category %p (pricing_category %p) displays as %p',
    (category, pricing_category, expectedDisplay) => {
      expect(displayCategoryFor({ category, pricing_category })).toBe(expectedDisplay);
    },
  );

  it('NEVER produces a value the Select cannot render', () => {
    // The whole defect in one assertion: a blank cell is an unrenderable value.
    for (const [category, pricing_category] of LIVE_PROD_CATALOG) {
      const display = displayCategoryFor({ category, pricing_category });
      expect(
        isRenderableOption(display),
        `"${category}" produced "${display}", which is not one of the six options`,
      ).toBe(true);
    }
  });

  it('covers the whole live catalog, not just the convenient row', () => {
    // Regression pin on the fixture itself: if someone trims this list back to
    // "Springs", the bug can ship again unnoticed.
    expect(LIVE_PROD_CATALOG.length).toBeGreaterThanOrEqual(30);
    const springsOnly = LIVE_PROD_CATALOG.filter(([c]) => c === 'Springs');
    expect(springsOnly).toHaveLength(1);
  });

  it('round-trips display -> bucket back to the item\'s own pricing_category', () => {
    // THE load-bearing guard. `bucketForLine` prefers pricing_category when a
    // line has one (invoice CREATE), but a line loaded from a saved invoice has
    // none, so the edit surface maps the DISPLAY string instead. Those two
    // routes must land on the same bucket or the same line prices differently
    // depending on which screen you opened — and the tier is the billed number,
    // because routers/invoices.py stores unit_price verbatim with no engine.
    //
    // Coverage limit, stated rather than implied: the live fixture is 30 `parts`
    // rows and 2 `openers`, so this exercises no `doors`, `labor` or `other`
    // row. A constructed disagreement — category 'Parts' with pricing_category
    // 'doors' — WOULD slip past, because invoice_lines never persists
    // pricing_category so edit-mode can only map the display string. That shape
    // does not exist in any live catalog row today; it is a structural gap, not
    // a passing test pretending to be coverage.
    for (const [category, pricing_category] of LIVE_PROD_CATALOG) {
      const display = displayCategoryFor({ category, pricing_category });
      expect(
        categoryToPricingCategory(display),
        `"${category}" displays as "${display}", which buckets to `
          + `"${categoryToPricingCategory(display)}" but the item says `
          + `"${pricing_category}" — create and edit would disagree`,
      ).toBe(pricing_category);
    }
  });
});

describe('categoryToPricingCategory — the 142-item mispricing (F4)', () => {
  // Prod retail tiers: parts 50/40/35/25 vs other 60/50/35/25. Bucketing a
  // parts row as `other` over-prices it by 10 points below $500 — a $50 roller
  // at $125 instead of $100.
  it.each([
    ['Accessories', 'parts'],
    ['Hardware', 'parts'],
    ['Operators', 'openers'],
    ['Operator', 'openers'],
    ['Springs', 'parts'],
    ['Spring', 'parts'],
    ['Tracks', 'parts'],
    ['Cables', 'parts'],
    ['Remotes', 'parts'],
    ['Keypads', 'parts'],
    ['Parts', 'parts'],
    ['Doors', 'doors'],
    ['Door', 'doors'],
    ['Openers', 'openers'],
    ['Other', 'other'],
  ])('%p buckets to %p', (category, bucket) => {
    expect(categoryToPricingCategory(category)).toBe(bucket);
  });

  it('keeps labor out of the tier engine', () => {
    // The engine rejects category='labor'; labor prices from the matrix.
    expect(categoryToPricingCategory('Labor')).toBe('labor');
    expect(normalizeToBucket('labor')).toBeNull();
    expect(normalizeToBucket('Labor')).toBeNull();
  });

  it('falls back to other for genuinely unknown words', () => {
    expect(categoryToPricingCategory('Flanges')).toBe('other');
    expect(categoryToPricingCategory('')).toBe('other');
    expect(categoryToPricingCategory(null)).toBe('other');
  });
});

describe('useLineCategories — helpers', () => {
  it('bucketToOption title-cases known buckets and rejects the rest', () => {
    expect(bucketToOption('openers')).toBe('Openers');
    expect(bucketToOption('parts')).toBe('Parts');
    expect(bucketToOption('springs')).toBeNull(); // not a bucket — display only
    expect(bucketToOption('')).toBeNull();
  });

  it('isRenderableOption matches the exported option list', () => {
    for (const opt of LINE_CATEGORY_OPTIONS) {
      expect(isRenderableOption(opt.value)).toBe(true);
    }
    expect(isRenderableOption('Accessories')).toBe(false);
    expect(isRenderableOption(null)).toBe(false);
  });
});
