/**
 * useLineCategories — one place that decides a line's DISPLAY category and its
 * PRICING bucket, shared by every surface that edits line items.
 *
 * Why this exists (2026-08-19 audit, docs/design/invoice-create-line-editor-parity-plan.md):
 *
 * `custom_catalog_items.category` is FREE-FORM by design and documented as
 * "independent of pricing_category" (models/tenant_models.py). On prod only 77
 * of the 300 items a picker can offer carry a string that matches one of the six
 * Select options — the other 223 are things like `3" Struts`,
 * `Solid Shafts 1" keyways`, `Logic 5 Openers`. A PrimeVue Select whose model
 * value matches no option renders its PLACEHOLDER, so those lines showed a blank
 * Category cell even though the value was stored fine.
 *
 * Worse, the blank fed `categoryToPricingCategory()` — a weaker duplicate of the
 * backend's `_derive_pricing_category` that only knew `springs→parts`. 142 live
 * `parts` items fell through to the `other` tier, which runs 10 points richer
 * below $500: a $50 roller priced $125 instead of $100. That is the bug this
 * module exists to kill, and it is why the synonym table below is a deliberate
 * MIRROR of `routers/catalog.py:_PRICING_SYNONYMS` rather than a fresh guess.
 * If you change one side, change both — a second copy of the category convention
 * is exactly how the two drifted apart the first time.
 */

// The six display options. "Springs" is display-only: there is no `springs`
// pricing bucket (springs price as `parts`), but the office thinks in springs
// and 77 catalog rows are labelled that way, so the option stays.
export const LINE_CATEGORY_OPTIONS = Object.freeze([
  { label: 'Doors', value: 'Doors' },
  { label: 'Openers', value: 'Openers' },
  { label: 'Springs', value: 'Springs' },
  { label: 'Labor', value: 'Labor' },
  { label: 'Parts', value: 'Parts' },
  { label: 'Other', value: 'Other' },
]);

// Engine buckets — the BASE set from routers/catalog.py:_VALID_PRICING_CATEGORIES.
//
// This is the OFFLINE FALLBACK, not the authority. `_valid_pricing_categories`
// unions this base with every `pricing_category` that has an active tier set,
// so an admin who seeds a "gates" tier makes `gates` valid server-side. Call
// `loadPricingCategories()` once at mount to widen the client to match; until
// it resolves (or if it fails) these five keep every existing behaviour intact.
const BASE_BUCKETS = ['doors', 'openers', 'parts', 'labor', 'other'];

export const VALID_BUCKETS = new Set(BASE_BUCKETS);

/**
 * Widen the client's bucket set from the server.
 *
 * The endpoint exists precisely so a new type "surfaces everywhere with no code
 * change" — three hardcoded copies of the six options is what it was meant to
 * prevent, and this module was the fourth until now.
 *
 * Deliberately ADDITIVE: the server's list is unioned in rather than replacing
 * the base. A transient empty/short response must not be able to shrink the
 * client's vocabulary and start bucketing live catalog rows to `other`, which
 * is the 10-point overcharge this whole plan started from.
 */
export async function loadPricingCategories(api) {
  try {
    const rows = await api.get('/api/catalogs/pricing-categories', {
      suppressErrorToast: true,
    });
    if (!Array.isArray(rows) || !rows.length) return VALID_BUCKETS;
    for (const r of rows) {
      const b = String(r || '').trim().toLowerCase();
      if (!b) continue;
      // Never admit a name the SYNONYM TABLE already settles.
      //
      // `normalizeToBucket` checks VALID_BUCKETS *before* PRICING_SYNONYMS, so
      // admitting one of these flips its mapping. `springs` is the obvious
      // case — 77 live catalog rows are labelled Springs and price off `parts`;
      // admitting it would reprice all 77 from a GET response. But it is a
      // CLASS, not an instance: `accessories`, `hardware`, `operators`,
      // `tracks`, `cables`, `remotes` and `keypads` all collide the same way,
      // and `Accessories` and `Operators` are live values in prod
      // `invoice_lines` today.
      //
      // A genuinely new type (`gates`) still surfaces with no code change,
      // which is the endpoint's purpose. Only already-settled names are held,
      // and changing one should be a deliberate edit here rather than a
      // payload silently repricing existing rows.
      if (PRICING_SYNONYMS[b]) continue;
      VALID_BUCKETS.add(b);
    }
  } catch {
    // Offline fallback is the base set — the behaviour that shipped in p1.
  }
  return VALID_BUCKETS;
}

/**
 * Display options for the Category select, derived from the buckets.
 *
 * "Springs" is display-only and has no bucket of its own (springs price as
 * `parts`), so it is kept explicitly. Everything else is title-cased from a
 * real bucket, which is what makes an admin-seeded type appear in the dropdown
 * without a code change.
 */
