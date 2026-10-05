/**
 * PerformanceView (GDXA-174) — pins:
 *  1. It reads the `{users: [...]}` envelope the endpoint actually returns.
 *     It used to read `Array.isArray(r) ? r : r?.items`, so the table was
 *     always empty.
 *  2. It sends `period=YYYY-MM`, the only filter the endpoint declares.
 *  3. A stat the server left null renders "—", never "0".
 *  4. A month with nothing recorded shows the "not enough data yet" state.
 *  5. A read failure raises the banner.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import PrimeVue from 'primevue/config';

const apiGet = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet }),
}));
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: vi.fn() }),
}));

import PerformanceView from '../PerformanceView.vue';

function user(id, name, stats = {}, unavailable = {}) {
  return {
    id,
    name,
    email: `${name}@example.test`,
    role: 'technician',
    stats: {
      jobs_completed: 0,
      revenue: 0,
      avg_job_value: null,
      estimates_created: null,
      estimates_accepted: null,
      hours_worked: null,
      tasks_completed: 0,
      commission_earned: 0,
      safety_checklists: 0,
      ...stats,
    },
    unavailable: {
      avg_job_value: 'no_data',
      hours_worked: 'no_data',
      estimates_created: 'not_recorded',
      estimates_accepted: 'not_recorded',
      ...unavailable,
    },
  };
}

function mountView() {
  return mount(PerformanceView, {
    global: { plugins: [PrimeVue], stubs: { DatePicker: true } },
  });
}

describe('PerformanceView', () => {
  beforeEach(() => {
    apiGet.mockReset();
  });

  it('renders rows from the users envelope and asks for one month', async () => {
    apiGet.mockResolvedValue({
      users: [
        user('u1', 'Alex', { jobs_completed: 2, revenue: 900, avg_job_value: 450, hours_worked: 11.5 },
          { avg_job_value: undefined, hours_worked: undefined }),
        user('u2', 'Blair'),
        user('u3', 'Casey', { hours_worked: 0.03 }, { hours_worked: undefined }),
      ],
      period: '2026-03',
    });
    const w = mountView();
    await flushPromises();
    // Two minutes on the clock is not "0.0".
    expect(w.find('[data-testid="hours-u3"]').text()).toBe('0.03');

    expect(apiGet).toHaveBeenCalledTimes(1);
    expect(apiGet.mock.calls[0][0]).toMatch(/^\/api\/performance\/users\?period=\d{4}-\d{2}$/);
    const table = w.find('[data-testid="performance-table"]');
    expect(table.exists()).toBe(true);
    expect(table.text()).toContain('Alex');
    expect(table.text()).toContain('Blair');
    expect(w.find('[data-testid="hours-u1"]').text()).toBe('11.5');
  });

  it('shows a dash, not a zero, for a stat the server could not give', async () => {
    apiGet.mockResolvedValue({
      users: [
        user('u1', 'Alex', { jobs_completed: 1, hours_worked: 4 }, { hours_worked: undefined }),
        user('u2', 'Blair'),
      ],
    });
    const w = mountView();
    await flushPromises();

    const idle = w.find('[data-testid="hours-u2"]');
    expect(idle.text()).toBe('—');
    expect(idle.attributes('title')).toMatch(/Nothing recorded/);
  });

  it('shows the not-enough-data state when the month has nothing recorded', async () => {
    apiGet.mockResolvedValue({ users: [user('u1', 'Alex'), user('u2', 'Blair')] });
    const w = mountView();
    await flushPromises();

    expect(w.find('[data-testid="perf-empty"]').exists()).toBe(true);
    expect(w.text()).toContain('Not enough data yet');
    expect(w.find('[data-testid="performance-table"]').exists()).toBe(false);
  });

  it('raises the banner when a figure could not be read', async () => {
    apiGet.mockResolvedValue({
      users: [user('u1', 'Alex', { jobs_completed: 3 }, { hours_worked: 'read_failed' })],
    });
    const w = mountView();
    await flushPromises();

    expect(w.find('[data-testid="perf-read-failed"]').exists()).toBe(true);
    expect(w.find('[data-testid="hours-u1"]').text()).toBe('—');
  });

  it('a tech still clocked in is activity, and the hours total says who it leaves out', async () => {
    apiGet.mockResolvedValue({
      users: [
        user('u1', 'Alex', {}, { hours_worked: 'in_progress' }),
        user('u2', 'Blair', { hours_worked: 6 }, { hours_worked: undefined }),
      ],
    });
    const w = mountView();
    await flushPromises();

    expect(w.find('[data-testid="perf-empty"]').exists()).toBe(false);
    const cell = w.find('[data-testid="hours-u1"]');
    expect(cell.text()).toBe('—');
    expect(cell.attributes('title')).toMatch(/Clocked in now/);
    expect(w.find('[data-testid="total-hours"]').text()).toContain('6');
    expect(w.find('[data-testid="total-hours-partial"]').text()).toContain('Leaves out 1 person');
  });

  it('does not tell a reader who may not see hours that there are none', async () => {
    apiGet.mockResolvedValue({ users: [user('u1', 'Alex', {}, { hours_worked: 'restricted' })] });
    const w = mountView();
    await flushPromises();

    expect(w.find('[data-testid="perf-empty"]').exists()).toBe(false);
    expect(w.find('[data-testid="hours-u1"]').attributes('title')).toMatch(/dispatch and admin/);
  });

  it('does not claim "nothing recorded" when every read failed', async () => {
    const failed = { jobs_completed: null, revenue: null, tasks_completed: null, safety_checklists: null };
    const why = {
      jobs_completed: 'read_failed', revenue: 'read_failed', hours_worked: 'read_failed',
      tasks_completed: 'read_failed', safety_checklists: 'read_failed',
    };
    apiGet.mockResolvedValue({ users: [user('u1', 'Alex', failed, why)] });
    const w = mountView();
    await flushPromises();

    expect(w.find('[data-testid="perf-read-failed"]').exists()).toBe(true);
    expect(w.find('[data-testid="perf-empty"]').exists()).toBe(false);
    expect(w.find('[data-testid="hours-u1"]').text()).toBe('—');
  });
});
