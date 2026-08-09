/**
 * "Reply by email" from Leads → composer without a job (2026-08-08).
 *
 * A lead has no job yet, so the old job_id-only gate dead-ended the path.
 * Pins: (1) ?to= alone opens the composer prefilled; (2) the SEND is the
 * contact event — a successful send stamps contacted on the lead
 * (advance-stage) / landing lead (status PATCH), never the click.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

let mockQuery = {};
const routerReplace = vi.fn();
vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/inbox', query: mockQuery }),
  useRouter: () => ({ replace: routerReplace }),
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

async function composeAndSend(w) {
  await w.find('[data-test="compose-body"]').setValue('On our way Tuesday.');
  calls.length = 0;
  await w.find('[data-test="compose-send"]').trigger('click');
  await flushPromises();
}

describe('InboxView — compose from a lead (no job yet)', () => {
  it('opens the composer from ?to= alone, strips the armed URL, and records the contact after a successful send', async () => {
    mockQuery = { to: 'bob@build.test', subject: 'Your service request', lead_id: 'lead-9' };
    const w = mount(InboxView, { global: { stubs: STUBS } });
    await flushPromises();

    expect(w.find('[data-test="inbox-compose"]').exists()).toBe(true);
    expect(w.find('[data-test="compose-to"]').element.value).toBe('bob@build.test');
    // Session-restore of the bare URL must not re-arm the stamp.
    expect(routerReplace).toHaveBeenCalledWith({ path: '/inbox' });

    await composeAndSend(w);
    const send = calls.find((c) => c.url.endsWith('/api/outlook/send'));
    expect(send).toBeTruthy();
    expect(send.body.job_id).toBeUndefined();
    const stamp = calls.find((c) => c.url.endsWith('/api/leads/lead-9/record-contact'));
    expect(stamp).toBeTruthy();
    expect(stamp.method).toBe('POST');
  });

  it('records the contact on a landing lead after send', async () => {
    mockQuery = { to: 'jane@acme.com', subject: 'Your website inquiry', landing_lead_id: 'll-1' };
    const w = mount(InboxView, { global: { stubs: STUBS } });
    await flushPromises();

    await composeAndSend(w);
    const stamp = calls.find((c) => c.url.endsWith('/api/landing-leads/ll-1/record-contact'));
    expect(stamp).toBeTruthy();
    expect(stamp.method).toBe('POST');
  });

  it('does not stamp anything when the compose came without a lead id', async () => {
    mockQuery = { to: 'plain@example.com', subject: 'Hello' };
    const w = mount(InboxView, { global: { stubs: STUBS } });
    await flushPromises();

    await composeAndSend(w);
    expect(calls.some((c) => c.url.includes('/record-contact'))).toBe(false);
  });

  it('does not stamp when the recipient was edited away from the lead', async () => {
    mockQuery = { to: 'bob@build.test', subject: 'Your service request', lead_id: 'lead-9' };
    const w = mount(InboxView, { global: { stubs: STUBS } });
    await flushPromises();

    // The user re-purposed the prefilled composer for someone else — the
    // lead was never emailed, so no contact fact may be recorded.
    await w.find('[data-test="compose-to"]').setValue('someone-else@example.com');
    await composeAndSend(w);
    expect(calls.some((c) => c.url.endsWith('/api/outlook/send'))).toBe(true);
    expect(calls.some((c) => c.url.includes('/record-contact'))).toBe(false);
  });
});
