/**
 * Dispatch intake queue × self-assigned jobs (2026-08-17 field report).
 *
 * The mobile "Assign to me" flow puts a tech on a job at create — but the
 * job still has no date. The intake queue ("New Jobs to Schedule") used to
 * drop ANY job with a tech, so every self-assigned job would have skipped
 * the queue and sat dateless in the tech's column on every selected day:
 * the 2026-08-10 "I made a job and it never showed up on dispatch"
 * invisibility bug wearing a new hat. The /audit of the self-assign fix
 * caught exactly this before it shipped.
 *
 * Pinned:
 *  1. Filter contract — a job leaves the queue only when it has BOTH a
 *     tech and a date. Teched-but-dateless stays in (card names the tech);
 *     parked (genuine holding areas) stays out; completed stays out.
 *  2. Static-source guard — the real DispatchView still filters on
 *     (technician_id && scheduled_at) and renders the "needs a date" line
 *     for teched-but-dateless queue cards.
 *
 * Harness mirrors DispatchCompleteFlow.spec.js: re-implement the computed's
 * contract in isolation, then hold the 1700-line view to it by source.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const RTS_ID = 'area-rts';
const PARKED_ID = 'area-parts';
const KNOWN_AREAS = new Set([RTS_ID, PARKED_ID]);

// The unassignedJobs contract, minus the dated-row/day plumbing that needs
// the whole view (dated rows are unchanged by the self-assign fix).
function inIntakeQueue(job) {
  if (job.completed) return false;
  if (job.technician_id && job.scheduled_at) return false;
  if (job.holding_area_id) {
    const parked = job.holding_area_id !== RTS_ID && KNOWN_AREAS.has(job.holding_area_id);
    if (parked) return false;
  }
  if (!job.scheduled_at) return true;
  return true; // dated branch: day-matching plumbing not under test here
}

describe('intake queue keeps self-assigned dateless jobs', () => {
  it('a teched-but-dateless job stays in the queue', () => {
    expect(inIntakeQueue({ technician_id: 'tech-1', scheduled_at: null, holding_area_id: RTS_ID })).toBe(true);
  });

  it('a job with both a tech and a date has left the queue', () => {
    expect(inIntakeQueue({ technician_id: 'tech-1', scheduled_at: '2026-08-18T14:00:00Z', holding_area_id: null })).toBe(false);
  });

  it('untouched cases: dateless-unteched in, parked out, completed out', () => {
    expect(inIntakeQueue({ technician_id: null, scheduled_at: null, holding_area_id: RTS_ID })).toBe(true);
    expect(inIntakeQueue({ technician_id: 'tech-1', scheduled_at: null, holding_area_id: PARKED_ID })).toBe(false);
    expect(inIntakeQueue({ completed: true, technician_id: 'tech-1', scheduled_at: null })).toBe(false);
  });
});

describe('static-source guard — DispatchView honors the contract', () => {
  const SRC = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), '../DispatchView.vue'),
    'utf8',
  );

  it('queue filter drops a job only when it has BOTH tech and date', () => {
    expect(SRC).toContain('if (j.technician_id && j.scheduled_at) return false;');
    // The old any-tech exclusion must not come back.
    expect(SRC).not.toContain('if (j.technician_id) return false;');
  });

  it('queue card renders who has a dateless job and that it needs a date', () => {
    expect(SRC).toContain('unassigned-tech-${job.id}');
    expect(SRC).toContain('needs a date');
  });
});
