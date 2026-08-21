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
import { describe, it, expect, beforeEach, vi } from 'vitest';
import {
  LINE_CATEGORY_OPTIONS,
  normalizeToBucket,
  bucketToOption,
  isRenderableOption,
  displayCategoryFor,
  categoryToPricingCategory,
  loadPricingCategories,
  lineCategoryOptions,
  VALID_BUCKETS,
  seededBuckets,
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


describe('loadPricingCategories — widening from the server (F9 / p5)', () => {
  // `/api/catalogs/pricing-categories` exists so an admin-seeded margin tier
  // ("gates") "surfaces everywhere with no code change". Three hardcoded copies
  // of the six options are what it was meant to prevent.
  const BASE = ['doors', 'openers', 'parts', 'labor', 'other'];

  beforeEach(() => {
    // The set is module state; put it back so tests do not leak into each other.
    for (const b of [...VALID_BUCKETS]) {
      if (!BASE.includes(b)) VALID_BUCKETS.delete(b);
    }
    seededBuckets.clear();
  });

  it('adds a seeded bucket the client did not know about', async () => {
    const api = { get: vi.fn().mockResolvedValue([...BASE, 'gates']) };
    await loadPricingCategories(api);
    expect(VALID_BUCKETS.has('gates')).toBe(true);
    // And it becomes a real display option, which is the point.
    expect(displayCategoryFor({ category: 'Gates', pricing_category: 'gates' }))
      .toBe('Gates');
  });

  it('is ADDITIVE — a short response cannot shrink the vocabulary', async () => {
    // The failure that matters. If the server briefly returns a truncated list
    // and the client REPLACED its set, live catalog rows would start bucketing
    // to `other` — the 10-point overcharge this whole plan began with.
    const api = { get: vi.fn().mockResolvedValue(['doors']) };
    await loadPricingCategories(api);
    for (const b of BASE) expect(VALID_BUCKETS.has(b)).toBe(true);
  });

  it('keeps the base set when the call fails', async () => {
    const api = { get: vi.fn().mockRejectedValue(new Error('offline')) };
    await loadPricingCategories(api);
    for (const b of BASE) expect(VALID_BUCKETS.has(b)).toBe(true);
    // 'Hardware' is in the synonym table; 'Rollers' is NOT and relies on the
    // item's pricing_category instead — picking it here was my mistake, not a
    // defect.
    expect(categoryToPricingCategory('Hardware')).toBe('parts');
  });

  it.each(['springs', 'accessories', 'hardware', 'operators', 'tracks', 'cables'])(
    'ADOPTS %p once a tier is seeded, because the backend does',
    async (word) => {
      // p5 refused these, to stop a GET response repricing the live rows
      // labelled with them. The refusal did not achieve that. The backend's
      // `_normalize_to_bucket` checks the valid set BEFORE `_PRICING_SYNONYMS`,
      // so the server re-points the name the moment a tier is seeded — with no
      // guard at all. Refusing here only made the two sides pick DIFFERENT
      // tiers for the same line, and `add_invoice_line` stores the client's
      // number verbatim. Mirroring the backend is the safe behaviour, not the
      // risky one.
      const api = { get: vi.fn().mockResolvedValue([...BASE, word]) };
      await loadPricingCategories(api);
      expect(VALID_BUCKETS.has(word)).toBe(true);
      expect(normalizeToBucket(word)).toBe(word);
      expect(categoryToPricingCategory(word)).toBe(word);
    },
  );

  it.each(['springs', 'accessories', 'hardware', 'operators', 'tracks', 'cables'])(
    'leaves %p mapped by the synonym table until a tier IS seeded',
    async (word) => {
      // Nothing moves on a tenant that has seeded nothing — which is every
      // tenant today: prod has active tier sets for doors/openers/other/parts
      // and nothing else.
      const before = categoryToPricingCategory(word);
      const api = { get: vi.fn().mockResolvedValue([...BASE]) };
      await loadPricingCategories(api);
      expect(VALID_BUCKETS.has(word)).toBe(false);
      expect(categoryToPricingCategory(word)).toBe(before);
    },
  );

  it('still admits a genuinely new type — the endpoint keeps its purpose', async () => {
    const api = { get: vi.fn().mockResolvedValue([...BASE, 'gates']) };
    await loadPricingCategories(api);
    expect(VALID_BUCKETS.has('gates')).toBe(true);
  });

  it('cannot move a row that states its own pricing_category', async () => {
    // THE safety claim, and the reason adopting `springs` is not a repricing.
    // All 78 live Springs-labelled catalog rows carry an explicit
    // pricing_category of `parts` (prod, 2026-08-19), and both sides honour an
    // explicit value ahead of any free-form word. Seed a springs tier and those
    // rows still price as parts.
    const api = { get: vi.fn().mockResolvedValue([...BASE, 'springs']) };
    await loadPricingCategories(api);
    expect(VALID_BUCKETS.has('springs')).toBe(true);

    const liveSpringRow = { category: '2.25" x .250 x 32" Springs', pricing_category: 'parts' };
    expect(categoryToPricingCategory(displayCategoryFor(liveSpringRow))).toBe('parts');
    // And it still READS as Springs to the office, which is why the option exists.
    expect(displayCategoryFor({ category: 'Springs', pricing_category: 'parts' })).toBe('Springs');
  });

  it('records what it adopted, so a seeded tier is inspectable', async () => {
    const api = { get: vi.fn().mockResolvedValue([...BASE, 'gates']) };
    await loadPricingCategories(api);
    expect([...seededBuckets]).toContain('gates');
    // Base buckets are not "seeded" — they were always there.
    for (const b of BASE) expect(seededBuckets.has(b)).toBe(false);
  });

  it('offers each category once, never a duplicate Springs', async () => {
    const api = { get: vi.fn().mockResolvedValue([...BASE, 'springs', 'gates']) };
    await loadPricingCategories(api);
    const values = lineCategoryOptions().map((o) => o.value);
    expect(new Set(values).size).toBe(values.length);
    expect(values.filter((v) => v === 'Springs')).toHaveLength(1);
    expect(values).toContain('Gates');
    // Other is the fallback and sorts last.
    expect(values[values.length - 1]).toBe('Other');
  });

  it('suppresses its own error toast — a missing endpoint is not the operator\'s problem', async () => {
    const api = { get: vi.fn().mockResolvedValue([]) };
    await loadPricingCategories(api);
    expect(api.get).toHaveBeenCalledWith(
      '/api/catalogs/pricing-categories',
      expect.objectContaining({ suppressErrorToast: true }),
    );
  });
});
