/**
 * Parts capture unification (PR4-billing-capture 2026-07-07).
 *
 * Closeout/mobile/van part captures now feed the billable checklist as
 * source-tagged 'used' rows. Pinned:
 *  1. LineItemEditor's parts picker fetches 'used' rows too, pre-checks
 *     them, badges provenance, and prefers the capture-time sell price.
 *  2. BillingView renders the "Parts used, never billed" review card fed by
 *     /api/parts-needed/unbilled-consumed.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const EDITOR_SRC = readFileSync(
  join(__dirname, '..', '..', 'components', 'LineItemEditor.vue'),
  'utf8',
);
const BILLING_SRC = readFileSync(join(__dirname, '..', 'BillingView.vue'), 'utf8');

describe('LineItemEditor — used parts reach the checklist', () => {
  it("fetches 'used' rows alongside ordered/received", () => {
    expect(EDITOR_SRC).toMatch(/status=ordered,received,used&unbilled=true/);
  });

  it('pre-checks received AND used (tech-attested) parts', () => {
    expect(EDITOR_SRC).toMatch(/p\.status === 'received' \|\| p\.status === 'used'/);
  });

  it('badges used rows with their capture source', () => {
    expect(EDITOR_SRC).toMatch(/status-pill status-used/);
    expect(EDITOR_SRC).toMatch(/used · \{\{ part\.source \|\| 'closeout' \}\}/);
  });

  it('prefers the capture-time sell price over sku-suggest', () => {
    const idx = EDITOR_SRC.indexOf('async function addSelectedParts');
    expect(idx).toBeGreaterThan(-1);
    const span = EDITOR_SRC.slice(idx, idx + 700);
    expect(span).toMatch(/Number\(p\.unit_price\) > 0 \? Number\(p\.unit_price\) : 0/);
    expect(span).toMatch(/if \(!unitPrice && p\.sku\)/);
  });
});

describe('BillingView — parts used, never billed review card', () => {
  it('renders the review card with count + suggested total', () => {
    expect(BILLING_SRC).toMatch(/data-testid="unbilled-parts-review"/);
    expect(BILLING_SRC).toMatch(/Parts used, never billed/);
    expect(BILLING_SRC).toMatch(/data\.suggested_total/);
  });

  it('loads the leak report with the billing data', () => {
    expect(BILLING_SRC).toMatch(/api\.get\("\/api\/parts-needed\/unbilled-consumed"\)/);
    expect(BILLING_SRC).toMatch(/leakedParts\.value = /);
  });

  it('review routes to the job (same reviewJob path as Ready-for-Billing)', () => {
    const idx = BILLING_SRC.indexOf('data-testid="unbilled-parts-review"');
    const span = BILLING_SRC.slice(idx, idx + 2200);
    expect(span).toMatch(/reviewJob\(\{ id: data\.job_id \}\)/);
  });

  it("has a Won't-bill dismiss path (audit round 2: no dismiss = the card becomes wallpaper)", () => {
    expect(BILLING_SRC).toMatch(/data-testid="dismiss-unbilled-parts"/);
    const fnIdx = BILLING_SRC.indexOf('async function dismissLeakedParts');
    expect(fnIdx).toBeGreaterThan(-1);
    const span = BILLING_SRC.slice(fnIdx, fnIdx + 700);
    expect(span).toMatch(/status: 'wont_bill'/);
  });
});
