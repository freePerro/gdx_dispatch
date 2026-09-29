/**
 * The Leads list's Progress chip: a lead's progress rendered through the
 * REAL JobStateChip, so a lead reads exactly as its job does on the job pages.
 */
import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import JobStateChip from '../../components/JobStateChip.vue';
import { leadProgressAsJob } from '../leadProgress';

const stubs = { Tag: { props: ['value', 'severity'], template: '<span :data-sev="severity">{{ value }}</span>' } };
const chip = (progress) => mount(JobStateChip, {
  props: { job: leadProgressAsJob(progress), showDepositBadge: false },
  global: { stubs },
});

describe('lead progress chip', () => {
  it('Paid reads as a won terminal', () => {
    const w = chip({ stage: 'paid', type: 'won', label: 'Paid', is_finished: true, deposit_paid: false });
    expect(w.text()).toBe('Paid');
    expect(w.find('span').attributes('data-sev')).toBe('success');
  });

  it('a scheduled job with no appointment reads "Awaiting Schedule"', () => {
    const w = chip({ stage: 'scheduled', type: 'open', label: 'Scheduled', scheduled_at: null });
    expect(w.text()).toBe('Awaiting Schedule');
  });

  it('a scheduled job WITH an appointment stays "Scheduled"', () => {
    const w = chip({ stage: 'scheduled', type: 'open', label: 'Scheduled', scheduled_at: '2026-10-02T15:00:00Z' });
    expect(w.text()).toBe('Scheduled');
  });

  it('a pre-job stage with no schedule key is never relabelled', () => {
    expect(leadProgressAsJob({ stage: 'sold', type: 'open', label: 'Sold' })).not.toHaveProperty('scheduled_at');
    expect(chip({ stage: 'sold', type: 'open', label: 'Sold' }).text()).toBe('Sold');
  });

  it('Declined reads as lost', () => {
    const w = chip({ stage: 'declined', type: 'lost', label: 'Declined' });
    expect(w.find('span').attributes('data-sev')).toBe('danger');
  });

  it('no progress gives no job', () => {
    expect(leadProgressAsJob(null)).toBeNull();
  });
});
