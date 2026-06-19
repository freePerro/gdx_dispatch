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
