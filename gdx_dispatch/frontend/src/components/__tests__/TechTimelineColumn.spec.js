// Sprint dispatch-timeline (2026-05-21) — TechTimelineColumn unit tests.
// Focus is on the pure math (hour ticks, snap-to-15min) + the drop event
// contract. The DOM-positional tests stay tight: vitest's jsdom can't
// faithfully render the absolute-positioned layer, but it CAN verify the
// component emits the right shape and counts the right number of children.
import { describe, it, expect } from 'vitest';
import { mount } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import TechTimelineColumn from '../TechTimelineColumn.vue';

function mountColumn(overrides = {}) {
  const tech = {
    id: 'tech-1',
    name: 'Mike',
    effective_shift_start: '08:00',
    effective_shift_end: '17:00',
    effective_workdays: 31,
    isOffToday: false,
    ...(overrides.tech || {}),
  };
  return mount(TechTimelineColumn, {
    props: {
      tech,
      jobs: overrides.jobs || [],
      selectedDate: overrides.selectedDate || new Date(2026, 4, 21), // 2026-05-21
      pxPerHour: overrides.pxPerHour ?? 48,
    },
    global: { plugins: [PrimeVue] },
  });
}

describe('TechTimelineColumn', () => {
  it('renders one major tick per shift hour (8am–5pm = 10 ticks)', () => {
    const w = mountColumn();
    // 8, 9, 10, 11, 12, 13, 14, 15, 16, 17 = 10 hour labels
    const labels = w.findAll('.hour-label');
    expect(labels.length).toBe(10);
    expect(labels[0].text()).toBe('8am');
    expect(labels[4].text()).toBe('12pm');
    expect(labels[9].text()).toBe('5pm');
  });

  it('renders three minor (15-min) ticks per hour, between majors', () => {
    const w = mountColumn();
    // 9 hours of span * 3 quarter-marks per hour = 27 minor ticks
    expect(w.findAll('.minor-tick').length).toBe(27);
  });

  it('shows "Off today" panel and no body when tech is off', () => {
    const w = mountColumn({ tech: { isOffToday: true } });
    expect(w.find('.tech-timeline-off').exists()).toBe(true);
    expect(w.find('.tech-timeline-body').exists()).toBe(false);
  });

  it('puts date-only jobs in the tray, timed jobs in the body', () => {
    const sel = new Date(2026, 4, 21);
    const trayJob = {
      id: 'j1', customer_name: 'Acme',
      // Local-time midnight on the selected day. toISOString() will emit
      // a tz-shifted UTC string, but Date(...).getHours() reads back as 0
      // in the test env (matches DispatchView's local-time normalize).
      scheduled_at: new Date(2026, 4, 21, 0, 0).toISOString(),
      scheduled_duration_hours: 2,
    };
    const timedJob = {
      id: 'j2', customer_name: 'Beta',
      scheduled_at: new Date(2026, 4, 21, 10, 30).toISOString(),
      scheduled_duration_hours: 1.5,
    };
    const w = mountColumn({ jobs: [trayJob, timedJob], selectedDate: sel });
    // Tray chip exists for the date-only job
    expect(w.find('[data-testid="tray-job-j1"]').exists()).toBe(true);
    // Timeline block exists for the timed job
    expect(w.find('[data-testid="timeline-job-j2"]').exists()).toBe(true);
    // No swap
    expect(w.find('[data-testid="tray-job-j2"]').exists()).toBe(false);
    expect(w.find('[data-testid="timeline-job-j1"]').exists()).toBe(false);
  });

  it('snaps drop Y to 15-min increments (pure math)', () => {
    const w = mountColumn({ pxPerHour: 48 });
    // The component exposes dropYToISO for tests. Simulate a body rect
    // by passing a fake element whose getBoundingClientRect returns top=0.
    const fakeEl = { getBoundingClientRect: () => ({ top: 0 }) };
    const sel = new Date(2026, 4, 21);
    // y=0 → shift_start (8:00).
    let iso = w.vm.dropYToISO(0, fakeEl);
    let d = new Date(iso);
    expect(d.getHours()).toBe(8);
    expect(d.getMinutes()).toBe(0);
    // y=48 → 9:00.
    iso = w.vm.dropYToISO(48, fakeEl);
    d = new Date(iso);
    expect(d.getHours()).toBe(9);
    expect(d.getMinutes()).toBe(0);
    // y=60 → 9:15 (60/48 = 1.25h, snaps to 1.25 → 9:15).
    iso = w.vm.dropYToISO(60, fakeEl);
    d = new Date(iso);
    expect(d.getHours()).toBe(9);
    expect(d.getMinutes()).toBe(15);
    // y=66 → 9:15 (1.375h → snaps to 1.25, not 1.5 — round half to even
    // makes this delicate, so accept 9:15 or 9:30 as the snap landing
    // but make sure it's a quarter boundary).
    iso = w.vm.dropYToISO(66, fakeEl);
    d = new Date(iso);
    expect([0, 15, 30, 45]).toContain(d.getMinutes());
    // Match against the selected date (not today).
    expect(d.getFullYear()).toBe(sel.getFullYear());
    expect(d.getMonth()).toBe(sel.getMonth());
    expect(d.getDate()).toBe(sel.getDate());
  });

  it('marks overlapping jobs with the overlap class', () => {
    const sel = new Date(2026, 4, 21);
    const a = {
      id: 'a', customer_name: 'A',
      scheduled_at: new Date(2026, 4, 21, 10, 0).toISOString(),
      scheduled_duration_hours: 2,
    };
    // Overlaps 10:30–11:30 with A (10:00–12:00)
    const b = {
      id: 'b', customer_name: 'B',
      scheduled_at: new Date(2026, 4, 21, 10, 30).toISOString(),
      scheduled_duration_hours: 1,
    };
    const w = mountColumn({ jobs: [a, b], selectedDate: sel });
    const aBlock = w.find('[data-testid="timeline-job-a"]');
    const bBlock = w.find('[data-testid="timeline-job-b"]');
    expect(aBlock.classes()).toContain('job-block--overlap');
    expect(bBlock.classes()).toContain('job-block--overlap');
  });

  it('exposes job blocks as keyboard-focusable buttons with an aria-label', () => {
    const sel = new Date(2026, 4, 21);
    const job = {
      id: 'k1', customer_name: 'Acme',
      scheduled_at: new Date(2026, 4, 21, 10, 0).toISOString(),
      scheduled_duration_hours: 2,
    };
    const w = mountColumn({ jobs: [job], selectedDate: sel });
    const block = w.find('[data-testid="timeline-job-k1"]');
    expect(block.attributes('role')).toBe('button');
    expect(block.attributes('tabindex')).toBe('0');
    expect(block.attributes('aria-label')).toContain('Acme');
    expect(block.attributes('aria-label')).toContain('2h');
  });

  it('emits open-drawer on Enter and Space just like click', async () => {
    const sel = new Date(2026, 4, 21);
    const job = {
      id: 'k2', customer_name: 'Beta',
      scheduled_at: new Date(2026, 4, 21, 10, 0).toISOString(),
      scheduled_duration_hours: 1,
    };
    const w = mountColumn({ jobs: [job], selectedDate: sel });
    const block = w.find('[data-testid="timeline-job-k2"]');
    await block.trigger('keydown', { key: 'Enter' });
    expect(w.emitted('open-drawer')).toHaveLength(1);
    expect(w.emitted('open-drawer')[0][0].id).toBe('k2');
    await block.trigger('keydown', { key: ' ' });
    expect(w.emitted('open-drawer')).toHaveLength(2);
    // Same payload as the click path.
    await block.trigger('click');
    expect(w.emitted('open-drawer')).toHaveLength(3);
    expect(w.emitted('open-drawer')[2][0]).toBe(w.emitted('open-drawer')[0][0]);
  });

  it('exposes tray chips as keyboard-focusable buttons that open the drawer', async () => {
    const sel = new Date(2026, 4, 21);
    const trayJob = {
      id: 'k3', customer_name: 'Gamma',
      scheduled_at: new Date(2026, 4, 21, 0, 0).toISOString(),
      effective_duration_hours: 2,
    };
    const w = mountColumn({ jobs: [trayJob], selectedDate: sel });
    const chip = w.find('[data-testid="tray-job-k3"]');
    expect(chip.attributes('role')).toBe('button');
    expect(chip.attributes('tabindex')).toBe('0');
    expect(chip.attributes('aria-label')).toContain('Gamma');
    await chip.trigger('keydown', { key: 'Enter' });
    expect(w.emitted('open-drawer')[0][0].id).toBe('k3');
  });

  it('marks a block that overflows the shift end with the overflow class', () => {
    const sel = new Date(2026, 4, 21);
    // 16:00 + 2h = 18:00 — shift ends at 17:00, so 1h overflow.
    const job = {
      id: 'big', customer_name: 'Big job',
      scheduled_at: new Date(2026, 4, 21, 16, 0).toISOString(),
      scheduled_duration_hours: 2,
    };
    const w = mountColumn({ jobs: [job], selectedDate: sel });
    const block = w.find('[data-testid="timeline-job-big"]');
    expect(block.classes()).toContain('job-block--overflow');
    expect(block.find('.block-overflow').text()).toMatch(/over/);
  });
});
