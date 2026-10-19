/**
 * Customer portal — status tag colour (GDXA-1943).
 *
 * The jobs table feeds the tag the job's lifecycle_stage, whose enum
 * (Job.lifecycle_stage, models/tenant_models.py) includes cancelled, lead,
 * service_call and estimate. The severity map knew none of them, so a
 * cancelled job showed the same neutral grey as an unknown value.
 *
 * Pinned:
 *  1. A cancelled job from /portal/jobs renders a coloured tag, through the
 *     real mount and the real template binding.
 *  2. Every lifecycle_stage enum value has a colour of its own.
 *  3. Display labels ("In Progress", "Complete", "Canceled", "Cancelled")
 *     colour the same as their enum key.
 *  4. The estimate and invoice values the portal API returns keep their colours.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {}, params: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import CustomerPortalView from '../CustomerPortalView.vue';

const MODELS = readFileSync(join(__dirname, '..', '..', '..', '..', 'models', 'tenant_models.py'), 'utf8');
const LIFECYCLE_ENUM = MODELS
  .match(/lifecycle_stage: Mapped\[str\] = mapped_column\(Enum\(([^)]*?), name="job_lifecycle_stage"/)[1]
  .match(/"([a-z_]+)"/g)
  .map((s) => s.slice(1, -1));

const stubs = {
  Button: { props: ['label'], template: '<button>{{ label }}</button>' },
  DataTable: {
    props: ['value'],
    template: '<div :data-testid="$attrs[\'data-testid\']"><div v-if="value && value.length" class="row"><slot /></div></div>',
    inheritAttrs: false,
  },
  Column: {
    props: ['field', 'header'],
    template: '<div><slot name="body" :data="$parent.$props.value[0]" /></div>',
  },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /></div>' },
  Image: { template: '<img />' },
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot name="footer" /></div>' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<div><slot /></div>' },
  TabPanels: { template: '<div><slot /></div>' },
  TabPanel: { template: '<div><slot /></div>' },
  Tag: { props: ['value', 'severity'], template: '<span class="tag" :data-severity="severity">{{ value }}</span>' },
  Message: { template: '<div><slot /></div>' },
  InputText: { template: '<input />' },
  Password: { template: '<input type="password" />' },
  Checkbox: { template: '<input type="checkbox" />' },
  Divider: { template: '<hr />' },
  ProgressSpinner: { template: '<div />' },
  Toast: { template: '<div />' },
};

async function mountWithJobs(jobs) {
  vi.stubGlobal('fetch', vi.fn(async (url) => {
    const u = String(url);
    if (u.includes('/portal/context')) return { ok: true, json: async () => ({ company: { name: 'GDX' } }) };
    if (u.includes('/portal/jobs')) return { ok: true, json: async () => jobs };
    return { ok: true, json: async () => [] };
  }));
  sessionStorage.setItem('gdx_portal_jwt', 'portal-token');
  const wrapper = mount(CustomerPortalView, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
});

describe('customer portal — status tag colour', () => {
  it('renders a cancelled job from /portal/jobs with a coloured tag', async () => {
    const wrapper = await mountWithJobs([
      { id: 'job-1', title: 'Opener install', lifecycle_stage: 'cancelled', dispatch_status: 'unassigned', photo_count: 0 },
    ]);
    const tag = wrapper.findAll('span.tag').find((t) => t.text() === 'cancelled');
    expect(tag, 'cancelled job tag rendered').toBeTruthy();
    expect(tag.attributes('data-severity')).toBe('danger');
  });

  it('gives every lifecycle_stage enum value a colour of its own', async () => {
    expect(LIFECYCLE_ENUM).toEqual(
      ['lead', 'service_call', 'estimate', 'scheduled', 'in_progress', 'completed', 'cancelled'],
    );
    const wrapper = await mountWithJobs([]);
    const expected = {
      lead: 'secondary', service_call: 'info', estimate: 'info', scheduled: 'info',
      in_progress: 'info', completed: 'success', cancelled: 'danger',
    };
    for (const stage of LIFECYCLE_ENUM) {
      expect(wrapper.vm.statusSeverity(stage), stage).toBe(expected[stage]);
    }
  });

  it('colours a display label the same as its enum key', async () => {
    const wrapper = await mountWithJobs([]);
    const sev = wrapper.vm.statusSeverity;
    expect(sev('In Progress')).toBe(sev('in_progress'));
    expect(sev('in progress')).toBe(sev('in_progress'));
    expect(sev('Complete')).toBe(sev('completed'));
    expect(sev('Completed')).toBe(sev('completed'));
    expect(sev('Cancelled')).toBe('danger');
    expect(sev('Canceled')).toBe('danger');
    expect(sev('Service Call')).toBe(sev('service_call'));
  });

  it('keeps the estimate and invoice colours the portal API returns', async () => {
    const wrapper = await mountWithJobs([]);
    const sev = wrapper.vm.statusSeverity;
    // routers/portal.py: CUSTOMER_VISIBLE_ESTIMATE_STATUSES ("rejected" is sent as "sent").
    expect(sev('sent')).toBe('info');
    expect(sev('accepted')).toBe('success');
    expect(sev('declined')).toBe('danger');
    expect(sev('expired')).toBe('secondary');
    // _payment_status plus the derived "overdue".
    expect(sev('paid')).toBe('success');
    expect(sev('unpaid')).toBe('warn');
    expect(sev('overdue')).toBe('danger');
    expect(sev(null)).toBe('secondary');
    expect(sev('something-new')).toBe('secondary');
  });
});
