/**
 * JobStateOverrideDialog — UX schema compliance.
 *
 * Doug 2026-05-10: "the re-open / warranty popup does not follow the ux
 * schema." Root cause: the component's scoped CSS was using PrimeVue v3
 * token names (--surface-*, --primary-*, --red-*, --text-color-secondary)
 * with hex fallbacks (#fff, #ccc, #e3f2fd, ...). The hex fallbacks
 * overrode dark mode entirely — path-cards rendered white-on-white,
 * unreadable.
 *
 * gdx/docs/frontend_view_pattern.md prescribes --p-* tokens with no hex
 * fallbacks. This spec pins that contract on the source so a future
 * "let me restore the fallback for IE compat" refactor fires here.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(
  join(__dirname, '..', 'JobStateOverrideDialog.vue'),
  'utf8',
);

// Extract just the <style scoped> block — we don't want to false-positive
// on token strings that appear in script comments or template attributes.
function styleBlock() {
  const m = SRC.match(/<style scoped>([\s\S]*?)<\/style>/);
  return m ? m[1] : '';
}

describe('JobStateOverrideDialog UX schema', () => {
  it('uses --p-* design tokens (not pre-v4 --surface-/--primary-/--red- names)', () => {
    const css = styleBlock();
    // Pre-v4 names that should not appear anywhere in the scoped CSS:
    const banned = [
      /--surface-(\d+|0)\b/,         // --surface-0, --surface-50, etc.
      /--primary-color\b/,            // bare --primary-color (use --p-primary-color)
      /--primary-(\d+)\b/,            // --primary-50, etc.
      /--red-(\d+)\b/,                // --red-50, --red-500, etc.
      /--text-color-secondary\b/,     // pre-v4 muted-text token
    ];
    for (const re of banned) {
      expect(css).not.toMatch(re);
    }
  });

  it('does not use hex-color fallbacks (they override dark mode)', () => {
    const css = styleBlock();
    // Doug 2026-05-10: hex fallbacks were the dark-mode killer.
    // No #rrggbb / #rgb literals in the scoped CSS — tokens or nothing.
    expect(css).not.toMatch(/#[0-9a-fA-F]{3}\b/);
    expect(css).not.toMatch(/#[0-9a-fA-F]{6}\b/);
  });

  it('the path-card surface colors come from --p-content-background + --p-content-border-color', () => {
    const css = styleBlock();
    // The dialog's option cards must inherit the active theme's content
    // surface so dark mode renders correctly.
    expect(css).toMatch(/\.path-card\s*\{[^}]*background\s*:\s*var\(--p-content-background\)/);
    expect(css).toMatch(/\.path-card\s*\{[^}]*border\s*:\s*1px\s+solid\s+var\(--p-content-border-color\)/);
  });

  it('Dialog has :breakpoints for mobile (matches project convention)', () => {
    expect(SRC).toMatch(/:breakpoints\s*=\s*"\{\s*'768px':/);
  });
});
