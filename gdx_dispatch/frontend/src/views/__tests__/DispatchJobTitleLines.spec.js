/**
 * Dispatch — job-title lines on board cards.
 *
 * Doug 2026-08-17: "The customer name shows up on the dispatch board but the
 * job name does not. it makes it very difficult to tell what a job is for if
 * there are several jobs for one customer."
 *
 * Contract pinned:
 *  1. Cards render the job title as its own line when the job has one.
 *  2. Cards with no title render no empty line.
 *  3. The dedupe guard: when displayCustomer already returns the title (a
 *     customer-less job after a future displayCustomer harmonization), the
 *     title line is suppressed — the same string must never print twice.
 *
 * Mirrors DispatchHoldingAreaJobTags.spec.js: a mini host reproduces the
 * card fragments (DispatchView is too heavy for unit tests); displayCustomer
 * and displayTitle are kept in lockstep with DispatchView.vue.
 */
import { describe, expect, it } from 'vitest';
import { mount } from '@vue/test-utils';
import { defineComponent, ref } from 'vue';

// Lockstep mirror of DispatchView.vue displayCustomer().
function displayCustomer(job) {
  if (!job.customer_name) return 'No customer attached (lead)';
  if (job.customer_name === 'Unknown Customer') return 'No customer attached (lead)';
  if (job.location_label) return `${job.customer_name} · ${job.location_label}`;
  return job.customer_name;
}

// Lockstep mirror of DispatchView.vue displayTitle().
function displayTitle(job) {
  const t = (job?.title || '').trim();
  return t && t !== displayCustomer(job) ? t : '';
}

const Host = defineComponent({
  setup() {
    const jobs = ref([
      { id: 'j1', customer_name: 'Anderson', title: 'Opener replacement — 2-car door' },
      { id: 'j2', customer_name: 'Anderson', title: 'Spring repair — north door' },
      { id: 'j3', customer_name: 'Anderson', title: '' }, // no title → no line
      // Guard case: if displayCustomer ever falls back to the title (as it
      // does in TechTimelineColumn/MobileDispatchView), the line must not
      // double-print. Simulated by title === rendered customer string.
      { id: 'j4', customer_name: 'No customer attached (lead)', title: 'No customer attached (lead)' },
    ]);
    return { jobs, displayCustomer, displayTitle };
  },
  template: `
    <div>
      <div v-for="job in jobs" :key="job.id" class="job-card" :data-testid="'card-' + job.id">
        <span class="job-customer">{{ displayCustomer(job) }}</span>
        <p v-if="displayTitle(job)" class="job-line job-title-line" :data-testid="'title-' + job.id">{{ displayTitle(job) }}</p>
      </div>
    </div>
  `,
});

describe('Dispatch — job-title lines on board cards', () => {
  it('renders each job title as its own line, so same-customer jobs are distinguishable', () => {
    const w = mount(Host);
    expect(w.find('[data-testid="title-j1"]').text()).toBe('Opener replacement — 2-car door');
    expect(w.find('[data-testid="title-j2"]').text()).toBe('Spring repair — north door');
    // Same customer on both cards — the titles are what tells them apart.
    expect(w.find('[data-testid="card-j1"]').text()).toContain('Anderson');
    expect(w.find('[data-testid="card-j2"]').text()).toContain('Anderson');
  });

  it('renders no title line when the job has no title', () => {
    const w = mount(Host);
    expect(w.find('[data-testid="title-j3"]').exists()).toBe(false);
  });

  it('suppresses the title line when it would repeat the customer string', () => {
    const w = mount(Host);
    expect(w.find('[data-testid="title-j4"]').exists()).toBe(false);
    // The string still appears exactly once (as the customer line).
    const text = w.find('[data-testid="card-j4"]').text();
    expect(text.match(/No customer attached \(lead\)/g)).toHaveLength(1);
  });
});
