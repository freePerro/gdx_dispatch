/**
 * EstimateView — email composer shows the attached PDF inline (2026-07-20).
 *
 * The composer already snapshotted the PDF server-side at open (base64 in
 * composer.pdf) but only showed a filename chip. The shared
 * ComposerPdfPreview renders those same bytes in an iframe so the sender
 * sees the actual document before it goes to the customer.
 *
 * Source-pin style (EstimateView has no mount harness): the preview's own
 * behavior is mount-tested in components/__tests__/ComposerPdfPreview.spec.js;
 * these pins hold the wiring in place.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(join(__dirname, '..', 'EstimateView.vue'), 'utf8');

describe('EstimateView — composer PDF preview wiring', () => {
  it('imports the shared ComposerPdfPreview component', () => {
    expect(SRC).toMatch(/import ComposerPdfPreview from "\.\.\/components\/ComposerPdfPreview\.vue"/);
  });

  it('renders the preview inside the estimate composer dialog, bound to composer.pdf', () => {
    const dlg = SRC.indexOf('data-testid="estimate-composer"');
    expect(dlg).toBeGreaterThan(-1);
    const dlgEnd = SRC.indexOf('</Dialog>', dlg);
    expect(SRC.slice(dlg, dlgEnd)).toMatch(/<ComposerPdfPreview :pdf="composer\.pdf" \/>/);
  });
});
