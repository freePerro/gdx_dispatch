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
