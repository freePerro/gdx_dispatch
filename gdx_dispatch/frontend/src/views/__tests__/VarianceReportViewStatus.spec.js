/**
 * VarianceReportView status colors (GDXA-1944) — the API returns job.status
 * as a display label ("In Progress", "Complete", "Cancelled"), not the
 * lifecycle_stage key, and every label used to fall through to "secondary".
 */
import { describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { inject, provide, toRef } from 'vue';

const apiGet = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet }),
}));
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: vi.fn() }),
}));

import VarianceReportView from '../VarianceReportView.vue';

const stubs = {
  Button: true,
  Card: true,
  DatePicker: true,
  EmptyState: true,
  // The table hands its rows to the status column the way PrimeVue does:
  // the column's #body slot renders once per row with { data }.
  DataTable: {
    props: ['value'],
    setup(props) {
      provide('rows', toRef(props, 'value'));
    },
    template: '<div><slot /></div>',
  },
  Column: {
    props: ['field'],
    setup() {
      return { rows: inject('rows') };
    },
    template: '<div v-if="field === \'status\'"><template v-for="(row, i) in rows" :key="i"><slot name="body" :data="row" /></template></div>',
  },
  Tag: {
    props: ['value', 'severity'],
    template: '<span class="tag" :data-value="value" :data-severity="severity">{{ value }}</span>',
  },
};

async function severities(labels) {
  apiGet.mockResolvedValueOnce({
    summary: { total_jobs: labels.length },
    details: labels.map((status, i) => ({ job_id: `j${i}`, status })),
  });
  const w = mount(VarianceReportView, { global: { stubs } });
  await flushPromises();
  return Object.fromEntries(
    w.findAll('.tag').map((t) => [t.attributes('data-value'), t.attributes('data-severity')]),
  );
}

describe('VarianceReportView status severity', () => {
  it('colors the display labels the API actually returns', async () => {
    expect(
      await severities(['Lead', 'Service Call', 'Estimate', 'Scheduled', 'In Progress', 'Complete', 'Cancelled']),
    ).toEqual({
      Lead: 'info',
      'Service Call': 'info',
      Estimate: 'info',
      Scheduled: 'warn',
      'In Progress': 'info',
      Complete: 'success',
      Cancelled: 'danger',
    });
  });

  it('accepts lifecycle_stage keys and the US spelling of canceled', async () => {
    expect(await severities(['in_progress', 'completed', 'Canceled'])).toEqual({
      in_progress: 'info',
      completed: 'success',
      Canceled: 'danger',
    });
  });

  it('leaves unknown or empty status neutral', async () => {
    expect(await severities(['On Hold', ''])).toEqual({ 'On Hold': 'secondary', '': 'secondary' });
  });
});
