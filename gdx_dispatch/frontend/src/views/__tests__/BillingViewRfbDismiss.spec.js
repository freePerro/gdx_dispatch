/**
 * BillingView — Ready-for-Billing dismiss verbs (2026-08-04).
 *
 * The RFB queue had exactly two exits: Create Invoice, or sit forever.
 * Warranty/goodwill/internal jobs and outright mistakes needed:
 *  - "Not billable": POST /api/jobs/{id}/not-billable with a REQUIRED reason
 *    (staff-decline rule, Doug 2026-07-30) — job leaves every billing surface,
 *    audit-logged, reversible from JobDetailView's tag.
 *  - "Delete": the standard soft-delete for jobs that exist by mistake,
 *    behind the canonical confirmAsync guard.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(join(__dirname, '..', 'BillingView.vue'), 'utf8');
const DETAIL_SRC = readFileSync(join(__dirname, '..', 'JobDetailView.vue'), 'utf8');

describe('BillingView RFB — Not billable + Delete verbs', () => {
  it('both new verbs render in the same Action column as Review/Create Invoice', () => {
    const actionColumnIdx = SRC.indexOf('header="Action"');
    expect(actionColumnIdx).toBeGreaterThan(-1);
    const after = SRC.slice(actionColumnIdx);
    const actionColumnBody = after.slice(0, after.indexOf('</Column>'));
    expect(actionColumnBody).toMatch(/mark-job-not-billable/);
    expect(actionColumnBody).toMatch(/delete-rfb-job/);
    // The existing verbs survive.
    expect(actionColumnBody).toMatch(/review-job-before-billing/);
    expect(actionColumnBody).toMatch(/create-invoice-for-job/);
  });

  it('reason dialog exists, requires a non-blank reason, and posts on confirm', () => {
    expect(SRC).toMatch(/data-testid="not-billable-dialog"/);
    expect(SRC).toMatch(/data-testid="not-billable-reason"/);
    // Confirm is disabled until a real (trimmed) reason exists.
    expect(SRC).toMatch(/:disabled="!notBillableReason\.trim\(\)"/);

    const start = SRC.indexOf('async function confirmNotBillable');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 1200);
    expect(span).toMatch(/api\.post\(\s*`\/api\/jobs\/\$\{[^}]+\}\/not-billable`/);
    expect(span).toMatch(/\{\s*reason\s*\}/);
    // The row leaves the queue client-side on success.
    expect(span).toMatch(/readyJobs\.value\s*=\s*readyJobs\.value\.filter/);
  });

  it('empty-reason guard exists in the handler too (not just the disabled prop)', () => {
    const start = SRC.indexOf('async function confirmNotBillable');
    const span = SRC.slice(start, start + 400);
    expect(span).toMatch(/if\s*\(!job\?\.id\s*\|\|\s*!reason/);
  });

  it('Delete goes through confirmAsync and api.del (soft delete, audit-logged server-side)', () => {
    const start = SRC.indexOf('async function deleteRfbJob');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 1200);
    expect(span).toMatch(/confirmAsync\(/);
    // api.del, NOT api.delete — the client has no .delete; that exact typo
    // produced 9 dead delete buttons (contract sweep 2026-07-24).
    expect(span).toMatch(/api\.del\(\s*`\/api\/jobs\/\$\{[^}]+\}`/);
    expect(span).not.toMatch(/api\.delete\(/);
    expect(span).toMatch(/readyJobs\.value\s*=\s*readyJobs\.value\.filter/);
  });
});

describe('useDestructiveConfirm — #215 regression pin', () => {
  it('resolves the confirm service eagerly during setup, not lazily in the handler', () => {
    // Issue #215: useConfirm() is inject() under the hood and only works
    // during setup(). Resolved lazily inside a click handler it always
    // failed, and the fallback silently auto-accepted — the Delete verb this
    // PR adds would have been a one-click job delete with no dialog.
    const composable = readFileSync(
      join(__dirname, '..', '..', 'composables', 'useDestructiveConfirm.js'),
      'utf8',
    );
    const body = composable.slice(composable.indexOf('export function useDestructiveConfirm'));
    const eagerIdx = body.indexOf('_confirm = useConfirm()');
    const getConfirmIdx = body.indexOf('function getConfirm');
    expect(eagerIdx).toBeGreaterThan(-1);
    expect(getConfirmIdx).toBeGreaterThan(-1);
    // The first useConfirm() call must sit BEFORE getConfirm's declaration —
    // i.e., it runs when the composable is constructed (setup context).
    expect(eagerIdx).toBeLessThan(getConfirmIdx);
  });
});

describe('JobDetailView — NOT BILLABLE tag + undo', () => {
  it('renders the tag only when marked, with the reason in the tooltip', () => {
    expect(DETAIL_SRC).toMatch(/data-testid="job-detail-not-billable"/);
    expect(DETAIL_SRC).toMatch(/v-if="job\.not_billable_at"/);
    expect(DETAIL_SRC).toMatch(/not_billable_reason/);
  });

  it('makeBillable confirms, then DELETEs the mark and refetches', () => {
    const start = DETAIL_SRC.indexOf('async function makeBillable');
    expect(start).toBeGreaterThan(-1);
    const span = DETAIL_SRC.slice(start, start + 1200);
    expect(span).toMatch(/confirmAsync\(/);
    expect(span).toMatch(/api\.del\(\s*`\/api\/jobs\/\$\{[^}]+\}\/not-billable`/);
    expect(span).toMatch(/fetchJob\(\)/);
  });
});
