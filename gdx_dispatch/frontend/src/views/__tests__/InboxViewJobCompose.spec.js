/**
 * "Email about this job" → composer attached to the job (P2.5).
 *
 * The marker is the whole mechanism: without job_id on the send payload the
 * subject carries no `[Job #…]`, and the customer's reply never links back.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/inbox', query: { job_id: 'j-77', job_label: 'JOB-2026-014', to: 'alice@example.com', subject: 'JOB-2026-014 — update' } }),
  useRouter: () => ({ replace: () => {} }),
}));

const toastAdd = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));

import InboxView from '../InboxView.vue';

const STUBS = {
  Tree: { template: '<div />' },
  ContextMenu: { template: '<div />', methods: { show() {}, hide() {} } },
  Menu: { template: '<div />', methods: { toggle() {}, hide() {} } },
  Popover: { template: '<div />', methods: { toggle() {}, hide() {} } },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /><slot name="footer" /></div>' },
  TreeSelect: { template: '<div />' },
  Button: {
    props: ['label', 'icon', 'disabled', 'severity', 'outlined', 'size', 'loading'],
    template: '<button :data-test="$attrs[\'data-test\']" @click="$emit(\'click\')"><slot>{{ label }}</slot></button>',
    inheritAttrs: false,
  },
  InputText: { props: ['modelValue'], template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />' },
  EmailBodyFrame: { template: '<div />' },
  EmailAttachments: { template: '<div />' },
  RouterLink: { template: '<a><slot /></a>' },
};

function mkResponse(body) {
  return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => body, text: async () => JSON.stringify(body) };
}

let originalFetch;
let calls;

beforeEach(() => {
  setActivePinia(createPinia());
  originalFetch = globalThis.fetch;
  calls = [];
  globalThis.fetch = vi.fn(async (url, init = {}) => {
    calls.push({ url, method: (init.method || 'GET').toUpperCase(), body: init.body ? JSON.parse(init.body) : null });
    if (url.endsWith('/api/outlook/folders')) return mkResponse([]);
    if (url.includes('/api/outlook/messages?')) return mkResponse({ items: [], has_more: false, next_offset: 0 });
    return mkResponse({ ok: true });
  });
  globalThis.localStorage.setItem('access_token', 'fake.jwt.token');
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.localStorage.clear();
});

describe('InboxView — compose from a job (P2.5)', () => {
  it('opens the composer prefilled from the query and shows the job chip', async () => {
    const w = mount(InboxView, { global: { stubs: STUBS } });
    await flushPromises();
    expect(w.find('[data-test="inbox-compose"]').exists()).toBe(true);
    expect(w.find('[data-test="compose-to"]').element.value).toBe('alice@example.com');
    expect(w.find('[data-test="compose-job-chip"]').text()).toContain('JOB-2026-014');
  });

  it('sends job_id so the server stamps the [Job #…] subject marker', async () => {
    const w = mount(InboxView, { global: { stubs: STUBS } });
    await flushPromises();
    await w.find('[data-test="compose-body"]').setValue('Doors arrive Tuesday.');
    calls.length = 0;
    await w.find('[data-test="compose-send"]').trigger('click');
    await flushPromises();
    const send = calls.find((c) => c.url.endsWith('/api/outlook/send'));
    expect(send.body.job_id).toBe('j-77');
  });

  it('saving that compose as a draft carries the job too', async () => {
    const w = mount(InboxView, { global: { stubs: STUBS } });
    await flushPromises();
    calls.length = 0;
    await w.find('[data-test="compose-save-draft"]').trigger('click');
    await flushPromises();
    const draft = calls.find((c) => c.url.endsWith('/api/outlook/drafts'));
    expect(draft.body.job_id).toBe('j-77');
  });
});

describe('InboxView — attachments on a job compose', () => {
  const PICKED = [{ kind: 'estimate_pdf', id: 'e-1', name: 'Estimate E-1' }, { kind: 'estimate_pdf', id: 'e-2', name: 'Estimate E-2' }];
  const Picker = {
    props: ['modelValue', 'jobId', 'customerId', 'disabled'],
    template: '<button data-test="pick" :data-job="jobId" @click="$emit(\'update:modelValue\', items)">pick</button>',
    data: () => ({ items: PICKED }),
  };
  const mountIt = () => mount(InboxView, { global: { stubs: { ...STUBS, EmailAttachmentPicker: Picker } } });

  it('the picker is scoped to the job, and the send carries every pick', async () => {
    const w = mountIt();
    await flushPromises();
    expect(w.find('[data-test="pick"]').attributes('data-job')).toBe('j-77');
    await w.find('[data-test="pick"]').trigger('click');
    await w.find('[data-test="compose-body"]').setValue('Two options attached.');
    calls.length = 0;
    await w.find('[data-test="compose-send"]').trigger('click');
    await flushPromises();
    const send = calls.find((c) => c.url.endsWith('/api/outlook/send'));
    expect(send.body.attachment_refs).toEqual([{ kind: 'estimate_pdf', id: 'e-1' }, { kind: 'estimate_pdf', id: 'e-2' }]);
  });

  it('a draft is not saved while files are picked — it would drop them silently', async () => {
    const w = mountIt();
    await flushPromises();
    await w.find('[data-test="pick"]').trigger('click');
    calls.length = 0;
    await w.find('[data-test="compose-save-draft"]').trigger('click');
    await flushPromises();
    expect(calls.find((c) => c.url.endsWith('/api/outlook/drafts'))).toBeUndefined();
    expect(w.text()).toContain('Drafts save without attachments');
  });
});


describe('InboxView — a send Outlook did not confirm', () => {
  const UNCONFIRMED = "Outlook didn't confirm the send — check Sent Items before trying again, it may already have gone.";

  it('says so on the status line, with no generic "try again" toast beside it', async () => {
    // Real useApi over a mocked fetch: the 502 goes through fireError.
    const base = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url, init) => {
      if (url.endsWith('/api/outlook/send')) {
        return { ok: false, status: 502, headers: { get: () => 'application/json' },
          json: async () => ({ detail: UNCONFIRMED }), text: async () => JSON.stringify({ detail: UNCONFIRMED }) };
      }
      return base(url, init);
    });
    toastAdd.mockReset();
    const w = mount(InboxView, { global: { stubs: STUBS } });
    await flushPromises();
    await w.find('[data-test="compose-body"]').setValue('Doors arrive Tuesday.');
    await w.find('[data-test="compose-send"]').trigger('click');
    await flushPromises();
    expect(w.text()).toContain(UNCONFIRMED);
    expect(toastAdd).not.toHaveBeenCalled();
  });
});