export function lineCategoryOptions() {
  // Springs and Other are appended explicitly, so exclude them from the bucket
  // sweep or the list ships duplicates.
  const seen = new Set(['springs', 'other']);
  const opts = [];
  for (const b of VALID_BUCKETS) {
    if (seen.has(b)) continue;
    seen.add(b);
    const label = b.charAt(0).toUpperCase() + b.slice(1);
    opts.push({ label, value: label });
  }
  opts.push({ label: 'Springs', value: 'Springs' });
  // Other sorts last: it is the fallback, not something anyone picks first.
  opts.sort((a, b) => a.value.localeCompare(b.value));
  opts.push({ label: 'Other', value: 'Other' });
  return opts;
}

// Free-form word → bucket, for words that are not simply a singular of the
// bucket. Mirrors routers/catalog.py:_PRICING_SYNONYMS exactly.
const PRICING_SYNONYMS = {
  operator: 'openers', operators: 'openers',
  remote: 'parts', remotes: 'parts', keypad: 'parts', keypads: 'parts',
  accessory: 'parts', accessories: 'parts', hardware: 'parts',
  spring: 'parts', springs: 'parts', track: 'parts', tracks: 'parts',
  cable: 'parts', cables: 'parts', part: 'parts',
};

/**
 * Map one free-form word to a valid (non-labor) pricing bucket, or null.
 * Exact match, then singular→plural, then domain synonyms — the same three
 * steps as the backend's `_normalize_to_bucket`.
 *
 * `labor` is never returned: labor prices from the LaborPriceItem matrix, not
 * the tier engine (which rejects category='labor'), so a labor line must not be
 * handed to `findTierMargin`.
 */
export function normalizeToBucket(freeForm) {
  const c = String(freeForm || '').trim().toLowerCase();
  if (!c) return null;
  if (VALID_BUCKETS.has(c) && c !== 'labor') return c;
  if (VALID_BUCKETS.has(`${c}s`) && `${c}s` !== 'labor') return `${c}s`;
  return PRICING_SYNONYMS[c] || null;
}

/** 'openers' → 'Openers'. Returns null for anything not a known bucket. */
export function bucketToOption(bucket) {
  const b = String(bucket || '').trim().toLowerCase();
  if (!b || !VALID_BUCKETS.has(b)) return null;
  return b.charAt(0).toUpperCase() + b.slice(1);
}

/**
 * True when a stored value would actually render in the Select.
 * Accepts both option shapes: LineItemEditor passes {label,value} objects,
 * EstimateView passes a plain string array.
 */
export function isRenderableOption(value, options = lineCategoryOptions()) {
  if (value == null || value === '') return false;
  return options.some((o) => (o.value ?? o) === value);
}

/**
 * Decide the DISPLAY category for a catalog item being turned into a line.
 *
 * Order matters, and it is not the obvious one:
 *
 *  1. The item's own words, when they already ARE an option. This is what keeps
 *     the 77 `Springs` rows reading "Springs" instead of being flattened to the
 *     "Parts" bucket they price from — more specific, still priced correctly.
 *  2. `pricing_category`, which every live catalog row carries and which is
 *     correct on all of them. This is the workhorse: `Logic 5 Openers` and
 *     `MAXUM Openers` are multi-word and match nothing, but both carry
 *     `openers`, so both land on "Openers".
 *  3. The synonym table, for rows with no pricing_category.
 *  4. "Other" — matching the backend's own fallback rather than EstimateView's
 *     old `"Parts"` guess, which would silently mis-bucket a door.
 *
 * Note on step 4, because it cuts toward the customer: on this tenant the
 * `other` tier is the RICHEST (60/50/35/25 vs parts 50/40/35/25), so an item
 * that reaches the fallback is priced HIGH, not safe. Backend parity is the
 * reason it stays — the two sides disagreeing is the bug this module exists to
 * end — but it is only correct while no live catalog row needs it. All 300
 * currently carry a usable pricing_category, and the spec pins that.
 */
export function displayCategoryFor(item, options = lineCategoryOptions()) {
  const raw = String(item?.category || '').trim();
  if (raw) {
    const titled = raw.charAt(0).toUpperCase() + raw.slice(1).toLowerCase();
    if (isRenderableOption(titled, options)) return titled;
  }
  const fromPricing = bucketToOption(item?.pricing_category);
  if (fromPricing && isRenderableOption(fromPricing, options)) return fromPricing;
  const normalized = bucketToOption(normalizeToBucket(raw));
  if (normalized && isRenderableOption(normalized, options)) return normalized;
  return 'Other';
}

/**
 * Display category → engine pricing bucket, for the tier lookup.
 *
 * Replaces the old local `categoryToPricingCategory`, which returned 'other' for
 * anything outside the six lowercase bucket names. Legacy invoice lines already
 * carry free-form values (`Accessories`, `Operators`, `Seals` are all in prod
 * `invoice_lines`), so running the synonym table here fixes the tier for those
 * rows too, not just for fresh catalog adds.
 */
export function categoryToPricingCategory(category) {
  const c = String(category || '').trim().toLowerCase();
  if (!c) return 'other';
  if (c === 'labor') return 'labor';
  return normalizeToBucket(c) || 'other';
}
