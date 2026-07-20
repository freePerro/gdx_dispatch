/**
 * D6 — the Outlook "Auto Email" admin tab lets an admin configure per-event
 * auto-email templates, but dispatch_trigger is never wired to any event, so
 * saving here silently sends nothing. Until the revive-vs-remove decision is
 * made, the tab must clearly say it's inactive so no admin is misled.
 *
 * Source-level assertion (same pattern as no_legacy_css_tokens.spec.js) —
 * robust against the Tabs-heavy mount, guards the notice from accidental
 * removal.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const VIEW = resolve(__dirname, '../OutlookSettingsView.vue');

describe('OutlookSettings Auto-Email inactive notice (D6)', () => {
  const src = readFileSync(VIEW, 'utf8');

  it('renders an inactive notice in the Auto-Email tab', () => {
    expect(src).toContain('data-test="auto-email-inactive"');
    expect(src).toMatch(/Not active yet/i);
    expect(src).toMatch(/not currently wired to any event/i);
  });

  it('places the notice inside the Auto-Email tab panel', () => {
    const panelIdx = src.indexOf('TAB_KEYS.AUTO_EMAIL');
    const noticeIdx = src.indexOf('auto-email-inactive');
    expect(panelIdx).toBeGreaterThan(-1);
    expect(noticeIdx).toBeGreaterThan(panelIdx); // notice comes after the tab opens
  });
});
