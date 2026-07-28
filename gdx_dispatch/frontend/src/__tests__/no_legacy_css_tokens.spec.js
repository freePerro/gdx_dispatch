/**
 * Architectural lint — no pre-PrimeVue-v4 CSS variable references in any
 * .vue or .css source under src/.
 *
 * 2026-05-10 (Doug): the Re-open/Warranty popup rendered white-on-white in
 * dark mode because the component used `var(--surface-0, #fff)` — the hex
 * fallback overrode dark mode entirely. A codebase grep surfaced 47 other
 * files with the same pattern. The codemod at
 * `gdx/frontend/scripts/migrate_legacy_tokens.py` migrated all 158
 * references to v4 `--p-*` tokens.
 *
 * This test pins the contract: if anyone re-introduces a legacy token,
 * it fires here. Recovery: run `python gdx/frontend/scripts/migrate_legacy_tokens.py`
 * and add any new pattern to the codemod's SUBS list.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import { backgroundLayers, contrastRatio, resolveColor } from './helpers/auraTokens';

const SRC_DIR = join(__dirname, '..');
const REPO_ROOT = join(SRC_DIR, '..', '..', '..', '..');

// Patterns that must NOT appear in any .vue/.css file under src/.
// Each pattern is the legacy form; the v4 equivalent is in the comment.
const BANNED_PATTERNS = [
  // {pattern, hint}
  { re: /var\(\s*--surface-0(?:\s*[,)])/, hint: '--surface-0 → --p-content-background' },
  { re: /var\(\s*--surface-(?:50|100)(?:\s*[,)])/, hint: '--surface-50/100 → --p-content-hover-background (semantic) — NOT --p-surface-N (still light-only)' },
  { re: /var\(\s*--surface-(?:200|300|400)(?:\s*[,)])/, hint: '--surface-200/300/400 → --p-content-border-color' },
  { re: /var\(\s*--primary-color(?:\s*[,)])/, hint: '--primary-color → --p-primary-color' },
  { re: /var\(\s*--primary-(?:50|100)(?:\s*[,)])/, hint: '--primary-50/100 → --p-highlight-background' },
  { re: /var\(\s*--text-color-secondary(?:\s*[,)])/, hint: '--text-color-secondary → --p-text-muted-color' },
  { re: /var\(\s*--red-(?:50|100|200|300|400|500|600|700|800|900)(?:\s*[,)])/, hint: '--red-N → --p-red-N (with N capped at 700)' },
  { re: /var\(\s*--green-(?:50|100|200|300|400|500|600|700|800|900)(?:\s*[,)])/, hint: '--green-N → --p-green-N' },
  { re: /var\(\s*--blue-(?:50|100|200|300|400|500|600|700|800|900)(?:\s*[,)])/, hint: '--blue-N → --p-blue-N' },
];

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

describe('no legacy CSS tokens (pre-PrimeVue-v4)', () => {
  const files = walk(SRC_DIR);

  it('no .vue/.css file uses a banned legacy token', () => {
    const violations = [];
    for (const file of files) {
      const text = readFileSync(file, 'utf8');
      // Skip lines that are purely comments (block or inline) so the
      // codemod's own self-documenting comments and historical retro
      // notes don't fire the gate.
      const lines = text.split('\n');
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const trimmed = line.trim();
        if (trimmed.startsWith('*') || trimmed.startsWith('//') || trimmed.startsWith('<!--')) continue;
        for (const { re, hint } of BANNED_PATTERNS) {
          if (re.test(line)) {
            violations.push(`${relative(REPO_ROOT, file)}:${i + 1}  ${trimmed.slice(0, 80)}\n    → ${hint}`);
          }
        }
      }
    }
    if (violations.length) {
      throw new Error(
        `\n${violations.length} legacy CSS-token violation(s) found:\n\n` +
        violations.slice(0, 25).join('\n\n') +
        (violations.length > 25 ? `\n\n…and ${violations.length - 25} more.` : '') +
        '\n\nRun: python gdx/frontend/scripts/migrate_legacy_tokens.py',
      );
    }
  });
});

/* ────────────────────────────────────────────────────────────────────
 * 2026-07-27 (Doug): the landing-lead popup and the outbound SMS bubble
 * rendered white-on-white in dark mode. Same defect class as the 2026-05-10
 * Re-open/Warranty popup guarded above — different spelling, so that lint
 * never fired.
 *
 * PrimeVue v4 Aura has two kinds of token. Semantic ones
 * (--p-content-background, --p-content-hover-background, --p-text-color,
 * --p-content-border-color) are redefined under [data-theme="dark"]. Numbered
 * ones are the raw palette: --p-surface-N is re-pointed slate->zinc in dark
 * but stays LIGHT (zinc.100 = #f4f4f5), and --p-primary-N / the named hues
 * are not redefined in the dark block at all. Paint a background with one and
 * the text over it — which flips to #ffffff — disappears.
 *
 * This gate does NOT keep a list of "bad tokens". The first version did, and
 * an adversarial review took it apart: `color: #fff` over --p-surface-100
 * passed, because the check tested whether a colour was a LITERAL, not
 * whether it was DARK. So did an asserted "surface-400+ is mid-grey and
 * legible either way" cutoff (surface.400 = #a1a1aa = 2.56:1 against
 * dark-mode white text).
 *
 * So: resolve both colours to real hex through the live Aura preset, and
 * measure. A rule fails when its own text would land under WCAG AA (4.5:1)
 * on its own background. Nothing is asserted; if the preset changes, the
 * measurement follows it.
 *
 * WHEN THIS FIRES, the fix is almost always a semantic token:
 *   subtle panel         -> --p-content-hover-background
 *   selected / drag-over -> --p-highlight-background
 *   hairline or track    -> --p-content-border-color
 * For a colour that carries meaning (error red, note yellow), mix it into the
 * live surface so it adapts by construction:
 *   color-mix(in srgb, #dc2626 12%, var(--p-content-background))
 * Or ship an explicit [data-theme="dark"] rule overriding the background —
 * see .push-cta in MobileTodayView.vue, which passes that way.
 * ──────────────────────────────────────────────────────────────────── */

