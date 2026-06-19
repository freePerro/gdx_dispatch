/**
 * MH-2 — Lock the PrimeVue CTA contrast override.
 *
 * Asserts the alias file (a) exists, (b) imports cleanly into main.js,
 * (c) aliases every PrimeVue `--p-button-success-*` token to its
 * corresponding `--p-button-primary-*` token (so brand-blue replaces
 * white-on-emerald), (d) uses `!important` on each alias so PrimeVue's
 * runtime-injected default tokens lose the cascade tie.
 *
 * Color-resolution itself isn't asserted in jsdom (no painted DOM),
 * but the structural contract is — if a future refactor strips
 * `!important` or removes an alias, this test fails and we re-walk
 * Lighthouse before the contrast regression ships.
 */
import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CSS_PATH = path.join(__dirname, '..', 'primevue-cta-contrast.css');
const MAIN_PATH = path.join(__dirname, '..', '..', 'main.js');

const SUCCESS_TOKENS = [
  // filled
  'background', 'border-color', 'color',
  'hover-background', 'hover-border-color', 'hover-color',
  'active-background', 'active-border-color', 'active-color',
  'focus-ring-color', 'focus-ring-shadow',
];

const OUTLINED_SUCCESS_TOKENS = [
  'border-color', 'color', 'hover-background', 'active-background',
];

const TEXT_SUCCESS_TOKENS = ['color', 'hover-background', 'active-background'];

describe('MH-2 — PrimeVue CTA contrast override', () => {
  it('the override file exists', () => {
    expect(fs.existsSync(CSS_PATH)).toBe(true);
  });

  it('is imported from main.js (so it actually loads in the SPA)', () => {
    const main = fs.readFileSync(MAIN_PATH, 'utf8');
    expect(main).toMatch(/primevue-cta-contrast\.css/);
  });

  const css = fs.existsSync(CSS_PATH) ? fs.readFileSync(CSS_PATH, 'utf8') : '';

  it.each(SUCCESS_TOKENS)('aliases --p-button-success-%s → --p-button-primary-%s with !important', (tok) => {
    const re = new RegExp(
      `--p-button-success-${tok}:\\s*var\\(--p-button-primary-${tok}\\)\\s*!important`,
    );
    expect(css).toMatch(re);
  });

  it.each(OUTLINED_SUCCESS_TOKENS)(
    'aliases --p-button-outlined-success-%s → --p-button-outlined-primary-%s with !important',
    (tok) => {
      const re = new RegExp(
        `--p-button-outlined-success-${tok}:\\s*var\\(--p-button-outlined-primary-${tok}\\)\\s*!important`,
      );
      expect(css).toMatch(re);
    },
  );

  it.each(TEXT_SUCCESS_TOKENS)(
    'aliases --p-button-text-success-%s → --p-button-text-primary-%s with !important',
    (tok) => {
      const re = new RegExp(
        `--p-button-text-success-${tok}:\\s*var\\(--p-button-text-primary-${tok}\\)\\s*!important`,
      );
      expect(css).toMatch(re);
    },
  );

  it('scopes overrides to both light and dark theme selectors', () => {
    expect(css).toMatch(/\[data-theme=\"light\"\]/);
    expect(css).toMatch(/\[data-theme=\"dark\"\]/);
  });

  it('does NOT remap Tag/Message/Toast/Badge success tokens (status semantics preserved)', () => {
    expect(css).not.toMatch(/--p-tag-success-/);
    expect(css).not.toMatch(/--p-message-success-/);
    expect(css).not.toMatch(/--p-toast-success-/);
    expect(css).not.toMatch(/--p-badge-success-/);
    expect(css).not.toMatch(/--p-progressbar-success-/);
  });

  // Fast-follow lock (first prod walk caught the alias-only strategy was
  // insufficient because Aura's primary IS emerald). The base primary
  // tokens MUST be brand-blue, not whatever Aura defaults to.
  it('overrides --p-button-primary-background to brand-blue (#2563eb)', () => {
    expect(css).toMatch(/--p-button-primary-background:\s*#2563eb\s*!important/);
  });

  it('overrides --p-button-primary-color to white (5.17:1 on #2563eb)', () => {
    expect(css).toMatch(/--p-button-primary-color:\s*#ffffff\s*!important/);
  });

  it('darkens on hover (active state is darker than base, not lighter)', () => {
    // base #2563eb, hover #1d4ed8, active #1e40af — strictly darkening
    expect(css).toMatch(/--p-button-primary-hover-background:\s*#1d4ed8\s*!important/);
    expect(css).toMatch(/--p-button-primary-active-background:\s*#1e40af\s*!important/);
  });

  it('does NOT introduce a separate dark-mode primary color (consistency across themes)', () => {
    // The same #2563eb background works in both modes (luminance, not
    // theme, decides contrast against white text). Locking this rules
    // out the future "dark mode looks better with a lighter blue"
    // refactor that would re-introduce WCAG failures.
    const matches = css.match(/--p-button-primary-background:[^;]+/g) || [];
    matches.forEach((m) => expect(m).toContain('#2563eb'));
  });
});
