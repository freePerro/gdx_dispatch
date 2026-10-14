/**
 * GDXA-885 — lock the text-link colour token in base.css.
 *
 * Views colour their text links `var(--p-primary-color)`, Aura's emerald
 * #10b981 in light mode: 2.54:1 on white. base.css re-points that token at
 * `--link-color` inside links, link-styled buttons and file-pick labels.
 *
 * jsdom resolves neither var() nor the cascade, so this checks the contract
 * (every light block sets a token that clears AA against the light surfaces,
 * dark keeps #34d399, the re-pointing rule exists and is light-only). The
 * painted colour is proven in a browser by e2e/link-color-contrast-885.spec.js.
 */
import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const css = fs.readFileSync(path.join(__dirname, '..', 'base.css'), 'utf8');

function luminance(hex) {
  const [r, g, b] = [1, 3, 5]
    .map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** The `--link-color` value declared inside the first block opened by `opener`. */
function linkColorIn(opener) {
  const start = css.indexOf(opener);
  expect(start, `${opener} block exists`).toBeGreaterThanOrEqual(0);
  const body = css.slice(start, css.indexOf('}', start));
  const m = body.match(/--link-color:\s*(#[0-9a-fA-F]{6})\s*;/);
  expect(m, `${opener} sets --link-color`).not.toBeNull();
  return m[1].toLowerCase();
}

// White panels, Aura's light --p-content-hover-background (slate-100), the
// app background and the elevated surface.
const LIGHT_SURFACES = ['#ffffff', '#f1f5f9', '#f5f7fa', '#f0f2f5'];

describe('GDXA-885 — text-link colour clears WCAG AA', () => {
  it.each([
    ['[data-theme="light"] {'],
    [':root:not([data-theme="dark"]) {'],
  ])('%s sets a link colour >= 4.5:1 on every light surface', (opener) => {
    const link = linkColorIn(opener);
    for (const bg of LIGHT_SURFACES) {
      expect(contrast(link, bg), `${link} on ${bg}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it('dark mode keeps Aura dark primary #34d399', () => {
    expect(linkColorIn(':root {')).toBe('#34d399');
  });

  it('re-points --p-primary-color at the link token on links, buttons and labels, light mode only', () => {
    expect(css).toMatch(
      /:where\(:root:not\(\[data-theme="dark"\]\)\)\s*:where\(a, button, label\)\s*\{\s*--p-primary-color:\s*var\(--link-color\);\s*\}/,
    );
  });

  it.each(['color', 'hover-color', 'active-color'])(
    'points PrimeVue <Button link> --p-button-link-%s at the link token with !important',
    (tok) => {
      expect(css).toMatch(new RegExp(`--p-button-link-${tok}:\\s*var\\(--link-color\\)\\s*!important`));
    },
  );

  it('does not darken --p-primary-color globally (it drives fills, borders, focus rings)', () => {
    const selectors = css
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .split('}')
      .filter((block) => /--p-primary-color\s*:/.test(block))
      .map((block) => block.slice(0, block.indexOf('{')).trim());
    expect(selectors).toEqual([':where(:root:not([data-theme="dark"])) :where(a, button, label)']);
  });
});
