/**
 * Architectural lint — no hex fallback on `--p-primary-color` in any .vue or
 * .css source under src/.
 *
 * 2026-10-14 (GDXA-1029): 76 sites wrote `var(--p-primary-color, #hex)`, most
 * with a BLUE fallback (#2563eb, #3b82f6). The token is never undefined —
 * main.js installs Aura (primary emerald #10b981) and assets/base.css re-points
 * it — so the fallback is dead code that states a false colour to whoever
 * reads the rule. GDXA-1030..1038 dropped every one; this pins it.
 *
 * Recovery: write `var(--p-primary-color)`. Other `--p-*` tokens are out of
 * scope here on purpose (the triage limited the class to this one token).
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const SRC_DIR = join(__dirname, '..');
const REPO_ROOT = join(SRC_DIR, '..', '..', '..', '..');

const HEX_FALLBACK = /var\(\s*--p-primary-color\s*,\s*#[0-9a-fA-F]{3,8}\b/;

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

export function findPrimaryHexFallbacks(source, label = '<inline>') {
  const out = [];
  String(source).split('\n').forEach((line, i) => {
    if (HEX_FALLBACK.test(line)) out.push(`${label}:${i + 1}  ${line.trim().slice(0, 80)}`);
  });
  return out;
}

describe('no hex fallback on --p-primary-color', () => {
  it('no .vue/.css file under src/ writes var(--p-primary-color, #hex)', () => {
    const files = walk(SRC_DIR);
    expect(files.length).toBeGreaterThan(100);
    const violations = files.flatMap((file) =>
      findPrimaryHexFallbacks(readFileSync(file, 'utf8'), relative(REPO_ROOT, file)),
    );
    if (violations.length) {
      throw new Error(
        `\n${violations.length} dead hex fallback(s) on --p-primary-color:\n\n` +
        violations.join('\n') +
        '\n\nWrite var(--p-primary-color): Aura always defines it (emerald, not blue).',
      );
    }
  });

  const CAUGHT = [
    ['the blue fallback the triage found', 'color: var(--p-primary-color, #3b82f6);'],
    ['a short hex', 'border: 1px solid var(--p-primary-color,#fff);'],
    ['spacing around the comma', 'background: var( --p-primary-color , #10B981 );'],
    ['nested in color-mix', 'background: color-mix(in srgb, var(--p-primary-color, #2563eb) 12%, transparent);'],
  ];
  it.each(CAUGHT)('catches %s', (_name, css) => {
    expect(findPrimaryHexFallbacks(css)).toHaveLength(1);
  });

  const ALLOWED = [
    ['the bare token', 'color: var(--p-primary-color);'],
    ['a different primary token', 'color: var(--p-primary-color-text, #fff);'],
    ['a numbered primary shade', 'background: var(--p-primary-50, #ecfdf5);'],
  ];
  it.each(ALLOWED)('allows %s', (_name, css) => {
    expect(findPrimaryHexFallbacks(css)).toHaveLength(0);
  });
});
