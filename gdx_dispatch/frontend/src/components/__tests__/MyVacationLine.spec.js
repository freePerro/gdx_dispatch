/**
 * MyVacationLine — the tech's own vacation line, shared by the desktop and
 * mobile clock pages (GDXA-1902).
 *
 * Pins, named for the failure each prevents:
 *  - under hire_anniversary the label is the window, never `year` (the year
 *    the window starts in): a tech in January read "Vacation 2025";
 *  - under calendar_year (window Jan 1 – Dec 31) it is just the year;
 *  - a row with no window (an older server) still says its year;
 *  - a window date is not shifted a day by UTC parsing;
 *  - the line reads me (no technician named), says a failed read, hides on a
 *    403, and re-reads on refresh().
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
vi.mock('../../composables/useApi', () => ({ useApi: () => ({ get: apiGet }) }));

import MyVacationLine from '../MyVacationLine.vue';
import { vacationWindowLabel } from '../../utils/vacationWindow';

const stubs = {
  Tag: { props: ['value', 'severity'], template: '<span class="tag" :data-severity="severity">{{ value }}</span>' },
};

const CALENDAR = {
  technician_id: 'me', year: 2026, as_of: '2026-05-20',
  allowance_hours: 40, allowance_source: 'shop', used_hours: 16, taken_hours: 8,
  scheduled_hours: 8, remaining_hours: 24, overdrawn: false,
  window_start: '2026-01-01', window_end: '2026-12-31',
};
const ANNIVERSARY = {
  ...CALENDAR, year: 2025, as_of: '2026-01-14',
  window_start: '2025-06-03', window_end: '2026-06-02',
};

function answer(balance) {
  apiGet.mockResolvedValue({ year: 2026, balances: balance ? [balance] : [] });
}

async function mountLine() {
  const w = mount(MyVacationLine, { global: { stubs } });
  await flushPromises();
  return w;
}

beforeEach(() => {
  apiGet.mockReset();
});

describe('vacationWindowLabel', () => {
  it('says the year for a calendar-year window', () => {
    expect(vacationWindowLabel(CALENDAR)).toBe('2026');
  });

  it('says the window, not the start year, under hire_anniversary', () => {
    expect(vacationWindowLabel(ANNIVERSARY)).toBe('Jun 3 2025 – Jun 2 2026');
  });

  it('says the year only for exactly Jan 1 – Dec 31', () => {
    // A Jan 1 hire date gives the calendar year under either policy, so the
    // year is the truth there; a Jan 2 one does not.
    expect(vacationWindowLabel({ window_start: '2026-01-01', window_end: '2026-12-31' })).toBe('2026');
    expect(vacationWindowLabel({ window_start: '2026-01-02', window_end: '2027-01-01' }))
      .toBe('Jan 2 2026 – Jan 1 2027');
  });

  it('falls back to the year when the server sent no window', () => {
    expect(vacationWindowLabel({ year: 2026, window_start: null, window_end: null })).toBe('2026');
    expect(vacationWindowLabel({})).toBe('');
  });

  it('does not shift a day west of Greenwich', () => {
    // new Date('2025-03-01') is Feb 28 in Chicago; the label must not be.
    expect(vacationWindowLabel({ window_start: '2025-03-01', window_end: '2026-02-28' }))
      .toBe('Mar 1 2025 – Feb 28 2026');
  });
});

describe('MyVacationLine', () => {
  it('reads my own balance and labels a calendar year by its year', async () => {
    answer(CALENDAR);
    const w = await mountLine();
    expect(apiGet).toHaveBeenCalledWith('/api/timeclock/time-off/balances', expect.anything());
    const line = w.find('[data-testid="my-vacation-balance"]').text();
    expect(line).toContain('Vacation 2026:');
    expect(line).toContain('16h used');
    expect(line).toContain('(8h of it still ahead)');
    expect(line).toContain('of 40h');
    expect(w.find('[data-testid="my-vacation-remaining"]').text()).toBe('24h left');
  });

  it('labels an anniversary year by its window, never the start year alone', async () => {
    answer(ANNIVERSARY);
    const w = await mountLine();
    const label = w.find('[data-testid="my-vacation-window"]').text();
    expect(label).toBe('Vacation Jun 3 2025 – Jun 2 2026:');
    expect(label).not.toMatch(/Vacation 2025:/);
  });

  it('says "no allowance set" rather than 0h left', async () => {
    answer({ ...CALENDAR, allowance_hours: null, remaining_hours: null, scheduled_hours: 0 });
    const w = await mountLine();
    expect(w.find('[data-testid="my-vacation-balance"]').text()).toContain('no allowance set');
    expect(w.find('[data-testid="my-vacation-remaining"]').exists()).toBe(false);
  });

  it('shows an overdrawn balance as negative and in danger', async () => {
    answer({ ...CALENDAR, remaining_hours: -8.5, overdrawn: true });
    const w = await mountLine();
    const tag = w.find('[data-testid="my-vacation-remaining"]');
    expect(tag.text()).toBe('-8.50h left');
    expect(tag.attributes('data-severity')).toBe('danger');
  });

  it('says the read failed, including an empty answer', async () => {
    apiGet.mockRejectedValue(Object.assign(new Error('503'), { status: 503 }));
    let w = await mountLine();
    expect(w.find('[data-testid="my-vacation-error"]').exists()).toBe(true);
    answer(null);
    w = await mountLine();
    expect(w.find('[data-testid="my-vacation-error"]').exists()).toBe(true);
  });

  it('hides on a 403 rather than saying the read failed', async () => {
    apiGet.mockRejectedValue(Object.assign(new Error('403'), { status: 403 }));
    const w = await mountLine();
    expect(w.find('[data-testid="my-vacation-error"]').exists()).toBe(false);
    expect(w.find('[data-testid="my-vacation-balance"]').exists()).toBe(false);
  });

  it('re-reads on refresh()', async () => {
    answer(CALENDAR);
    const w = await mountLine();
    answer({ ...CALENDAR, used_hours: 24, remaining_hours: 16 });
    await w.vm.refresh();
    await flushPromises();
    expect(apiGet).toHaveBeenCalledTimes(2);
    expect(w.find('[data-testid="my-vacation-remaining"]').text()).toBe('16h left');
  });

  it('lands a class set on it on the rendered line', async () => {
    answer(CALENDAR);
    const w = mount(MyVacationLine, { attrs: { class: 'entries-section' }, global: { stubs } });
    await flushPromises();
    expect(w.find('[data-testid="my-vacation-balance"]').classes()).toContain('entries-section');
  });
});
