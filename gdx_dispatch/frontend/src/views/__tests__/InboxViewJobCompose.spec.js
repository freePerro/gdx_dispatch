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
  useRoute: () => ({ query: { job_id: 'j-77', job_label: 'JOB-2026-014', to: 'alice@example.com', subject: 'JOB-2026-014 — update' } }),
}));

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
