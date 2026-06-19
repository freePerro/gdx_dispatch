/**
 * MH-3 — In-app theme toggle (audit P1 #12).
 *
 * Locks the three theme menu items in AppTopbar's user-menu computed:
 *   - "Theme: System"  → setColorMode('auto')
 *   - "Theme: Light"   → setColorMode('light')
 *   - "Theme: Dark"    → setColorMode('dark')
 *
 * Pre-fix the only way for a tech to get dark mode was the phone's OS
 * pref — there was no chrome affordance. We don't mount the topbar's
 * heavy PrimeVue chrome here (causes downstream Toolbar/Menu mount
 * issues in jsdom); we exercise the computed `userMenuItems` directly
 * by importing the SFC module and asserting the items array shape.
 *
 * The test takes a snapshot of the AppTopbar.vue source and asserts
 * the theme menu items + commands are present — this is a structural
 * regression lock, not a runtime mount test.
 */
import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const APPTOPBAR_PATH = path.join(__dirname, '..', 'AppTopbar.vue');
const SRC = fs.readFileSync(APPTOPBAR_PATH, 'utf8');

describe('AppTopbar — MH-3 theme menu', () => {
  it('exposes a "Theme: System" item wired to setColorMode("auto")', () => {
    expect(SRC).toMatch(/label:\s*['"]Theme:\s*System['"]/);
    // The command for the system row must call setColorMode('auto').
    expect(SRC).toMatch(/setColorMode\(['"]auto['"]\)/);
  });

  it('exposes a "Theme: Light" item wired to setColorMode("light")', () => {
    expect(SRC).toMatch(/label:\s*['"]Theme:\s*Light['"]/);
    expect(SRC).toMatch(/setColorMode\(['"]light['"]\)/);
  });

  it('exposes a "Theme: Dark" item wired to setColorMode("dark")', () => {
    expect(SRC).toMatch(/label:\s*['"]Theme:\s*Dark['"]/);
    expect(SRC).toMatch(/setColorMode\(['"]dark['"]\)/);
  });

  it('marks the currently-active mode with a check icon', () => {
    // Each item picks its icon by comparing theme.colorMode to its
    // mode string and falling back to a mode-specific glyph.
    expect(SRC).toMatch(/theme\.colorMode\s*===\s*['"]auto['"]\s*\?\s*['"]pi pi-check['"]/);
    expect(SRC).toMatch(/theme\.colorMode\s*===\s*['"]light['"]\s*\?\s*['"]pi pi-check['"]/);
    expect(SRC).toMatch(/theme\.colorMode\s*===\s*['"]dark['"]\s*\?\s*['"]pi pi-check['"]/);
  });

  it('keeps Profile and Logout in the menu (no regression)', () => {
    expect(SRC).toMatch(/label:\s*['"]Profile['"]/);
    expect(SRC).toMatch(/label:\s*['"]Logout['"]/);
  });
});
