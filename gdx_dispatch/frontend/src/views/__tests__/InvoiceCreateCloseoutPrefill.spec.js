/**
 * InvoiceCreateView — closeout prefill (2026-08-07).
 *
 * "Click invoice — it does not show hours or notes from the job": the
 * create screen prefilled from the estimate only, so a service job with a
 * closeout and no estimate opened blank. Pinned:
 *  1. prefillFromJobCloseout hits the suggestion endpoint and fills the
 *     labor line ONLY into a still-empty starter editor (estimate wins).
 *  2. It runs AFTER the estimate prefill in both entry paths (job change
 *     and ?job_id= mount).
 *  3. The closeout context card renders hours and the tech's notes, with a
 *     one-tap "Use as invoice notes".
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(join(__dirname, '..', 'InvoiceCreateView.vue'), 'utf8');

describe('InvoiceCreateView — closeout prefill', () => {
  it('fetches the suggestion and fills only an empty starter editor', () => {
    const start = SRC.indexOf('async function prefillFromJobCloseout');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 1600);
    expect(span).toMatch(/closeout-billing-suggestion/);
    expect(span).toMatch(/starterOnly/);
    expect(span).toMatch(/taxable:\s*false/);
    expect(span).toMatch(/category:\s*'Labor'/);
  });

  it('runs after the estimate prefill on both entry paths', () => {
    expect(SRC).toMatch(/prefillFromJobEstimate\(form\.value\.job_id\)\.then\(\(\) =>\s*\n?\s*prefillFromJobCloseout\(form\.value\.job_id\)/);
    const mountIdx = SRC.indexOf('await prefillFromJobEstimate(qJobId);');
    expect(mountIdx).toBeGreaterThan(-1);
    expect(SRC.slice(mountIdx, mountIdx + 200)).toMatch(/await prefillFromJobCloseout\(qJobId\);/);
  });

  it('renders the closeout context card with hours, notes, and the notes shortcut', () => {
    const card = SRC.slice(SRC.indexOf('data-testid="closeout-context"'), SRC.indexOf('data-testid="closeout-context"') + 2000);
    expect(card).toMatch(/closeout-context-hours/);
    expect(card).toMatch(/closeout-context-notes/);
    expect(card).toMatch(/data-testid="use-closeout-notes"/);
    expect(card).toMatch(/form\.notes = closeoutSuggestion\.closeout\.notes/);
  });

  // Round 2 (2026-08-07): "it is missing the notes the tech put on it" —
  // the work summary lives in job_notes, not the closeout note.
  it('closeout notes auto-fill the invoice notes when empty', () => {
    const start = SRC.indexOf('async function prefillFromJobCloseout');
    const span = SRC.slice(start, start + 2000);
    expect(span).toMatch(/s\.closeout\?\.notes && !form\.value\.notes/);
    expect(span).toMatch(/form\.value\.notes = s\.closeout\.notes/);
  });

  it('tech job notes render with internal badge and per-note actions', () => {
    const card = SRC.slice(SRC.indexOf('data-testid="closeout-tech-notes"'), SRC.indexOf('data-testid="closeout-tech-notes"') + 1800);
    expect(card).toMatch(/n\.visibility === 'internal'/);
    expect(card).toMatch(/note-to-invoice-notes/);
    expect(card).toMatch(/note-to-labor-desc/);
  });

  // 2026-08-08 audit: the estimate prefill took the LATEST estimate
  // regardless of status — draft/declined prices prefilled the invoice.
  it('estimate prefill filters to ACCEPTED estimates only (§15.1)', () => {
    const start = SRC.indexOf('async function prefillFromJobEstimate');
    const span = SRC.slice(start, start + 1200);
    expect(span).toMatch(/=== 'accepted'/);
  });

  it('use-as-labor-description targets the Labor line and stays editable text', () => {
    const start = SRC.indexOf('function useNoteAsLaborDescription');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 700);
    expect(span).toMatch(/'labor'/);
    expect(span).toMatch(/target\.description = body\.slice\(0, 500\)/);
  });
});
