/**
 * Dunning opt-in surfaces (PR6-billing-capture 2026-07-07).
 *
 * Pinned:
 *  1. InvoiceRemindersView has the auto-send toggle (default OFF comes from
 *     the API) and shows the qualify-preview when flipped ON — the operator
 *     sees exactly who gets emailed before saving.
 *  2. InvoiceDetailView has the per-invoice Pause/Resume reminders toggle
 *     hitting the dunning-pause endpoint, driven by dunning_paused.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const REMINDERS_SRC = readFileSync(join(__dirname, '..', 'InvoiceRemindersView.vue'), 'utf8');
const DETAIL_SRC = readFileSync(join(__dirname, '..', 'InvoiceDetailView.vue'), 'utf8');

describe('InvoiceRemindersView — auto-send opt-in with preview', () => {
  it('renders the auto-send toggle', () => {
    expect(REMINDERS_SRC).toMatch(/data-testid="reminders-auto-send-toggle"/);
    expect(REMINDERS_SRC).toMatch(/settings\.auto_send_enabled/);
  });

  it('fetches and renders the qualify-preview when toggled on', () => {
    expect(REMINDERS_SRC).toMatch(/\/api\/invoice-reminders\/auto-send-preview/);
    expect(REMINDERS_SRC).toMatch(/data-testid="reminders-auto-send-preview"/);
    expect(REMINDERS_SRC).toMatch(/autoSendPreview\.count/);
  });

  it('renders the REAL permanent-dismiss control for the weekly nudge (audit round 2: it was a phantom)', () => {
    expect(REMINDERS_SRC).toMatch(/data-testid="reminders-nudge-dismiss"/);
    expect(REMINDERS_SRC).toMatch(/settings\.auto_send_nudge_dismissed/);
  });

  it('persists auto_send_enabled + nudge dismissal in the save payload', () => {
    const idx = REMINDERS_SRC.indexOf('async function saveSettings');
    const span = REMINDERS_SRC.slice(idx, idx + 700);
    expect(span).toMatch(/auto_send_enabled: settings\.value\.auto_send_enabled/);
    expect(span).toMatch(/auto_send_nudge_dismissed: settings\.value\.auto_send_nudge_dismissed/);
  });
});

describe('InvoiceDetailView — per-invoice dunning mute', () => {
  it('renders Pause/Resume reminders on sent/overdue invoices', () => {
    expect(DETAIL_SRC).toMatch(/data-testid="dunning-pause-btn"/);
    expect(DETAIL_SRC).toMatch(/'Resume reminders' : 'Pause reminders'/);
  });

  it('hits the dunning-pause endpoint and flips local state', () => {
    const idx = DETAIL_SRC.indexOf('async function toggleDunningPause');
    expect(idx).toBeGreaterThan(-1);
    const span = DETAIL_SRC.slice(idx, idx + 700);
    expect(span).toMatch(/dunning-pause`, \{ paused: next \}/);
  });

  it('normalization carries dunning_paused from the API payload', () => {
    expect(DETAIL_SRC).toMatch(/dunning_paused: Boolean\(payload\.dunning_paused\)/);
  });
});
