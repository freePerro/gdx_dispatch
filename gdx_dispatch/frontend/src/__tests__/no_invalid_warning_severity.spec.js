/**
 * Architectural lint — the PrimeVue-3 severity token `'warning'` is INVALID
 * in PrimeVue 4 (valid: secondary/success/info/warn/danger/contrast) and
 * renders a colorless tag/button.
 *
 * 2026-07-24 (contract-gap sweep, Tier 4): 65 severity sites across 54 files
 * carried the dead token — every one of them a status pill silently rendering
 * grey. This test pins the fix: any exact quoted 'warning' token in a
 * non-comment line under src/ fires here. The word inside longer strings
 * (CSS classes like `banner-warning`, prose) is untouched — only the exact
 * token `'warning'` / `"warning"` is banned, because in this codebase it is
 * only ever produced as a severity value.
 *
 * Recovery: use 'warn' (or the right token) — better, use the shared maps in
 * src/utils/statusSeverity.js so two screens can't disagree about the same
 * status again.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const SRC_DIR = join(__dirname, '..');

const EXACT_WARNING = /(['"`])warning\1/;

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry.startsWith('.') || entry === '__tests__') continue;
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) out.push(...walk(full));
    else if (full.endsWith('.vue') || (full.endsWith('.js') && !full.endsWith('.spec.js'))) out.push(full);
  }
  return out;
}

function isCommentLine(line) {
  const s = line.trim();
  return s.startsWith('//') || s.startsWith('*') || s.startsWith('/*') || s.startsWith('<!--') || s.startsWith('#');
}

describe("no invalid PrimeVue-3 'warning' severity token", () => {
  it("no .vue/.js source line uses the exact quoted token 'warning'", () => {
    const violations = [];
    for (const file of walk(SRC_DIR)) {
      const lines = readFileSync(file, 'utf8').split('\n');
      lines.forEach((line, i) => {
        if (isCommentLine(line)) return;
        if (EXACT_WARNING.test(line)) {
          violations.push(`${relative(SRC_DIR, file)}:${i + 1}  ${line.trim().slice(0, 100)}`);
        }
      });
    }
    if (violations.length) {
      throw new Error(
        `\n${violations.length} invalid 'warning' severity token(s) found (use 'warn', ideally via utils/statusSeverity.js):\n\n` +
        violations.slice(0, 25).join('\n'),
      );
    }
    expect(violations).toEqual([]);
  });
});
