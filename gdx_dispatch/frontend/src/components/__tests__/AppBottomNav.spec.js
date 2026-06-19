/**
 * AppBottomNav — pin the More-drawer height override.
 *
 * 2026-05-10 (Doug): "when you tap on more you only see a little bit of
 * the screen." Root cause: PrimeVue Drawer position="bottom" defaults to
 * `height: 10rem` (~160px) per @primeuix/styles/drawer. On a phone that's
 * ~20% of the viewport. AppBottomNav now ships an explicit override that
 * sizes the drawer to leave the bottom-nav visible and a little safety
 * margin.
 *
 * If a future PrimeVue upgrade changes the selector or someone removes
 * the override, this test fails — and Doug's complaint resurfaces.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(
  join(__dirname, '..', 'AppBottomNav.vue'),
  'utf8',
);

describe('AppBottomNav More-drawer styles', () => {
  // PrimeVue's `class="more-drawer"` prop is merged into ptmi('root'),
  // which lands on the `.p-drawer` panel itself (NOT on a parent of it).
  // So the override has to target `.more-drawer.p-drawer` (concatenated
  // class selector), not `.more-drawer .p-drawer` (descendant — wouldn't
  // match anything).
  it('targets .more-drawer.p-drawer with a height override', () => {
    expect(SRC).toMatch(
      /\.more-drawer\.p-drawer\s*\{[^}]*height\s*:/,
    );
  });

  it('sized to leave the bottom nav visible (uses --bottom-nav-height)', () => {
    expect(SRC).toMatch(/--bottom-nav-height/);
  });

  // The drawer is teleported to <body>, so a scoped Vue rule with
  // [data-v-hash] never matches the rendered DOM. The height override
  // has to live in a NON-scoped <style> block so the compiled selector
  // is global. If a future refactor wraps it in `<style scoped>`, this
  // test fails — and Doug's complaint resurfaces.
  it('lives in a non-scoped <style> block (so it survives teleport)', () => {
    // Find the height rule, then walk back to the nearest <style ...> tag
    // and verify it does NOT carry the `scoped` attribute.
    const heightRuleIdx = SRC.search(
      /\.more-drawer\.p-drawer\s*\{[^}]*height\s*:/,
    );
    expect(heightRuleIdx).toBeGreaterThan(-1);

    const before = SRC.slice(0, heightRuleIdx);
    const lastStyleOpen = before.lastIndexOf('<style');
    expect(lastStyleOpen).toBeGreaterThan(-1);
    const styleTag = SRC.slice(
      lastStyleOpen,
      SRC.indexOf('>', lastStyleOpen) + 1,
    );
    expect(styleTag).not.toMatch(/\bscoped\b/);
  });
});
