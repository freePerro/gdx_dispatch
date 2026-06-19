/**
 * Slice 4 Wave 0b — the single frontend job-state reader.
 *
 * Pins: authoritative display_state mapping (type → severity/icon, stage
 * overrides), graceful legacy fallback that NEVER fabricates a terminal,
 * and null-safety. Every one of the ~49 surfaces depends on this — a
 * regression here is a regression everywhere.
 */
import { describe, expect, it } from 'vitest';
import { jobDisplayState } from './jobDisplayState';

describe('jobDisplayState — authoritative display_state', () => {
  it('maps won → success + check icon, finished', () => {
    const s = jobDisplayState({
      display_state: { stage: 'paid', type: 'won', label: 'Paid', is_finished: true },
    });
    expect(s).toEqual({
      stage: 'paid', type: 'won', label: 'Paid', isFinished: true,
      severity: 'success', icon: 'pi pi-check-circle', unverified: false,
    });
  });

  it('maps lost (Declined) → danger + times icon, finished', () => {
    const s = jobDisplayState({
      display_state: { stage: 'declined', type: 'lost', label: 'Declined', is_finished: true },
    });
    expect(s.severity).toBe('danger');
    expect(s.icon).toBe('pi pi-times-circle');
    expect(s.isFinished).toBe(true);
  });

  it('open service_call → info, no icon, not finished', () => {
    const s = jobDisplayState({
      display_state: { stage: 'service_call', type: 'open', label: 'Service Call', is_finished: false },
    });
    expect(s).toMatchObject({ severity: 'info', icon: '', isFinished: false, type: 'open' });
  });

  it('overdue stage override → warn + warning icon (beats plain open)', () => {
    const s = jobDisplayState({
      display_state: { stage: 'overdue', type: 'open', label: 'Overdue', is_finished: false },
    });
    expect(s.severity).toBe('warn');
    expect(s.icon).toBe('pi pi-exclamation-triangle');
  });

  it('ready_to_bill stage override → warn', () => {
    const s = jobDisplayState({
      display_state: { stage: 'ready_to_bill', type: 'open', label: 'Ready to Bill', is_finished: false },
    });
    expect(s.severity).toBe('warn');
  });

  it('isFinished is derived from type even if is_finished flag missing', () => {
    const s = jobDisplayState({
      display_state: { stage: 'cancelled', type: 'lost', label: 'Cancelled' },
    });
    expect(s.isFinished).toBe(true);
  });

  it('coerces an unknown type to open (never trusts a bad type)', () => {
    const s = jobDisplayState({
      display_state: { stage: 'weird', type: 'bogus', label: 'Weird' },
    });
    expect(s.type).toBe('open');
    expect(s.severity).toBe('info');
  });
});

describe('jobDisplayState — graceful fallback (no display_state)', () => {
  it('falls back to job.status, typed open, NEVER a fabricated terminal', () => {
    const s = jobDisplayState({ status: 'Scheduled' });
    expect(s).toMatchObject({
      label: 'Scheduled', type: 'open', isFinished: false, severity: 'info',
    });
  });

  it('falls back to lifecycle_stage and titleizes it', () => {
    const s = jobDisplayState({ lifecycle_stage: 'in_progress' });
    expect(s.label).toBe('In Progress');
    expect(s.type).toBe('open');
  });

  it('NEVER echoes the deceptive "Complete" as a clean state (auditor 2026-05-18)', () => {
    // The foundational lie this sprint kills: a paid job whose enrichment
    // failed must NOT render a clean "Complete". It renders explicitly
    // unverified, muted, NOT finished — never authoritative-looking.
    const s = jobDisplayState({ status: 'Complete' });
    expect(s.isFinished).toBe(false);
    expect(s.unverified).toBe(true);
    expect(s.severity).toBe('secondary');
    expect(s.icon).toBe('pi pi-question-circle');
    expect(s.label).toBe('Complete — sync pending');
    expect(s.label).not.toBe('Complete');
  });

  it('all deceptive-family legacy values get the unverified treatment', () => {
    for (const v of ['Completed', 'closed', 'DONE', 'finished']) {
      const s = jobDisplayState({ lifecycle_stage: v });
      expect(s.unverified).toBe(true);
      expect(s.severity).toBe('secondary');
      expect(s.isFinished).toBe(false);
      expect(s.label.endsWith('— sync pending')).toBe(true);
    }
  });

  it('non-deceptive legacy stages stay useful but are flagged unverified', () => {
    const s = jobDisplayState({ status: 'Scheduled' });
    expect(s.label).toBe('Scheduled');
    expect(s.unverified).toBe(true);
    expect(s.severity).toBe('info');
  });

  it('authoritative state is never flagged unverified', () => {
    const s = jobDisplayState({
      display_state: { stage: 'invoiced', type: 'open', label: 'Invoiced', is_finished: false },
    });
    expect(s.unverified).toBe(false);
  });
});

describe('jobDisplayState — null safety', () => {
  it.each([null, undefined, 42, 'x', {}])('safe default for %s', (bad) => {
    const s = jobDisplayState(bad);
    expect(s).toMatchObject({ stage: 'unknown', type: 'open', label: 'Unknown', isFinished: false });
  });
});
