/**
 * EstimateView on a phone (2026-08-11).
 *
 * Doug's call: one estimate editor, not two. A separate mobile editor would
 * have meant a second pricing path — and a simplified one that POSTs
 * `unit_price` with no `cost` takes the manual branch in estimates.py and
 * leaves cost_snapshot NULL, producing lines whose margin can never be
 * measured. Same view + a layout means the margin engine, cost/override
 * handling and tax stay exactly as they are.
 *
 * The view is 3050 lines and mounting it needs the pricing engine, tax config,
 * catalogs and a route param, so the layout contract is pinned as source
 * assertions (the approach AppBottomNav.spec.js already uses). jsdom applies no
 * media queries, so a mounted test could not assert this anyway.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(join(__dirname, '..', 'EstimateView.vue'), 'utf8');
const MOBILE_BLOCK = SRC.slice(SRC.search(/@media\s*\(max-width:\s*768px\)/));

describe('EstimateView — phone layout', () => {
  it('has a mobile breakpoint block', () => {
    expect(SRC).toMatch(/@media\s*\(max-width:\s*768px\)/);
  });

  it('restacks the line-item row instead of keeping nine fixed columns', () => {
    // The bug: the block already hid .line-item-header but left the ROW at
    // `64px 120px minmax(160px,1fr) 70px 110px 110px 80px 90px 36px` (~840px),
    // so an estimate scrolled sideways on a phone and the Total column sat
    // off-screen — you could not see what you were quoting.
    expect(MOBILE_BLOCK).toMatch(/\.line-item-row\s*\{[^}]*grid-template-columns:/);
    expect(MOBILE_BLOCK).not.toMatch(/\.line-item-row\s*\{[^}]*64px/);
  });

  it('releases the 840px min-width floor', () => {
    // Regression guard, earned the hard way (2026-08-11 phone walk on the demo
    // stack): the desktop rule carries `min-width: 840px` — the sum of the nine
    // tracks — and overriding grid-template-columns alone left it in force. The
    // card stayed 840px wide on a 390px screen and .line-items-editor scrolled
    // sideways instead: the same bug, one container out. The first version of
    // THIS spec passed while that was live, because a grid-tracks assertion
    // says nothing about min-width.
    expect(MOBILE_BLOCK).toMatch(/\.line-item-row\s*\{[^}]*min-width:\s*0/);
    expect(MOBILE_BLOCK).toMatch(/\.line-items-editor\s*\{[^}]*overflow-x:\s*visible/);
  });

  it('labels each field, since the column header row is hidden there', () => {
    expect(MOBILE_BLOCK).toMatch(/\.line-item-header\s*\{\s*display:\s*none/);
    expect(MOBILE_BLOCK).toMatch(/\.line-label\s*\{[^}]*display:\s*block/);
  });

  it('keeps the labels out of the desktop grid entirely', () => {
    // display:none is load-bearing: a display:none element is not a grid item,
    // so the nine-track desktop row is unchanged by their presence.
    const beforeMedia = SRC.slice(0, SRC.search(/@media\s*\(max-width:\s*768px\)/));
    expect(beforeMedia).toMatch(/\.line-label\s*\{\s*display:\s*none/);
  });

  it('ships one label per editable column', () => {
    const labels = [...SRC.matchAll(/<span class="line-label">([^<]+)<\/span>/g)].map((m) => m[1]);
    expect(labels).toEqual(['Category', 'Description', 'Qty', 'Cost', 'Unit Price', 'Margin', 'Total']);
  });

  it('frees the fixed column widths and rehomes the row actions', () => {
    expect(MOBILE_BLOCK).toMatch(/\.line-item-row\s*>\s*\.col-desc/);
    expect(MOBILE_BLOCK).toMatch(/\.line-item-row\s*>\s*\.col-action\s*\{[^}]*grid-column:\s*1\s*\/\s*-1/);
  });

  it('still renders the plugin estimate source (the reason for all of this)', () => {
    // "Add captured door" is what a phone-shaped estimate editor is FOR — a
    // door captured in the field has to land somewhere.
    expect(SRC).toMatch(/estimateSource/);
  });
});