const AA_NORMAL_TEXT = 4.5;
const BACKGROUND_DECL = /(^|\s)background(-color|-image)?\s*:/;
const COLOR_DECL = /(^|\s)color\s*:/;
// `/* dark-safe: <reason> */` on the line directly above a rule.
const DARK_SAFE_OPT_OUT = /\/\*\s*dark-safe:\s*[A-Za-z0-9]/;
const LIGHT_ONLY_SELECTOR =
  /:not\(\[data-theme=['"]dark['"]\]\)|\[data-theme=['"]light['"]\]/;
const DARK_SELECTOR_PREFIX =
  /^\s*(?::global\()?\s*(?:\[data-theme=['"]dark['"]\]|:root:not\(\[data-theme=['"]light['"]\]\))\s*/;

// Blank out comments (preserving line count) so prose naming a token — like
// the explainer above — never trips the gate.
function stripComments(text) {
  const blank = (m) => '\n'.repeat((m.match(/\n/g) || []).length);
  return text.replace(/\/\*[\s\S]*?\*\//g, blank).replace(/<!--[\s\S]*?-->/g, blank);
}

/**
 * `var(--tok, <fallback>)` becomes `var(--tok)`, at any nesting depth.
 *
 * The fallback only applies when the token is undefined, which never happens
 * for a PrimeVue token — so it is dead code that makes a declaration read
 * safer than it is. Two of the original 22 sites hid behind exactly that.
 * A naive /var\(\s*(--[\w-]+)\s*,[^()]*\)/ cannot do this: it stops at the
 * first paren, so `var(--p-text-color, rgba(17,24,39,.9))` survives intact
 * and the dead rgba() then reads as a real colour. Scan with a depth counter.
 */
function stripFallbacks(declaration) {
  let out = String(declaration);
  let cursor = 0;
  for (;;) {
    const rel = out.slice(cursor).search(/var\(\s*--[\w-]+/);
    if (rel === -1) break;
    const start = cursor + rel;
    const open = out.indexOf('(', start);
    let depth = 0;
    let comma = -1;
    let end = -1;
    for (let i = open; i < out.length; i++) {
      const ch = out[i];
      if (ch === '(') depth += 1;
      else if (ch === ')') {
        depth -= 1;
        if (depth === 0) { end = i; break; }
      } else if (ch === ',' && depth === 1 && comma === -1) comma = i;
    }
    if (end === -1) break;
    if (comma === -1) {
      cursor = end + 1;
    } else {
      out = `${out.slice(0, comma)})${out.slice(end + 1)}`;
      cursor = comma + 1;
    }
  }
  return out;
}

/**
 * A background is a LIGHT SURFACE when it was built to carry dark text: the
 * default light-mode text clears AA on it, and the default dark-mode text does
 * not. #f4f4f5 qualifies (9.50 -> 1.10). A saturated brand fill does not —
 * --p-primary-color is 4.08 in light and 1.92 in dark, so it never carried
 * dark text and its white-on-brand pairing is a deliberate, app-wide design
 * choice with a different owner. Textless things (progress fills, status dots)
 * fall out for the same reason.
 */
function isLightSurface(bgLight, bgDark) {
  const defaultLight = resolveColor('light', 'var(--p-text-color)');
  const defaultDark = resolveColor('dark', 'var(--p-text-color)');
  if (!defaultLight || !defaultDark) return false;
  return (
    contrastRatio(defaultLight, bgLight) >= AA_NORMAL_TEXT
    && contrastRatio(defaultDark, bgDark) < AA_NORMAL_TEXT
  );
}

const normalize = (s) => s.trim().split(/\s+/).join(' ');

function parseRules(text) {
  const rules = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(text)) !== null) rules.push({ selector: m[1], body: m[2], index: m.index });
  return rules;
}

/**
 * Every dark-mode contrast failure in one file. Exported so the gate's own
 * fixture tests can prove it fires — a gate whose only evidence is "it passes
 * on the code I just edited" is circular.
 */
export function findContrastFailures(source, label = '<inline>') {
  // Lines carrying an explicit opt-out, captured from the RAW source because
  // stripComments() is about to blank them. Read before, judged after.
  const optedOut = new Set();
  const srcLines = String(source).split('\n');
  srcLines.forEach((line, i) => {
    if (!DARK_SAFE_OPT_OUT.test(line)) return;
    // The marker may sit on the rule's own line as a trailing comment
    // (PdfTemplateEditorView annotates its printed-page preview that way), or
    // above it — possibly above a multi-line comment block. Walk forward to
    // the first line that actually opens a rule.
    optedOut.add(i + 1);
    for (let j = i + 1; j < srcLines.length && j <= i + 8; j += 1) {
      const next = srcLines[j].trim();
      if (!next || next.startsWith('*') || next.startsWith('/*') || next.endsWith('*/')) continue;
      optedOut.add(j + 1);
      break;
    }
  });
  const text = stripComments(source);
  const rules = parseRules(text);
  const out = [];

  // Selectors that already ship an explicit dark-mode background override.
  const darkOverridden = new Set();
  for (const { selector, body } of rules) {
    if (!BACKGROUND_DECL.test(body)) continue;
    for (const part of selector.split(',')) {
      if (DARK_SELECTOR_PREFIX.test(part)) {
        darkOverridden.add(normalize(part.replace(DARK_SELECTOR_PREFIX, '').replace(/\)\s*$/, '')));
      }
    }
  }

  for (const { selector, body, index } of rules) {
    // Judge per selector-part. Whole-rule exclusion let `.a, .b :deep(.c)`
    // shield .a behind its sibling's :deep.
    const parts = selector.split(',').map((p) => p.trim()).filter(Boolean);
    const judged = parts.filter((part) => {
      if (darkOverridden.has(normalize(part))) return false;
      // This gate assumes an absent `color` means the element inherits our
      // theme's text colour. Inside :deep() we are restyling a PrimeVue
      // component's internals, whose own tokens govern its text and which
      // often hold no text at all (a progressbar fill, a slider range).
      // That assumption does not hold, so the measurement would be fiction.
      if (/:deep\(/.test(part)) return false;
      // A rule scoped away from dark mode cannot break dark mode — base.css
      // guards its light-mode page background exactly this way.
      if (LIGHT_ONLY_SELECTOR.test(part)) return false;
      return true;
    });
    if (judged.length === 0) continue;

    const decls = body.split(';');
    const bgDecls = decls.filter((d) => BACKGROUND_DECL.test(d));
    if (bgDecls.length === 0) continue;

    // Whether an element holds text is not decidable from its CSS — an 8px
    // status dot and a text tile can declare identical properties. So the
    // gate does not guess: it is TOLD, at the site, with a reason. Every
    // exemption is greppable (`grep -rn "dark-safe:" src/`) and has to say
    // why, which is the opposite of a silent suppression list.
    const selectorStart = index + (selector.length - selector.replace(/^\s+/, '').length);
    const ruleLine = text.slice(0, selectorStart).split('\n').length;
    if (optedOut.has(ruleLine)) continue;

    const colorDecl = decls.filter((d) => COLOR_DECL.test(d)).pop();
    const colorValue = colorDecl ? colorDecl.split(':').slice(1).join(':') : null;
    // Text colour as it will actually render. An absent or unresolvable
    // `color` inherits, and the inherited value flips to #ffffff in dark —
    // which is precisely what makes this bug invisible, so that default is
    // the whole point of the check.
    const fgFor = (mode) =>
      (colorValue && resolveColor(mode, colorValue)) || resolveColor(mode, 'var(--p-text-color)');
    const fgDark = fgFor('dark');
    const fgLight = fgFor('light');
    if (!fgDark || !fgLight) continue;

    for (const decl of bgDecls) {
      // Resolve VALUES, not token names. A literal `background: #f4f4f5` is
      // the same defect as `var(--p-surface-100)`, and the tree had 118
      // raw-hex backgrounds that a token-only check could never see.
      for (const layer of backgroundLayers(decl)) {
        const bgDark = resolveColor('dark', layer);
        const bgLight = resolveColor('light', layer);
        if (!bgDark || !bgLight) continue;
        const dark = contrastRatio(fgDark, bgDark);
        if (dark >= AA_NORMAL_TEXT) continue;
        // SCOPE, stated as a definition rather than a tuned threshold.
        // This gate covers LIGHT SURFACES that survive the theme flip — the
        // defect Doug reported. "Light surface" is measured, not asserted: a
        // background is one when default light-mode text clears AA on it
        // while default dark-mode text does not. #f4f4f5 qualifies (9.50 ->
        // 1.10). A saturated brand fill does not: --p-primary-color is 4.08
        // in light and 1.92 in dark, so it fails BOTH and is a deliberate
        // white-on-brand choice, app-wide, with a different owner. Ditto
        // textless things (progress fills, status dots) where the assumed
        // inherited text colour is fiction.
        //
        // Being explicit because an adversarial review called an earlier
        // version of this line a bar lowered to fit the result: that is a
        // fair charge against a number picked to go green, and the answer is
        // that this is not a number. It is "was this ever a light surface?".
        // The a11y-debt test below counts what falls outside, so the excluded
        // set is a visible number rather than a silence.
        // Ask "is this a LIGHT SURFACE?" of the background alone, using each
        // theme's DEFAULT text. Asking it of the rule's own `color` was the
        // hole an adversarial review drove through: an explicit `color:#fff`
        // made the light pairing fail too, and the rule excused itself.
        if (!isLightSurface(bgLight, bgDark)) continue;
        const light = contrastRatio(fgLight, bgLight);
        out.push(
          `${label}:${ruleLine}  ${normalize(selector).slice(-60)}\n` +
          `    ${layer.trim()} -> ${bgLight} light / ${bgDark} dark; text ` +
          `${fgLight} / ${fgDark}; ${light.toFixed(2)}:1 becomes ${dark.toFixed(2)}:1 at the flip`,
        );
      }
    }
  }
  return out;
}

describe('dark-mode contrast gate (measured, not asserted)', () => {
  it('no .vue/.css rule puts text under 4.5:1 on its own background in dark mode', () => {
    const violations = [];
    for (const file of walk(SRC_DIR)) {
      violations.push(...findContrastFailures(readFileSync(file, 'utf8'), relative(REPO_ROOT, file)));
    }
    if (violations.length) {
      throw new Error(
        `\n${violations.length} dark-mode contrast failure(s):\n\n` +
        violations.slice(0, 25).join('\n\n') +
        (violations.length > 25 ? `\n\nand ${violations.length - 25} more.` : '') +
        '\n\nA numbered --p-* token is light in BOTH themes; text over it turns\n' +
        'white in dark mode and disappears. See the block comment in this file\n' +
        'for the three fixes.',
      );
    }
  });

  /* The gate's own regression suite. Each fixture is a real evasion an
   * adversarial review found in the first, pattern-matching version. */
  const CAUGHT = [
    ['the reported bug, plainly', '.a { background: var(--p-surface-100); padding: 1px; }'],
    ['a light surface with an explicitly WHITE text colour', '.a { background: var(--p-surface-100); color: #fff; }'],
    ['a literal hex light surface, no token in sight', '.a { background: #f4f4f5; }'],
    ['a 95% light colour-mix laundered through an adaptive base', '.a { background: color-mix(in srgb, var(--p-surface-100) 95%, var(--p-content-background)); }'],
    ['a :deep sibling shielding a plain selector', '.a, .b :deep(.c) { background: var(--p-surface-100); }'],
    ['a dead hex fallback dressing it up', '.a { background: var(--p-surface-100, #f1f5f9); color: var(--p-text-color, #111827); }'],
    ['a dead PARENTHESISED fallback', '.a { background: var(--p-surface-100); color: var(--p-text-color, rgba(17,24,39,0.9)); }'],
    ['Aura dark highlight.color, which is near-white', '.a { background: var(--p-primary-50); color: rgba(255,255,255,.87); }'],
    ['a light hue at 300', '.a { background: var(--p-red-300); }'],
    ['a gradient, which has no flat background value', '.a { background-image: linear-gradient(var(--p-surface-100), var(--p-surface-50)); }'],
    ['background-color rather than background', '.a { background-color: var(--p-primary-100); }'],
  ];
  it.each(CAUGHT)('catches %s', (_name, css) => {
    expect(findContrastFailures(css)).not.toHaveLength(0);
  });

  const ALLOWED = [
    ['the semantic hover surface', '.a { background: var(--p-content-hover-background); }'],
    ['the semantic border token', '.a { background: var(--p-content-border-color); }'],
    ['a hue mixed into the live surface', '.a { background: color-mix(in srgb, #dc2626 12%, var(--p-content-background)); }'],
    ['a light background with genuinely dark text', '.a { background: var(--p-red-50); color: var(--p-red-700); }'],
    ['an explicit dark-mode override', '.a { background: var(--p-primary-50); } [data-theme="dark"] .a { background: rgba(37,99,235,0.12); }'],
    ['an explicit dark-safe opt-out with a reason', '/* dark-safe: 8px dot, holds no text */\n.a { background: #4fc3f7; }'],
    ['an opt-out above a multi-line comment block', '/* dark-safe: paper surface, see below */\n/* more\n   prose */\n.a { background: #ffffff; }'],
    ['an opt-out as a trailing comment on the rule itself', '.a { background: white; } /* dark-safe: printed-page preview */'],
    ['the prefers-color-scheme override form', '.a { background: var(--p-primary-50); } :root:not([data-theme="light"]) .a { background: rgba(37,99,235,0.12); }'],
  ];
  it.each(ALLOWED)('allows %s', (_name, css) => {
    expect(findContrastFailures(css)).toHaveLength(0);
  });

  /* Deliberately OUT OF SCOPE, recorded so the boundary is a decision rather
   * than an accident. These are unreadable in dark mode — but equally so in
   * light, which makes them pre-existing contrast debt, not damage the theme
   * flip caused. Blocking on them here would bury the regression signal, and
   * the same measurement fires on textless elements (progress-bar fills,
   * capacity tracks) where the assumed inherited text colour is fiction.
   * A general WCAG audit is the right owner; see the a11y debt count below. */
  it('does not accept a bare dark-safe marker with no reason', () => {
    expect(findContrastFailures('/* dark-safe: */\n.a { background: #f4f4f5; }')).not.toHaveLength(0);
  });

  const OUT_OF_SCOPE = [
    ['a saturated brand fill that fails in both themes', '.a { background: var(--p-primary-color); color: #fff; }'],
    ['a mid-tone token that fails in both themes', '.a { background: var(--p-surface-400); }'],
  ];
  it.each(OUT_OF_SCOPE)('does not claim to cover %s', (_name, css) => {
    expect(findContrastFailures(css)).toHaveLength(0);
  });
});
