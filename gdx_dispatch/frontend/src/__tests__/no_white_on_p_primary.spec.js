/**
 * Architectural lint — no white text on a solid `--p-primary-color` fill.
 *
 * 2026-10-19 (GDXA-1760/1761): the fill is Aura emerald wherever base.css
 * does not darken it — dark #34d399 everywhere (white on it is 1.92:1), light
 * #10b981 outside a link, button or label (2.54:1). WCAG AA needs 4.5:1.
 * base.css defines `--text-on-p-primary`, set beside the darkened fill so the
 * two always change together: zinc-900 on emerald, #fff on the light link
 * colour. Every rule that fills with the primary takes its text from it.
 *
 * A violation is one CSS rule block in a .vue or .css file under src/ whose
 * declarations include both `background(-color): var(--p-primary-color…`
 * and a white `color` (#fff, #ffffff, white, rgb(255,255,255), or Aura's
 * `var(--p-primary-contrast-color)`, which is #fff in light mode and so fails
 * on an emerald span the same way).
 *
 * Recovery: write `color: var(--text-on-p-primary);`.
 *
 * Sibling of no_white_on_interactive_primary.spec.js (GDXA-1754), which
 * guards the --interactive-primary fill the same way.
 *
 * PENDING lists sites owned by other agents, fixed in the GDXA-1760 chain.
 * Each child removes its own entries; an entry that no longer violates fails
 * this spec, so the list can only shrink, and the last child leaves it empty.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const SRC_DIR = join(__dirname, '..');

// `<file relative to src/> <selector>` — keyed by selector, not line number.
const PENDING = new Set([
  'views/PlannerView.vue .msg.mine', // jobs-dispatch
  'views/SegmentsView.vue .segment-chip.active', // customers-crm
]);

const PRIMARY_FILL = /(?:^|[;\s])background(?:-color)?\s*:\s*var\(\s*--p-primary-color\s*[,)]/;
// Aura's --p-primary-contrast-color counts as white: it is #fff in light mode,
// where only a link, button or label has a darkened fill.
const WHITE_TEXT = /(?:^|[;\s])color\s*:\s*(?:#fff(?:fff)?\b|white\b|rgba?\(\s*255\s*,\s*255\s*,\s*255\s*[,)]|var\(\s*--p-primary-contrast-color\s*[,)])/i;

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry.startsWith('.')) continue;
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) out.push(...walk(full));
    else if (full.endsWith('.vue') || full.endsWith('.css')) out.push(full);
  }
  return out;
}

/** Returns `{ selector, line }` for each innermost rule block in the class. */
export function findWhiteOnPPrimary(source) {
  const src = String(source).replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, ' '));
  const out = [];
  const block = /([^{}]*)\{([^{}]*)\}/g;
  let m;
  while ((m = block.exec(src))) {
    const body = m[2];
    if (!PRIMARY_FILL.test(body) || !WHITE_TEXT.test(body)) continue;
    const head = m[1].split(/[;\n]/).map((s) => s.trim()).filter(Boolean);
    const selector = (head[head.length - 1] || '').replace(/\s+/g, ' ');
    const line = src.slice(0, m.index + m[1].length).split('\n').length;
    out.push({ selector, line });
  }
  return out;
}

describe('no white text on --p-primary-color', () => {
  const files = walk(SRC_DIR);
  const found = files.flatMap((file) =>
    findWhiteOnPPrimary(readFileSync(file, 'utf8')).map((v) => ({
      key: `${relative(SRC_DIR, file)} ${v.selector}`,
      at: `${relative(SRC_DIR, file)}:${v.line}`,
    })),
  );

  it('no new rule pairs a white colour with a --p-primary-color fill', () => {
    expect(files.length).toBeGreaterThan(100);
    const fresh = found.filter((v) => !PENDING.has(v.key));
    if (fresh.length) {
      throw new Error(
        `\n${fresh.length} rule(s) put white text on --p-primary-color (1.92:1 in dark mode):\n\n` +
        fresh.map((v) => `${v.at}  ${v.key}`).join('\n') +
        '\n\nWrite color: var(--text-on-p-primary).',
      );
    }
  });

  it('every PENDING entry still violates (remove the ones you fixed)', () => {
    const live = new Set(found.map((v) => v.key));
    expect([...PENDING].filter((k) => !live.has(k))).toEqual([]);
  });

  it('base.css defines --text-on-p-primary beside the darkened fill', () => {
    const css = readFileSync(join(SRC_DIR, 'assets/base.css'), 'utf8');
    expect(css).toMatch(/:root\s*\{\s*--text-on-p-primary:\s*#18181b;/);
    expect(css).toMatch(
      /:where\(a, button, label\)\s*\{\s*--p-primary-color:\s*var\(--link-color\);\s*--text-on-p-primary:\s*#fff;/,
    );
  });

  const CAUGHT = [
    ['#fff on the token', '.a { background: var(--p-primary-color); color: #fff; }'],
    ['the token with a fallback', '.a {\n  background: var(--p-primary-color, #10b981);\n  color: #ffffff;\n}'],
    ['background-color and white', '.a { color: white; background-color: var(--p-primary-color); }'],
    ['rgb white', '.a { background: var( --p-primary-color ); color: rgb(255, 255, 255); }'],
    ['a one-line rule', '.msg.mine { align-self: flex-end; background: var(--p-primary-color); color: white; }'],
    ['Aura contrast token', '.a { background: var(--p-primary-color); color: var(--p-primary-contrast-color, #ffffff); }'],
  ];
  it.each(CAUGHT)('catches %s', (_name, css) => {
    expect(findWhiteOnPPrimary(css)).toHaveLength(1);
  });

  const ALLOWED = [
    ['the new token', '.a { background: var(--p-primary-color); color: var(--text-on-p-primary); }'],
    ['a primary border only', '.a { border: 1px solid var(--p-primary-color); color: #fff; }'],
    ['white border-color only', '.a { background: var(--p-primary-color); border-color: #fff; }'],
    ['white in a different rule', '.a { background: var(--p-primary-color); }\n.b { color: #fff; }'],
    ['a commented-out colour', '.a { background: var(--p-primary-color); /* color: #fff; */ }'],
    ['a different primary token', '.a { background: var(--p-primary-color-soft); color: #fff; }'],
  ];
  it.each(ALLOWED)('allows %s', (_name, css) => {
    expect(findWhiteOnPPrimary(css)).toHaveLength(0);
  });
});
