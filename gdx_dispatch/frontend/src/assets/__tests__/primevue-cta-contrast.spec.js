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

/**
 * Toast width on a phone — 2026-08-12 phone audit.
 *
 * PrimeVue's toast is a fixed 25rem (400px) anchored to a screen edge, so on a
 * 390px phone it rendered at left:-30px and every message the app showed was
 * clipped off the side. Measured on ALL 26 audited screens, because the toast
 * container mounts app-wide — the single most widespread phone defect found.
 *
 * Pinned here rather than in a component spec because .p-toast is teleported to
 * <body>: only a global stylesheet can reach it. jsdom applies no media queries,
 * so this is a structural assertion — the real proof is a browser measurement,
 * not this test.
 */
describe('responsive.css — phone toast', () => {
  const cssPath = path.join(
    path.dirname(fileURLToPath(import.meta.url)), '..', 'responsive.css',
  );
  const css = fs.readFileSync(cssPath, 'utf8');

  // Extract the BODY of the @media block, brace-matched. A naive
  // `slice(indexOf(...))` runs to end-of-file, so it would still pass if the
  // rule were moved OUT of the media query — making toasts full-width on a
  // 2560px desktop while the test stayed green. (Caught in review, 2026-08-12.)
  function mediaBody(source, query) {
    const start = source.indexOf(query);
    if (start === -1) return '';
    const open = source.indexOf('{', start);
    let depth = 0;
    for (let i = open; i < source.length; i += 1) {
      if (source[i] === '{') depth += 1;
      else if (source[i] === '}') {
        depth -= 1;
        if (depth === 0) return source.slice(open + 1, i);
      }
    }
    return '';
  }

  const phoneBlock = mediaBody(css, '@media (max-width: 768px)');

  it('the extractor really is brace-matched (guards the guard)', () => {
    // If this ever returns the whole file again, every assertion below is void.
    expect(phoneBlock).not.toBe('');
    expect(phoneBlock).not.toMatch(/@media \(min-width/);
    expect(phoneBlock.length).toBeLessThan(css.length);
  });

  it('constrains .p-toast to the viewport, inside the phone breakpoint', () => {
    expect(phoneBlock).toMatch(/\.p-toast\s*\{[^}]*max-width:\s*calc\(100vw/);
  });

  it('marks width/left/right important — inline styles set the position', () => {
    // `width` needs it to beat PrimeVue's stylesheet rule; `left`/`right` need
    // it to beat INLINE styles on the toast root, where cascade order is
    // irrelevant. Dropping !important breaks the position half silently.
    expect(phoneBlock).toMatch(/\.p-toast\s*\{[^}]*width:\s*auto\s*!important/);
    expect(phoneBlock).toMatch(/\.p-toast\s*\{[^}]*left:[^;]*!important/);
    expect(phoneBlock).toMatch(/\.p-toast\s*\{[^}]*right:[^;]*!important/);
  });

  it('clears the transform that the *-center positions carry', () => {
    // .p-toast-top-center / -bottom-center set translateX(-50%); with both
    // edges pinned that would push the toast half off-screen.
    expect(phoneBlock).toMatch(/\.p-toast\s*\{[^}]*transform:\s*none\s*!important/);
  });
});

/**
 * Mobile viewport-unit + input-zoom guards — 2026-08-12 cross-platform pass.
 *
 * Both pin decisions that an adversarial review corrected:
 *  - svh NOT dvh for the fullscreen dialog. dvh re-evaluates as browser chrome
 *    hides/shows, resizing the dialog while the user types in it.
 *  - the iOS 16px input threshold applies to the five HAND-ROLLED auth forms.
 *    PrimeVue's .p-inputtext is already 1rem, so the app's ordinary inputs were
 *    never the problem — my first reading generalised from /login alone.
 *    A global rule in this file could not fix them anyway: a scoped
 *    `.field input[data-v-…]` outranks `input[type=…]` regardless of order.
 */
describe('mobile viewport units and input zoom', () => {
  const dir = path.dirname(fileURLToPath(import.meta.url));
  const css = fs.readFileSync(path.join(dir, '..', 'responsive.css'), 'utf8');

  it('sizes the fullscreen dialog with svh, keeping vh as the fallback', () => {
    expect(css).toMatch(/\.p-dialog\s*\{[^}]*height:\s*100vh/);
    expect(css).toMatch(/\.p-dialog\s*\{\s*height:\s*100svh/);
  });

  it('does not use dvh for the dialog (it resizes mid-typing)', () => {
    expect(css).not.toMatch(/\.p-dialog\s*\{[^}]*100dvh/);
  });

  it.each([
    'LoginView.vue', 'ForgotPasswordView.vue', 'ResetPasswordView.vue',
  ])('%s sizes its hand-rolled inputs at >=16px', (file) => {
    const src = fs.readFileSync(path.join(dir, '..', '..', 'views', file), 'utf8');
    // Only rules that actually TARGET a form control. Matching any selector
    // containing the string "input" also catches `.input-group label`, and a
    // label's font-size has nothing to do with zoom — the first version of this
    // test failed on exactly that. The last simple-selector token has to be the
    // control itself.
    const targetsControl = (selector) => selector.split(',').some((part) => {
      const last = part.trim().split(/\s+|>/).filter(Boolean).pop() || '';
      return /^(input|select|textarea)(\[[^\]]*\])?$/.test(last.replace(/:[a-z-]+$/, ''));
    });
    const inputRules = [...src.matchAll(/([^{}]*)\{([^}]*)\}/g)]
      .filter(([, sel]) => targetsControl(sel) && !sel.includes('::'))
      .map(([, sel, body]) => ({ sel: sel.trim(), fs: /font-size:\s*([0-9.]+)rem/.exec(body) }))
      .filter((r) => r.fs);
    expect(inputRules.length).toBeGreaterThan(0);
    for (const r of inputRules) {
      expect(parseFloat(r.fs[1]), `${file} "${r.sel}" is ${r.fs[1]}rem — iOS will zoom`)
        .toBeGreaterThanOrEqual(1);
    }
  });
});
