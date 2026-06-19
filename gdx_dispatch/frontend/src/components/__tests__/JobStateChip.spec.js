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
});
