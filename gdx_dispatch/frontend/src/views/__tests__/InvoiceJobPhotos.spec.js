/**
 * InvoiceDetailView — job photos on the invoice PDF (2026-08-07).
 *
 * The office picks job photos on a DRAFT invoice; checked photos print as a
 * "Job Photos" grid on the PDF. Pinned:
 *  1. The picker card renders only for job-linked invoices with photos, via
 *     AuthedImage (a plain <img> can't send the Bearer token).
 *  2. Checkboxes are disabled once the invoice leaves draft.
 *  3. Toggling PATCHes attached_photo_ids (whole-list replace).
 *  4. normalizeInvoice carries attached_photo_ids so state survives reloads.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(join(__dirname, '..', 'InvoiceDetailView.vue'), 'utf8');

describe('InvoiceDetailView — job photo picker', () => {
  it('renders the picker card for job-linked invoices with photos', () => {
    const idx = SRC.indexOf('data-testid="invoice-job-photos"');
    expect(idx).toBeGreaterThan(-1);
    const before = SRC.slice(Math.max(0, idx - 300), idx);
    expect(before).toMatch(/v-if="invoice\.job_id && jobPhotos\.length"/);
  });

  it('uses AuthedImage for thumbnails (Bearer-gated downloads)', () => {
    expect(SRC).toMatch(/import AuthedImage from "\.\.\/components\/AuthedImage\.vue"/);
    // Window from the card testid through the end of its photo grid — the
    // first </template> after the testid is the inline v-if in the hint, so
    // slice generously instead.
    const card = SRC.slice(SRC.indexOf('data-testid="invoice-job-photos"'));
    const cardBody = card.slice(0, 2500);
    expect(cardBody).toMatch(/<AuthedImage/);
  });

  it('locks the checkboxes once the invoice is no longer a draft', () => {
    const card = SRC.slice(SRC.indexOf('data-testid="invoice-job-photos"'));
    const cardBody = card.slice(0, card.indexOf('</div>\n      </template>'));
    expect(cardBody).toMatch(/:disabled="invoice\.status !== 'draft'/);
  });

  it('togglePhoto PATCHes the whole attached_photo_ids list', () => {
    const start = SRC.indexOf('async function togglePhoto');
    expect(start).toBeGreaterThan(-1);
    const span = SRC.slice(start, start + 900);
    expect(span).toMatch(/api\.patch\(`\/api\/invoices\/\$\{[^}]+\}`,\s*\{\s*attached_photo_ids:/);
    // Draft-only guard lives in the handler too, not just the disabled attr.
    expect(span).toMatch(/invoice\.value\.status !== "draft"/);
  });

  it('normalizeInvoice carries attached_photo_ids', () => {
    const start = SRC.indexOf('function normalizeInvoice');
    const span = SRC.slice(start, start + 6000);
    expect(span).toMatch(/attached_photo_ids:\s*Array\.isArray\(payload\.attached_photo_ids\)/);
  });
});
