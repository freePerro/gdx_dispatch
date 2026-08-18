/**
 * Slice 4 Wave 0b — JobStateChip renders the canonical state.
 *
 * The chip is deliberately dumb (logic lives in the util, pinned in
 * jobDisplayState.spec.js); this proves the wiring: the right label /
 * severity / data-* reach the PrimeVue Tag, and fallback still renders.
 */
import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import JobStateChip from '../JobStateChip.vue';

// Tag stub recording the props the chip passes through (PrimeVue's real
// Tag adds theme markup we don't assert on).
const Tag = {
  name: 'Tag',
  props: ['value', 'severity', 'icon'],
  inheritAttrs: false,
  template:
    '<span class="tag-stub" :data-value="value" :data-severity="severity"' +
    ' :data-icon="icon || \'\'" :data-stage="$attrs[\'data-stage\']"' +
    ' :data-type="$attrs[\'data-type\']"' +
    ' :data-unverified="$attrs[\'data-unverified\'] || \'\'"' +
    ' :data-badge="$attrs[\'data-badge\'] || \'\'"' +
    ' :title="$attrs.title || \'\'">{{ value }}</span>',
};

const mountChip = (props) =>
  mount(JobStateChip, { props, global: { stubs: { Tag } } });

describe('JobStateChip', () => {
  it('renders authoritative Paid state (won → success)', () => {
    const w = mountChip({
      job: { display_state: { stage: 'paid', type: 'won', label: 'Paid', is_finished: true } },
    });
    const tag = w.find('.tag-stub');
    expect(tag.attributes('data-value')).toBe('Paid');
    expect(tag.attributes('data-severity')).toBe('success');
    expect(tag.attributes('data-type')).toBe('won');
    expect(tag.attributes('data-stage')).toBe('paid');
    expect(tag.attributes('data-icon')).toBe('pi pi-check-circle');
  });

  it('renders Declined (lost → danger)', () => {
    const w = mountChip({
      job: { display_state: { stage: 'declined', type: 'lost', label: 'Declined', is_finished: true } },
    });
    expect(w.find('.tag-stub').attributes('data-severity')).toBe('danger');
    expect(w.find('.tag-stub').attributes('data-type')).toBe('lost');
  });

  it('falls back to legacy status with neutral, non-terminal styling + unverified', () => {
    const w = mountChip({ job: { status: 'Scheduled' } });
    const tag = w.find('.tag-stub');
    expect(tag.attributes('data-value')).toBe('Scheduled');
    expect(tag.attributes('data-severity')).toBe('info');
    expect(tag.attributes('data-type')).toBe('open');
    expect(tag.attributes('data-unverified')).toBe('true');
    expect(tag.attributes('title')).toContain('not yet confirmed');
  });

  it('deceptive "Complete" fallback renders unverified/muted, NOT a clean done state', () => {
    // The regression guard for the auditor's foundational-lie finding.
    const w = mountChip({ job: { status: 'Complete' } });
    const tag = w.find('.tag-stub');
    expect(tag.attributes('data-value')).toBe('Complete — sync pending');
    expect(tag.attributes('data-severity')).toBe('secondary');
    expect(tag.attributes('data-unverified')).toBe('true');
  });

  it('showIcon=false suppresses the icon', () => {
    const w = mountChip({
      job: { display_state: { stage: 'paid', type: 'won', label: 'Paid', is_finished: true } },
      showIcon: false,
    });
    expect(w.find('.tag-stub').attributes('data-icon')).toBe('');
  });

  it('null job renders the safe default, does not throw', () => {
    const w = mountChip({ job: null });
    expect(w.find('.tag-stub').attributes('data-value')).toBe('Unknown');
  });

  // --- Deposit-paid badge (2026-08-18) -----------------------------------
  // A paid deposit must NOT flip the stage to "Paid" (that was the bug);
  // it renders as a companion badge next to the true work state.

  it('deposit_paid on an open stage renders the badge next to the state', () => {
    const w = mountChip({
      job: {
        display_state: {
          stage: 'scheduled', type: 'open', label: 'Scheduled',
          is_finished: false, deposit_paid: true,
        },
        scheduled_at: '2026-08-20T09:00:00Z',
      },
    });
    const tags = w.findAll('.tag-stub');
    expect(tags).toHaveLength(2);
    expect(tags[0].attributes('data-value')).toBe('Scheduled');
    const badge = w.find('[data-badge="deposit-paid"]');
    expect(badge.exists()).toBe(true);
    expect(badge.attributes('data-value')).toBe('Deposit paid');
    expect(badge.attributes('data-severity')).toBe('success');
  });

  it('badge hides on the Paid terminal (redundant there)', () => {
    const w = mountChip({
      job: {
        display_state: {
          stage: 'paid', type: 'won', label: 'Paid',
          is_finished: true, deposit_paid: true,
        },
      },
    });
    expect(w.findAll('.tag-stub')).toHaveLength(1);
  });

  it('no badge without deposit_paid, and showDepositBadge=false opts out', () => {
    const plain = mountChip({
      job: { display_state: { stage: 'scheduled', type: 'open', label: 'Scheduled', is_finished: false } },
    });
    expect(plain.findAll('.tag-stub')).toHaveLength(1);
    const optedOut = mountChip({
      job: {
        display_state: {
          stage: 'scheduled', type: 'open', label: 'Scheduled',
          is_finished: false, deposit_paid: true,
        },
      },
      showDepositBadge: false,
    });
    expect(optedOut.findAll('.tag-stub')).toHaveLength(1);
  });
});
