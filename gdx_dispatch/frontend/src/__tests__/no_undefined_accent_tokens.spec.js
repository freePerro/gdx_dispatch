/**
 * No `var(--accent-*)` without a fallback unless the token is defined.
 *
 * GDXA-1481: SegmentsView painted the active audience chip with
 * `var(--accent-b)`, which nothing under src/ defines. The fill resolved to
 * transparent under `color: #fff` — white on white in light mode, on the chip
 * that says which audience Add Tag / Export CSV act on. An undefined custom
 * property fails silently, so only a guard catches it.
 *
 * A reference with a fallback (`var(--accent-a, #f7b32b)`) still renders and
 * is out of scope here. KNOWN is a ratchet: it may shrink, never grow.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const SRC_DIR = join(__dirname, '..');

// file (relative to src/) → token. Empty since GDXA-1482 fixed JobDetailView.
const KNOWN = new Set();

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry.startsWith('.')) continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) out.push(...walk(full));
    else if (full.endsWith('.vue') || full.endsWith('.css')) out.push(full);
  }
  return out;
}

describe('no undefined --accent-* token', () => {
  const files = walk(SRC_DIR).map((f) => [relative(SRC_DIR, f), readFileSync(f, 'utf8')]);
  const defined = new Set();
  for (const [, text] of files) {
    for (const m of text.matchAll(/(--accent-[\w-]+)\s*:/g)) defined.add(m[1]);
  }
  const found = new Set();
  for (const [rel, text] of files) {
    for (const m of text.matchAll(/var\(\s*(--accent-[\w-]+)\s*\)/g)) {
      if (!defined.has(m[1])) found.add(`${rel} ${m[1]}`);
    }
  }

  it('every fallback-less --accent-* reference is defined somewhere', () => {
    const fresh = [...found].filter((k) => !KNOWN.has(k));
    expect(fresh, 'undefined token renders transparent — use a --p-* theme token').toEqual([]);
  });

  it('KNOWN lists only instances that still exist', () => {
    const stale = [...KNOWN].filter((k) => !found.has(k));
    expect(stale, 'fixed — drop it from KNOWN').toEqual([]);
  });
});
