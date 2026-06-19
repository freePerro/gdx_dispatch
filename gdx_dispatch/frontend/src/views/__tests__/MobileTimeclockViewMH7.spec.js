/**
 * MH-7 — MobileTimeclockView max-shift prompts.
 *
 * Audit P1 #9: pre-fix the page showed "Clocked In 401:44:52" with no
 * warning. The fix adds:
 *   - 8h "Still working?" prompt with a confirm-to-dismiss button
 *   - 16h emphasized "Likely needs manual review" banner
 *
 * Source-level structural locks (no mount — the heavy PrimeVue chrome
 * is the slow path; we test the helper-level contract here). The
 * companion backend pytest exercises the response shape.
 */
import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SFC_PATH = path.join(__dirname, '..', 'MobileTimeclockView.vue');
const SRC = fs.readFileSync(SFC_PATH, 'utf8');

describe('MobileTimeclockView — MH-7 prompts', () => {
  it('declares warningAfterHours and maxShiftHours refs with the documented defaults', () => {
    expect(SRC).toMatch(/warningAfterHours\s*=\s*ref\(\s*8\s*\)/);
    expect(SRC).toMatch(/maxShiftHours\s*=\s*ref\(\s*16\s*\)/);
  });

  it('reads warning_after_hours + max_shift_hours from the status payload', () => {
    expect(SRC).toMatch(/status\?\.warning_after_hours/);
    expect(SRC).toMatch(/status\?\.max_shift_hours/);
  });

  it('exposes shouldShowStillWorkingPrompt + shouldShowMaxShiftBanner computeds', () => {
    expect(SRC).toMatch(/shouldShowStillWorkingPrompt\s*=\s*computed/);
    expect(SRC).toMatch(/shouldShowMaxShiftBanner\s*=\s*computed/);
  });

  it('still-working dismissal re-arms via setTimeout (not permanent silence)', () => {
    expect(SRC).toMatch(/stillWorkingDismissed\.value\s*=\s*true/);
    // The re-arm — without this, a tech dismissing at 8h gets no
    // follow-up nudge until the 16h banner. The interval is 1 hour.
    expect(SRC).toMatch(/stillWorkingDismissed\.value\s*=\s*false/);
    expect(SRC).toMatch(/setTimeout/);
  });

  it('resets stillWorkingDismissed on clock-out (new shift → fresh prompts)', () => {
    // The fetchStatus branch that handles clocked_out resets the flag.
    const m = SRC.match(/stopElapsedTimer\(\)[^}]*stillWorkingDismissed\.value\s*=\s*false/s);
    expect(m, 'expected stillWorkingDismissed = false in the clocked_out branch').not.toBeNull();
  });

  it('renders the still-working prompt above the status card', () => {
    // The data-test attributes are the contract; assert they exist + in
    // order (banner before status card).
    const promptIdx = SRC.indexOf('data-test="mt-still-working-prompt"');
    const bannerIdx = SRC.indexOf('data-test="mt-max-shift-banner"');
    const statusCardIdx = SRC.indexOf('data-test="mt-status-card"');
    expect(promptIdx).toBeGreaterThan(-1);
    expect(bannerIdx).toBeGreaterThan(-1);
    expect(statusCardIdx).toBeGreaterThan(-1);
    expect(promptIdx).toBeLessThan(statusCardIdx);
    expect(bannerIdx).toBeLessThan(statusCardIdx);
  });

  it('still-working prompt has a Yes-still-working confirm button', () => {
    expect(SRC).toMatch(/data-test="mt-still-working-confirm"/);
    expect(SRC).toMatch(/Yes,\s*still working/i);
  });

  it('max-shift banner uses role="alert" (screen reader urgency)', () => {
    const m = SRC.match(/data-test="mt-max-shift-banner"[^>]*role="alert"|role="alert"[^>]*data-test="mt-max-shift-banner"/s);
    expect(m, 'expected role="alert" on the max-shift banner').not.toBeNull();
  });

  it('still-working prompt uses role="status" (screen reader polite)', () => {
    const m = SRC.match(/data-test="mt-still-working-prompt"[^>]*role="status"|role="status"[^>]*data-test="mt-still-working-prompt"/s);
    expect(m, 'expected role="status" on the still-working prompt').not.toBeNull();
  });

  it('dark mode CSS exists for both banners', () => {
    expect(SRC).toMatch(/\[data-theme="dark"\] \.banner-info/);
    expect(SRC).toMatch(/\[data-theme="dark"\] \.banner-warning/);
  });
});
