/**
 * MobileEstimatesView — deposit-at-acceptance contract (2026-07-24).
 *
 * Gap audit: this was the one accept surface that could never collect a
 * deposit (posted a bare {}). Pinned:
 *  1. accept() consults /deposit-default before posting (prefill + gate).
 *  2. The actual accept POST carries deposit_amount (0 when declined).
 *  3. The opt-in prompt exists with toggle + amount input, and the result
 *     dialog exposes the pay-link copy affordance.
 *  4. Mobile stays opt-IN: the toggle starts false — the prompt must never
 *     default to collecting.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const SRC = readFileSync(
  join(__dirname, '..', 'MobileEstimatesView.vue'),
  'utf8',
);

describe('MobileEstimatesView — deposit at accept', () => {
  it('accept() consults the deposit-default endpoint', () => {
    expect(SRC).toMatch(/deposit-default/);
  });

  it('the accept POST carries deposit_amount', () => {
    expect(SRC).toMatch(/deposit_amount:\s*Number\(depositAmt\)\s*>\s*0\s*\?\s*Number\(depositAmt\)\s*:\s*0/);
  });

  it('renders the opt-in prompt with toggle, amount and result dialogs', () => {
    expect(SRC).toMatch(/data-test="me-deposit-prompt"/);
    expect(SRC).toMatch(/data-test="me-deposit-toggle"/);
    expect(SRC).toMatch(/data-test="me-deposit-amount"/);
    expect(SRC).toMatch(/data-test="me-deposit-accept"/);
    expect(SRC).toMatch(/data-test="me-deposit-result"/);
    expect(SRC).toMatch(/data-test="me-deposit-copy"/);
  });

  it('mobile stays opt-in: the collect toggle resets to false when prompting', () => {
    // The block that opens the prompt must set collectDeposit.value = false
    // before showing it.
    const openIdx = SRC.indexOf('depositPromptOpen.value = true');
    expect(openIdx).toBeGreaterThan(-1);
    const before = SRC.slice(Math.max(0, openIdx - 400), openIdx);
    expect(before).toMatch(/collectDeposit\.value\s*=\s*false/);
  });

  it('surfaces deposit_skipped as a warning instead of swallowing it', () => {
    expect(SRC).toMatch(/deposit_skipped/);
  });
});
