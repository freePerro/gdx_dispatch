/**
 * Architectural lint — no white text on a solid `--interactive-primary` fill.
 *
 * 2026-10-19 (GDXA-1752/1754): dark `--interactive-primary` is #63b3ff, and
 * `color: #fff` on it measures 2.2:1 (WCAG AA needs 4.5:1). Light (#2563eb)
 * passes, so it only shows in dark mode. base.css now defines
 * `--text-on-interactive-primary` (#fff light, the app background dark) in all
 * three theme blocks; every rule that fills with the primary must take its
 * text colour from that token.
 *
 * A violation is one CSS rule block in a .vue or .css file under src/ whose
 * declarations include both `background(-color): var(--interactive-primary…`
 * and a white `color` (#fff, #ffffff, white, rgb(255,255,255)).
 *
 * Recovery: write `color: var(--text-on-interactive-primary);`.
 *
 * PENDING lists sites owned by other agents, fixed in the GDXA-1752 chain.
 * Each child removes its own entries; an entry that no longer violates fails
 * this spec, so the list can only shrink, and the last child leaves it empty.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const SRC_DIR = join(__dirname, '..');

// `<file relative to src/> <selector>` — keyed by selector, not line number.
const PENDING = new Set([
  'components/AIAssistantIntegrationCard.vue .btn-primary', // ai-mcp
  'components/PhoneComIntegrationCard.vue .btn-primary', // comms-email-phone
  'views/MobilePhoneView.vue .tab-chip.active', // comms-email-phone
  'views/MobileSmsView.vue .unread-badge', // comms-email-phone
  'views/MobileSmsView.vue .msg-out', // comms-email-phone
  'views/PhoneComMessagesView.vue .thread-unread-badge', // comms-email-phone
  'views/OnboardingView.vue .active .dot', // platform-core
  'views/OnboardingView.vue .btn-primary', // platform-core
]);

const PRIMARY_FILL = /(?:^|[;\s])background(?:-color)?\s*:\s*var\(\s*--interactive-primary\s*[,)]/;
const WHITE_TEXT = /(?:^|[;\s])color\s*:\s*(?:#fff(?:fff)?\b|white\b|rgba?\(\s*255\s*,\s*255\s*,\s*255\s*[,)])/i;

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
export function findWhiteOnPrimary(source) {
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

describe('no white text on --interactive-primary', () => {
  const files = walk(SRC_DIR);
  const found = files.flatMap((file) =>
    findWhiteOnPrimary(readFileSync(file, 'utf8')).map((v) => ({
      key: `${relative(SRC_DIR, file)} ${v.selector}`,
      at: `${relative(SRC_DIR, file)}:${v.line}`,
    })),
  );

  it('no new rule pairs a white colour with an --interactive-primary fill', () => {
    expect(files.length).toBeGreaterThan(100);
    const fresh = found.filter((v) => !PENDING.has(v.key));
    if (fresh.length) {
      throw new Error(
        `\n${fresh.length} rule(s) put white text on --interactive-primary (2.2:1 in dark mode):\n\n` +
        fresh.map((v) => `${v.at}  ${v.key}`).join('\n') +
        '\n\nWrite color: var(--text-on-interactive-primary).',
      );
    }
  });

  it('every PENDING entry still violates (remove the ones you fixed)', () => {
    const live = new Set(found.map((v) => v.key));
    expect([...PENDING].filter((k) => !live.has(k))).toEqual([]);
  });

  const CAUGHT = [
    ['#fff on the token', '.a { background: var(--interactive-primary); color: #fff; }'],
    ['the token with a fallback', '.a {\n  background: var(--interactive-primary, #2563eb);\n  color: #ffffff;\n}'],
    ['background-color and white', '.a { color: white; background-color: var(--interactive-primary); }'],
    ['rgb white', '.a { background: var( --interactive-primary ); color: rgb(255, 255, 255); }'],
  ];
  it.each(CAUGHT)('catches %s', (_name, css) => {
    expect(findWhiteOnPrimary(css)).toHaveLength(1);
  });

  const ALLOWED = [
    ['the new token', '.a { background: var(--interactive-primary); color: var(--text-on-interactive-primary); }'],
    ['the soft tint', '.a { background: var(--interactive-primary-soft); color: #fff; }'],
    ['white border-color only', '.a { background: var(--interactive-primary); border-color: #fff; }'],
    ['white in a different rule', '.a { background: var(--interactive-primary); }\n.b { color: #fff; }'],
    ['a commented-out colour', '.a { background: var(--interactive-primary); /* color: #fff; */ }'],
  ];
  it.each(ALLOWED)('allows %s', (_name, css) => {
    expect(findWhiteOnPrimary(css)).toHaveLength(0);
  });
});
