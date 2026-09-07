/**
 * MobileTimeclockView — pins the contract:
 *  - Renders the status-card and clock-in button when not clocked in.
 *  - Switches to clock-out + start-break when clocked in (work entry active).
 *  - Calls /api/timeclock/clock-in on Clock In tap.
 *  - Today's entries render as cards (not a DataTable).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost }),
}));

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));

import MobileTimeclockView from '../MobileTimeclockView.vue';

const stubs = {
  Tag: { props: ['value', 'severity'], template: '<span class="tag">{{ value }}</span>' },
  Select: {
    props: ['modelValue', 'options'],
    emits: ['update:modelValue'],
    template: '<select />',
  },
  Button: {
    props: ['label', 'icon', 'severity', 'loading', 'disabled'],
    emits: ['click'],
    template: '<button class="big-btn" @click="$emit(\'click\')">{{ label }}</button>',
  },
};

describe('MobileTimeclockView', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset();
    // Disable real geolocation to keep the test deterministic. The view's
    // initGps guards with `'geolocation' in navigator` — stub a no-op
    // implementation so the guard passes but the callback never fires
    // (avoids touching real OS APIs in jsdom).
    Object.defineProperty(global.navigator, 'geolocation', {
      configurable: true,
      writable: true,
      value: {
        // Synchronously fire the error callback so refreshGps resolves
        // immediately. Otherwise the view's gpsWithTimeout waits 2s for
        // the timeout race winner — flushPromises won't wait that long.
        getCurrentPosition: (_ok, err) => {
          if (typeof err === 'function') err({ code: 1, message: 'denied' });
        },
      },
    });
  });

  it('#645 — the elapsed ticker shows WORKED time, not gross, and agrees with Today', async () => {
    // The ticker sat directly above "Today: X h" and computed gross wall clock
    // off clock_in_at, so after a 30-minute lunch the same card read 8:00:00
    // over Today 7.50h. Two numbers on one card that do not add up.
    apiGet.mockImplementation((url) => {
      if (url === '/api/timeclock/status') {
        return Promise.resolve({
          clocked_in: true,
          on_break: false,
          // Clocked in 8h ago, 30m lunch already taken.
          active_entry: {
            entry_type: 'work',
            clock_in_at: new Date(Date.now() - 8 * 3600 * 1000).toISOString(),
          },
          today_hours: 7.5,
          open_shift_elapsed_hours: 7.5,
        });
      }
      if (url === '/api/timeclock/entries') return Promise.resolve([]);
      if (url === '/api/jobs') return Promise.resolve([]);
      return Promise.resolve([]);
    });

    const wrapper = mount(MobileTimeclockView, { global: { stubs } });
    await flushPromises();

    const elapsed = wrapper.find('[data-test="mt-elapsed"]').text();
    // 7.5h worked -> 07:30:xx. The gross figure would render 08:00:xx.
    expect(elapsed.startsWith('07:30')).toBe(true);
    expect(elapsed.startsWith('08:00')).toBe(false);
    expect(wrapper.find('[data-test="mt-today-total"]').text()).toContain('7.50');
  });

  it('#645 — the ticker stops while on break', async () => {
    // The paid clock is not running during a break, so neither is its display.
    vi.useFakeTimers();
    try {
      apiGet.mockImplementation((url) => {
        if (url === '/api/timeclock/status') {
          return Promise.resolve({
            clocked_in: true,
            on_break: true,
            active_entry: {
              entry_type: 'work',
              clock_in_at: new Date(Date.now() - 4 * 3600 * 1000).toISOString(),
            },
            today_hours: 3.0,
            open_shift_elapsed_hours: 3.0,
          });
        }
        if (url === '/api/timeclock/entries') return Promise.resolve([]);
        if (url === '/api/jobs') return Promise.resolve([]);
        return Promise.resolve([]);
      });

      const wrapper = mount(MobileTimeclockView, { global: { stubs } });
      await flushPromises();
      const before = wrapper.find('[data-test="mt-elapsed"]').text();
      expect(before.startsWith('03:00')).toBe(true);

      await vi.advanceTimersByTimeAsync(5000);
      await flushPromises();
      expect(wrapper.find('[data-test="mt-elapsed"]').text()).toBe(before);
    } finally {
      vi.useRealTimers();
    }
  });

  it('renders Clock In when not clocked in', async () => {
    apiGet.mockImplementation((url) => {
      if (url === '/api/timeclock/status') return Promise.resolve({ clocked_in: false });
      if (url === '/api/timeclock/entries') return Promise.resolve([]);
      if (url === '/api/jobs') return Promise.resolve([]);
      return Promise.resolve([]);
    });

    const wrapper = mount(MobileTimeclockView, { global: { stubs } });
    await flushPromises();

    expect(wrapper.find('[data-test="mt-clock-in"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="mt-clock-out"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="mt-status-tag"]').text()).toBe('Clocked Out');
  });

  it('renders Clock Out + Start Break when clocked in (work entry)', async () => {
    apiGet.mockImplementation((url) => {
      if (url === '/api/timeclock/status') {
        return Promise.resolve({
          clocked_in: true,
          on_break: false,
          active_entry: { entry_type: 'work', clock_in_at: new Date().toISOString() },
          today_hours: 2.5,
        });
      }
      if (url === '/api/timeclock/entries') return Promise.resolve([]);
      if (url === '/api/jobs') return Promise.resolve([]);
      return Promise.resolve([]);
    });

    const wrapper = mount(MobileTimeclockView, { global: { stubs } });
    await flushPromises();

    expect(wrapper.find('[data-test="mt-clock-in"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="mt-clock-out"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="mt-break-start"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="mt-status-tag"]').text()).toBe('Clocked In');
    expect(wrapper.find('[data-test="mt-today-total"]').text()).toContain('2.50h');
  });

  it('renders End Break when on break', async () => {
    apiGet.mockImplementation((url) => {
      if (url === '/api/timeclock/status') {
        return Promise.resolve({
          clocked_in: true,
          on_break: true,
          active_entry: { entry_type: 'break', clock_in_at: new Date().toISOString() },
        });
      }
      if (url === '/api/timeclock/entries') return Promise.resolve([]);
      if (url === '/api/jobs') return Promise.resolve([]);
      return Promise.resolve([]);
    });

    const wrapper = mount(MobileTimeclockView, { global: { stubs } });
    await flushPromises();

    expect(wrapper.find('[data-test="mt-break-start"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="mt-break-end"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="mt-status-tag"]').text()).toBe('On Break');
  });

  it('POSTs /api/timeclock/clock-in when Clock In is tapped', async () => {
    apiGet.mockImplementation((url) => {
      if (url === '/api/timeclock/status') return Promise.resolve({ clocked_in: false });
      if (url === '/api/timeclock/entries') return Promise.resolve([]);
      if (url === '/api/jobs') return Promise.resolve([]);
      return Promise.resolve([]);
    });
    apiPost.mockResolvedValue({});

    const wrapper = mount(MobileTimeclockView, { global: { stubs } });
    await flushPromises();

    await wrapper.find('[data-test="mt-clock-in"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledWith('/api/timeclock/clock-in', expect.any(Object));
  });

  it('renders today\'s entries as cards when present', async () => {
    const today = new Date().toISOString();
    apiGet.mockImplementation((url) => {
      if (url === '/api/timeclock/status') return Promise.resolve({ clocked_in: false });
      if (url === '/api/timeclock/entries') {
        return Promise.resolve([
          { id: 'e1', entry_type: 'work', clock_in_at: today, clock_out_at: today, minutes: 90 },
          { id: 'e2', entry_type: 'break', clock_in_at: today, clock_out_at: today, minutes: 15 },
        ]);
      }
      if (url === '/api/jobs') return Promise.resolve([]);
      return Promise.resolve([]);
    });

    const wrapper = mount(MobileTimeclockView, { global: { stubs } });
    await flushPromises();

    const rows = wrapper.findAll('[data-test="mt-entry-row"]');
    expect(rows).toHaveLength(2);
    expect(rows[0].text()).toContain('WORK');
    expect(rows[1].text()).toContain('BREAK');
  });
});
