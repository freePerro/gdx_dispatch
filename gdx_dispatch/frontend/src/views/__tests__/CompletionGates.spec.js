/**
 * Completion gates (PR5-billing-capture 2026-07-07).
 *
 * Pinned:
 *  1. The closeout dialog has the deliberate "No parts used" attestation
 *     (satisfies the require-parts gate; silence still 422s server-side)
 *     and a per-part note for parts not in the system — and sends both.
 *  2. SettingsView exposes the new require_invoice_on_complete toggle.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const DIALOG_SRC = readFileSync(
  join(__dirname, '..', '..', 'components', 'MobileJobCloseoutDialog.vue'),
  'utf8',
);
const SETTINGS_SRC = readFileSync(join(__dirname, '..', 'SettingsView.vue'), 'utf8');

describe('MobileJobCloseoutDialog — no-parts attestation + part notes', () => {
  it('renders the attestation checkbox when the parts list is empty', () => {
    expect(DIALOG_SRC).toMatch(/data-testid="mjco-no-parts-used"/);
    expect(DIALOG_SRC).toMatch(/No parts were used on this job/);
  });

  it('sends no_parts_used and per-part note in the closeout payload', () => {
    expect(DIALOG_SRC).toMatch(/no_parts_used: noPartsUsed\.value/);
    expect(DIALOG_SRC).toMatch(/note: \(p\.note \|\| ''\)\.trim\(\) \|\| null/);
  });

  it('attestation alone makes the form submittable (backend still gates)', () => {
    expect(DIALOG_SRC).toMatch(/hasNotes \|\| noPartsUsed\.value/);
  });

  it('note input renders for free-text (not-in-system) parts', () => {
    expect(DIALOG_SRC).toMatch(/data-testid="`mjco-part-note-\$\{idx\}`"/);
    expect(DIALOG_SRC).toMatch(/Not in system\? Explain it for the office/);
  });
});

describe('SettingsView — invoice-before-complete toggle', () => {
  it('exposes workflowFlags.require_invoice_on_complete', () => {
    expect(SETTINGS_SRC).toMatch(/data-testid="wf-req-invoice"/);
    expect(SETTINGS_SRC).toMatch(/require_invoice_on_complete: false/);
  });
});
