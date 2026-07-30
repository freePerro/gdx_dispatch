/**
 * Plan §1 — the closeout finally has a read side. Static-source pins that the
 * two surfaces actually consume it, in the house style of the other
 * wiring-guard specs (a behavioral render of the 1800-line views is not what
 * protects this; the wiring is).
 *
 * Desktop: JobDetailView fetches GET /api/jobs/{id}/closeout and renders the
 * closeout card (hours, parts attestation, notes, signature METADATA, closer,
 * revision history). Mobile: the Time card shows the attested hours + notes
 * from job.closeout, and renders even without an arrival stamp.
 *
 * Also pinned: neither surface ever touches `signature_data` from the
 * closeout payload — the API deliberately never sends it (audit A4), and a
 * view reaching for it would mean someone re-added the blob server-side.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const DESKTOP = readFileSync(join(__dirname, '..', 'JobDetailView.vue'), 'utf8');
const MOBILE = readFileSync(join(__dirname, '..', 'MobileJobDetailView.vue'), 'utf8');

describe('closeout read side — wiring', () => {
  it('JobDetailView fetches the closeout endpoint and renders the card', () => {
    expect(DESKTOP).toMatch(/api\.get\(`\/api\/jobs\/\$\{route\.params\.id\}\/closeout`/);
    expect(DESKTOP).toMatch(/data-testid="job-closeout-card"/);
    expect(DESKTOP).toMatch(/data-testid="closeout-hours"/);
    expect(DESKTOP).toMatch(/data-testid="closeout-parts"/);
    expect(DESKTOP).toMatch(/data-testid="closeout-signature"/);
    // The revision history block — the supersede model must be VISIBLE.
    expect(DESKTOP).toMatch(/data-testid="closeout-history"/);
    // fetchCloseout must run with the rest of the related fetches.
    expect(DESKTOP).toMatch(/fetchCloseout\(\),/);
  });

  it('MobileJobDetailView shows the attested hours and notes', () => {
    expect(MOBILE).toMatch(/data-testid="mobile-closeout-summary"/);
    expect(MOBILE).toMatch(/data-testid="mobile-closeout-notes"/);
    expect(MOBILE).toMatch(/job\.closeout\.hours_worked/);
    // The Time card renders for a closeout even with no arrival stamp.
    expect(MOBILE).toMatch(/v-if="job\.arrived_at \|\| job\.closeout"/);
  });

  it('neither surface reaches for the raw signature blob off the closeout', () => {
    // signature_present is the contract; closeout.signature_data must not
    // exist anywhere — the API never sends it (audit A4).
    expect(DESKTOP).not.toMatch(/closeout\.signature_data/);
    expect(MOBILE).not.toMatch(/closeout\.signature_data/);
    expect(DESKTOP).toMatch(/closeout\.signature_present/);
  });
});
