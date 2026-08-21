/**
 * The client/server MIRROR, pinned.
 *
 * `useLineCategories` opens by warning that a second copy of the category
 * convention "is exactly how the two drifted apart the first time" — and then
 * p5 drifted them apart again, deliberately, by refusing to adopt any bucket
 * name the synonym table already knew. Nothing caught it, because every test
 * asserted the client against ITSELF.
 *
 * This file asserts the client against `routers/catalog.py`. Change one side
 * and it fails, which is the only mechanism that keeps a mirror a mirror.
 *
 * Why the divergence mattered: `add_invoice_line` stores the client's
 * `unit_price` verbatim — there is no server-side repricing. So when the two
 * sides disagree about which tier a line belongs to, the client's answer is the
 * one that reaches the customer's invoice.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import {
  VALID_BUCKETS,
  normalizeToBucket,
  loadPricingCategories,
  seededBuckets,
} from '../useLineCategories.js';

const PY = readFileSync(
  join(__dirname, '..', '..', '..', '..', 'routers', 'catalog.py'),
  'utf8',
);
const JS = readFileSync(join(__dirname, '..', 'useLineCategories.js'), 'utf8');

/** The full body of a top-level Python def — up to the next one. */
function pyFunc(name) {
  const at = PY.indexOf(`def ${name}(`);
  expect(at, `${name} not found`).toBeGreaterThan(-1);
  const rest = PY.slice(at);
  const next = rest.indexOf('\ndef ', 1);
  return next === -1 ? rest : rest.slice(0, next);
}

/** Pull a `name = { "a": "b", ... }` / `name = { a: 'b', ... }` table into pairs. */
function parseTable(src, marker) {
  const at = src.indexOf(marker);
  expect(at, `${marker} not found — did it get renamed?`).toBeGreaterThan(-1);
  const body = src.slice(at + marker.length, src.indexOf('}', at));
  const pairs = {};
  for (const m of body.matchAll(/["']?([A-Za-z_]+)["']?\s*:\s*["']([a-z]+)["']/g)) {
    pairs[m[1]] = m[2];
  }
  return pairs;
}

describe('pricing bucket parity — client mirrors routers/catalog.py', () => {
  const BASE = ['doors', 'openers', 'parts', 'labor', 'other'];

  beforeEach(() => {
    for (const b of [...VALID_BUCKETS]) if (!BASE.includes(b)) VALID_BUCKETS.delete(b);
    seededBuckets.clear();
  });

  it('the synonym tables are identical, key for key', () => {
    const py = parseTable(PY, '_PRICING_SYNONYMS = {');
    const js = parseTable(JS, 'const PRICING_SYNONYMS = {');
    expect(Object.keys(py).length).toBeGreaterThan(10); // parser actually parsed
    expect(js).toEqual(py);
  });

  it('the base bucket sets are identical', () => {
    const pyBase = [...PY.matchAll(/_VALID_PRICING_CATEGORIES = \{([^}]+)\}/g)][0][1]
      .match(/"([a-z]+)"/g).map((q) => q.replaceAll('"', ''));
    expect([...pyBase].sort()).toEqual([...BASE].sort());
  });

  it('both check the valid set BEFORE the synonym table', async () => {
    // The ordering p5 inverted. In Python:
    //     if c in valid ...            <-- first
    //     return _PRICING_SYNONYMS.get(c)
    const body = pyFunc('_normalize_to_bucket');
    expect(body).toContain('_PRICING_SYNONYMS');
    expect(body.indexOf('in valid')).toBeLessThan(body.indexOf('_PRICING_SYNONYMS'));

    // And the client agrees, behaviourally: seed a name the synonym table also
    // knows, and the bucket must win — same as the server.
    expect(normalizeToBucket('accessories')).toBe('parts'); // unseeded
    await loadPricingCategories({ get: vi.fn().mockResolvedValue([...BASE, 'accessories']) });
    expect(normalizeToBucket('accessories')).toBe('accessories'); // seeded
  });

  it('singular→plural works for a seeded type on both sides', async () => {
    expect(pyFunc('_normalize_to_bucket')).toContain('f"{c}s"');
    await loadPricingCategories({ get: vi.fn().mockResolvedValue([...BASE, 'gates']) });
    expect(normalizeToBucket('gate')).toBe('gates');
  });

  it('neither side ever returns `labor` from the free-form path', async () => {
    // Labor prices off the LaborPriceItem matrix; the tier engine rejects it.
    expect(pyFunc('_normalize_to_bucket')).toContain('!= "labor"');
    expect(normalizeToBucket('labor')).toBeNull();
    await loadPricingCategories({ get: vi.fn().mockResolvedValue([...BASE, 'labor']) });
    expect(normalizeToBucket('labor')).toBeNull();
  });
});
