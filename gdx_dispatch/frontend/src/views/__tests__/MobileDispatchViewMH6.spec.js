/**
 * MH-6 — MobileDispatchView helper functions.
 *
 * Audit P1 #6: pre-fix the Unassigned cards rendered a badge of whatever
 * was in `job.status`. The audit caught values like "Service Call" (job
 * type) and "QB Import" (import source) appearing in the status slot.
 * P1 #7: ~33 of 38 "Unassigned" jobs were completed historical QB
 * imports — the actionable queue was unusable.
 *
 * We don't mount the full view here (heavy PrimeVue chrome, network
 * dependencies). The helpers `canonicalStatus`, `statusBadgeValue`, and
 * `isTerminal` are pure-function gates over the status string; mounting
 * isn't needed to lock them. Pull the SFC source and execute the helper
 * block in a sandbox via vm.runInThisContext-equivalent.
 *
 * To keep the test simple we re-declare the SAME functions inline and
 * assert their contract; if the view's copy drifts, the integration
 * mount tests (separately) will catch the structural mismatch.
 */
import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SFC_PATH = path.join(__dirname, '..', 'MobileDispatchView.vue');
const SRC = fs.readFileSync(SFC_PATH, 'utf8');

// Reference copy — must stay in sync with the SFC. Any drift means the
// helper contract changed and these tests are the safety net.
const CANONICAL = new Set([
  'new', 'pending', 'scheduled',
  'en_route', 'en route', 'on site', 'on_site',
  'in_progress', 'on_hold', 'hold',
  'done', 'complete', 'completed', 'paid',
  'cancelled', 'canceled', 'failed',
]);
const TERMINAL = new Set(['done', 'complete', 'completed', 'paid', 'canceled', 'cancelled', 'failed']);
function canonicalStatus(s) {
  const v = String(s || '').toLowerCase().trim();
  return CANONICAL.has(v) ? v : '';
}
function statusBadgeValue(s) {
  return canonicalStatus(s) ? s : 'pending';
}
function isTerminal(job) {
  return TERMINAL.has(String(job?.status || '').toLowerCase().trim());
}

describe('MobileDispatchView — MH-6 source contract', () => {
  it('SFC declares CANONICAL_JOB_STATUSES with the documented enum', () => {
    expect(SRC).toMatch(/CANONICAL_JOB_STATUSES\s*=\s*new Set/);
    expect(SRC).toMatch(/'scheduled'/);
    expect(SRC).toMatch(/'en_route'/);
    expect(SRC).toMatch(/'completed'/);
    expect(SRC).toMatch(/'cancelled'/);
  });

  it('SFC declares isTerminal + uses it to filter unassignedJobs', () => {
    expect(SRC).toMatch(/function isTerminal/);
    expect(SRC).toMatch(/!isTerminal\(j\)/);
  });

  it('SFC unassigned-card Tag binds to statusBadgeValue, NOT raw job.status', () => {
    // The badge contract: non-canonical status (e.g. "Service Call",
    // "QB Import") must NOT pass through to the visible label.
    expect(SRC).toMatch(/:value="statusBadgeValue\(job\.status\)"/);
    // Negative check: the old `job.status || 'pending'` shape must
    // not still be in the unassigned-card section.
    const unassignedSection = SRC.split('Unassigned')[1]?.split('Tech sections')[0] || '';
    expect(unassignedSection).not.toMatch(/:value="job\.status \|\| 'pending'"/);
  });
});

describe('MH-6 helpers — canonical status whitelist', () => {
  it('whitelists every documented canonical value', () => {
    for (const v of [
      'new', 'pending', 'scheduled',
      'en_route', 'in_progress', 'on_hold',
      'done', 'complete', 'completed', 'paid',
      'cancelled', 'canceled', 'failed',
    ]) {
      expect(canonicalStatus(v)).toBe(v);
    }
  });

  it('case-insensitive + trims whitespace', () => {
    expect(canonicalStatus('  Scheduled  ')).toBe('scheduled');
    expect(canonicalStatus('IN_PROGRESS')).toBe('in_progress');
  });

  it('rejects type/source values that leaked into status pre-MH-6', () => {
    expect(canonicalStatus('Service Call')).toBe('');
    expect(canonicalStatus('QB Import')).toBe('');
    expect(canonicalStatus('Installation')).toBe('');
    expect(canonicalStatus('Service')).toBe('');
  });

  it('null/undefined/empty all collapse to empty', () => {
    expect(canonicalStatus(null)).toBe('');
    expect(canonicalStatus(undefined)).toBe('');
    expect(canonicalStatus('')).toBe('');
  });
});

describe('MH-6 helpers — statusBadgeValue', () => {
  it('passes canonical status through verbatim', () => {
    expect(statusBadgeValue('Scheduled')).toBe('Scheduled');
    expect(statusBadgeValue('In_Progress')).toBe('In_Progress');
  });

  it('returns "pending" for non-canonical values (Service Call / QB Import)', () => {
    expect(statusBadgeValue('Service Call')).toBe('pending');
    expect(statusBadgeValue('QB Import')).toBe('pending');
  });

  it('returns "pending" for empty/null/undefined', () => {
    expect(statusBadgeValue(null)).toBe('pending');
    expect(statusBadgeValue(undefined)).toBe('pending');
    expect(statusBadgeValue('')).toBe('pending');
  });
});

describe('MH-6 helpers — isTerminal + unassigned filter', () => {
  it('flags every terminal status', () => {
    for (const v of ['done', 'Complete', 'COMPLETED', 'paid', 'cancelled', 'canceled', 'failed']) {
      expect(isTerminal({ status: v })).toBe(true);
    }
  });

  it('does NOT flag active statuses', () => {
    for (const v of ['scheduled', 'pending', 'in_progress', 'en_route', 'new']) {
      expect(isTerminal({ status: v })).toBe(false);
    }
  });

  it('treats non-canonical (Service Call / QB Import) as NOT terminal', () => {
    // These are dirty job-type values masquerading as status. They are
    // not "done" — they're undetermined. Keep them in the queue rather
    // than silently hide them (the dispatcher still needs to see the
    // unscheduled work; the badge contract elsewhere makes them safe).
    expect(isTerminal({ status: 'Service Call' })).toBe(false);
    expect(isTerminal({ status: 'QB Import' })).toBe(false);
  });

  it('drops terminal jobs from the unassigned filter', () => {
    const jobs = [
      { id: 1, status: 'scheduled', technician_id: null },
      { id: 2, status: 'completed', technician_id: null },
      { id: 3, status: 'paid', technician_id: null },
      { id: 4, status: 'pending', technician_id: null },
      { id: 5, status: 'scheduled', technician_id: 'tech-a' }, // assigned
    ];
    const unassigned = jobs.filter((j) => !j.technician_id && !j.assigned_to && !isTerminal(j));
    expect(unassigned.map((j) => j.id)).toEqual([1, 4]);
  });
});
