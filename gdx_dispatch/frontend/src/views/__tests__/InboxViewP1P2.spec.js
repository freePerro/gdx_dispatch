/**
 * Inbox P1/P2 surfaces — search, link badges, conversation strip, reply-all,
 * forward, drafts, AI draft, create-task.
 *
 * Each test locks the CONTRACT the backend actually enforces (payload shape,
 * URL, method), because that's the class of bug that shipped the original
 * mobile 422: the UI looked right and the request didn't match.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import InboxView from '../InboxView.vue';
import { useAuthStore } from '../../stores/auth';

const STUBS = {
  AppLayout: { template: '<div><slot /></div>' },
  Tree: { template: '<div class="stub-tree"></div>' },
  ContextMenu: { template: '<div></div>', methods: { show() {}, hide() {} } },
  Menu: { template: '<div></div>', methods: { toggle() {}, hide() {} } },
  Popover: { template: '<div></div>', methods: { toggle() {}, hide() {} } },
  Dialog: {
    template: '<div v-if="visible" class="stub-dialog"><slot /><slot name="footer" /></div>',
    props: ['visible'],
  },
  TreeSelect: { template: '<div></div>' },
  Button: {
    template: '<button :data-test="$attrs[\'data-test\']" @click="$emit(\'click\', $event)"><slot>{{ label }}</slot></button>',
    props: ['label', 'icon', 'disabled', 'severity', 'outlined', 'size', 'loading'],
    inheritAttrs: false,
  },
  // inheritAttrs left ON so the parent's data-test AND its @input listener
  // fall through to the real <input> — the component under test wires its
  // search on @input, exactly as PrimeVue's InputText forwards it.
  InputText: {
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    props: ['modelValue'],
  },
  EmailBodyFrame: { template: '<div class="stub-body" />' },
  EmailAttachments: { template: '<div class="stub-atts" />' },
};
const globalConfig = { stubs: STUBS };

if (!globalThis.matchMedia) {
  globalThis.matchMedia = () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} });
}

function mkResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok, status,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

const FOLDERS = [{
  id: 'f1', graph_folder_id: 'g-inbox', display_name: 'Inbox', parent_folder_id: null,
  well_known_name: 'inbox', total_count: 1, unread_count: 0, child_folder_count: 0,
  is_hidden: false, depth: 0, is_system: true, color: null, pinned: false, sort_order: 0,
}];

const MSG_ID = '11111111-1111-1111-1111-111111111111';
const SIBLING_ID = '33333333-3333-3333-3333-333333333333';

const MESSAGES = [{
  id: MSG_ID,
  subject: 'Broken spring',
  from_address: 'alice@example.com',
  to_addresses: ['office@gdx.com', 'sam@gdx.com'],
  cc_addresses: ['boss@gdx.com'],
  direction: 'inbound',
  received_at: '2026-07-20T12:00:00Z',
  body_preview: 'Door wont open',
  is_read: true,
  has_attachments: false,
  linked_customer_id: 'c-1',
  linked_customer_name: 'Acme Doors',
  linked_job_id: 'j-1',
  linked_job_label: 'JOB-2026-014',
  conversation_id: 'conv-9',
}];

// viewer_is_owner drives the owner-only affordances (Forward, the personal
// toggle). Doug — the connected mailbox — is the owner, which is the case
// these tests exercise.
const DETAIL = { ...MESSAGES[0], internet_message_id: '<x@y>', body_r2_key: null, viewer_is_owner: true };

const THREAD = [
  MESSAGES[0],
  {
    id: SIBLING_ID, subject: 'Re: Broken spring', from_address: 'doug@gdx.com',
    to_addresses: ['alice@example.com'], direction: 'outbound',
    received_at: '2026-07-21T08:00:00Z', body_preview: 'On our way',
    is_read: true, has_attachments: false, conversation_id: 'conv-9',
  },
];

function makeFetch(capture = []) {
  return vi.fn(async (url, init = {}) => {
    const method = (init.method || 'GET').toUpperCase();
    capture.push({ url, method, body: init.body ? JSON.parse(init.body) : null });
    if (url.endsWith('/api/outlook/folders')) return mkResponse(FOLDERS);
    if (url.includes('/api/outlook/messages?')) {
      return mkResponse({ items: MESSAGES, has_more: false, next_offset: 1 });
    }
    if (url.includes('/thread')) return mkResponse(THREAD);
    if (url.includes('/body')) return mkResponse({ fetched: true, content_type: 'html', body_html: '<p>hi</p>' });
    if (url.includes('/forward')) return mkResponse({ ok: true, detail: null });
    if (url.includes('/ai-draft')) return mkResponse({ draft_text: 'We can be there Tuesday.', source: 'ai' });
    if (url.includes('/create-task')) return mkResponse({ id: 't-1', title: 'Email: Broken spring' });
    if (url.endsWith('/api/outlook/drafts')) return mkResponse({ ok: true, graph_message_id: 'D1' });
    if (url.endsWith('/api/outlook/send')) return mkResponse({ ok: true, detail: null });
    if (url.includes('/api/customers?')) return mkResponse({ items: [{ id: 'c-2', name: 'Beta Garage', email: 'b@x.com' }] });
    if (url.includes('/api/jobs?')) return mkResponse({ items: [{ id: 'j-2', job_number: 'JOB-2026-020', customer_id: 'c-2' }] });
    if (url.includes(`/api/outlook/messages/${MSG_ID}/link`)) return mkResponse({ ...DETAIL, linked_job_id: 'j-2', linked_job_label: 'JOB-2026-020' });
    if (url.includes(`/api/outlook/messages/${MSG_ID}`)) return mkResponse(DETAIL);
    return mkResponse({});
  });
}

let originalFetch;
let calls;

beforeEach(() => {
  setActivePinia(createPinia());
  // Linking is office-roles-only server-side; the button is gated to match.
  useAuthStore().user = { role: 'admin' };
  originalFetch = globalThis.fetch;
  calls = [];
  globalThis.fetch = makeFetch(calls);
  globalThis.localStorage.setItem('access_token', 'fake.jwt.token');
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
  globalThis.fetch = originalFetch;
  globalThis.localStorage.clear();
});

async function mountAndOpen() {
  const wrapper = mount(InboxView, { global: globalConfig });
  await flushPromises();
  await wrapper.findAll('[data-test="inbox-row"]')[0].trigger('click');
  await flushPromises();
  return wrapper;
}

describe('InboxView — search (1.1)', () => {
  it('sends q= to the server rather than filtering the loaded page', async () => {
    const wrapper = mount(InboxView, { global: globalConfig });
    await flushPromises();
    calls.length = 0;
    await wrapper.find('[data-test="inbox-search"]').setValue('spring');
    await wrapper.find('[data-test="inbox-search"]').trigger('input');
    vi.advanceTimersByTime(400);
    await flushPromises();
    const search = calls.find((c) => c.url.includes('q=spring'));
    expect(search).toBeTruthy();
  });

  it('clearing the search re-queries without q', async () => {
    const wrapper = mount(InboxView, { global: globalConfig });
    await flushPromises();
    await wrapper.find('[data-test="inbox-search"]').setValue('spring');
    await wrapper.find('[data-test="inbox-search"]').trigger('input');
    vi.advanceTimersByTime(400);
    await flushPromises();
    calls.length = 0;
    await wrapper.find('[data-test="inbox-search-clear"]').trigger('click');
    await flushPromises();
    const listCall = calls.find((c) => c.url.includes('/api/outlook/messages?'));
    expect(listCall.url).not.toContain('q=');
  });
});

describe('InboxView — link badges (P2.1)', () => {
  it('shows the customer name and job label on a list row, not raw ids', async () => {
    const wrapper = mount(InboxView, { global: globalConfig });
    await flushPromises();
    const links = wrapper.find('[data-test="inbox-row-links"]');
    expect(links.exists()).toBe(true);
    expect(links.text()).toContain('Acme Doors');
    expect(links.text()).toContain('JOB-2026-014');
    expect(links.text()).not.toContain('c-1');
  });
});

describe('InboxView — conversation (1.3)', () => {
  it('renders the other messages in the thread from the server', async () => {
    const wrapper = await mountAndOpen();
    const strip = wrapper.find('[data-test="inbox-thread"]');
    expect(strip.exists()).toBe(true);
    expect(strip.text()).toContain('Re: Broken spring');
    // The open message itself is not repeated in the strip.
    expect(wrapper.findAll('[data-test="inbox-thread-row"]')).toHaveLength(1);
  });
});

describe('InboxView — reply-all + forward (1.4)', () => {
  it('reply-all puts the other recipients in cc and the sender in to', async () => {
    const wrapper = await mountAndOpen();
    await wrapper.find('[data-test="inbox-reply-all"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-test="compose-to"]').element.value).toBe('alice@example.com');
    const cc = wrapper.find('[data-test="compose-cc"]').element.value;
    expect(cc).toContain('office@gdx.com');
    expect(cc).toContain('boss@gdx.com');
    // The original sender must not be duplicated into cc.
    expect(cc).not.toContain('alice@example.com');
  });

  it('forward posts to the native Graph forward endpoint with a recipient list', async () => {
    const wrapper = await mountAndOpen();
    await wrapper.find('[data-test="inbox-forward"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-test="forward-to"]').setValue('sub@vendor.com');
    calls.length = 0;
    await wrapper.find('[data-test="forward-send"]').trigger('click');
    await flushPromises();
    const fwd = calls.find((c) => c.url.includes('/forward'));
    expect(fwd.method).toBe('POST');
    expect(fwd.body.to).toEqual(['sub@vendor.com']);
  });
});

describe('InboxView — drafts + AI (1.5 / P2.4)', () => {
  it('save draft posts the compose contents to /api/outlook/drafts', async () => {
    const wrapper = mount(InboxView, { global: globalConfig });
    await flushPromises();
    await wrapper.find('[data-test="inbox-new"]').trigger('click');
    await wrapper.find('[data-test="compose-to"]').setValue('a@b.com');
    await wrapper.find('[data-test="compose-subject"]').setValue('Half written');
    await wrapper.find('[data-test="compose-body"]').setValue('to be continued');
    calls.length = 0;
    await wrapper.find('[data-test="compose-save-draft"]').trigger('click');
    await flushPromises();
    const draft = calls.find((c) => c.url.endsWith('/api/outlook/drafts'));
    expect(draft.method).toBe('POST');
    expect(draft.body.subject).toBe('Half written');
    expect(draft.body.to).toEqual(['a@b.com']);
    // body_html, not body — SendMailIn/DraftIn are extra=forbid.
    expect(draft.body.body_html).toContain('to be continued');
    expect(draft.body.body).toBeUndefined();
  });

  it('AI draft inserts the suggestion above the quoted original', async () => {
    const wrapper = await mountAndOpen();
    await wrapper.find('[data-test="inbox-reply"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-test="compose-ai-draft"]').trigger('click');
    await flushPromises();
    const body = wrapper.find('[data-test="compose-body"]').element.value;
    expect(body.startsWith('We can be there Tuesday.')).toBe(true);
    expect(body).toContain('alice@example.com wrote:');
  });
});

describe('InboxView — email → task (P2.2)', () => {
  it('posts create-task for the open message', async () => {
    const wrapper = await mountAndOpen();
    calls.length = 0;
    await wrapper.find('[data-test="inbox-create-task"]').trigger('click');
    await flushPromises();
    const task = calls.find((c) => c.url.includes('/create-task'));
    expect(task.method).toBe('POST');
    expect(task.url).toContain(MSG_ID);
  });
});

describe('InboxView — manual link (P2.1/P2.2)', () => {
  it('picking a job posts the link and updates the row badge', async () => {
    const wrapper = await mountAndOpen();
    await wrapper.find('[data-test="inbox-link-open"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-test="link-job-search"]').setValue('JOB-2026-020');
    await wrapper.find('[data-test="link-job-search"]').trigger('input');
    vi.advanceTimersByTime(400);
    await flushPromises();
    await wrapper.find('[data-test="link-job-results"] button').trigger('click');
    calls.length = 0;
    await wrapper.find('[data-test="link-save"]').trigger('click');
    await flushPromises();
    const link = calls.find((c) => c.url.includes('/link'));
    expect(link.method).toBe('POST');
    expect(link.body.job_id).toBe('j-2');
    expect(wrapper.find('[data-test="inbox-row-links"]').text()).toContain('JOB-2026-020');
  });

  it('clearing both ids sends DELETE (records a durable "no link")', async () => {
    const wrapper = await mountAndOpen();
    await wrapper.find('[data-test="inbox-link-open"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-test="link-clear"]').trigger('click');
    calls.length = 0;
    await wrapper.find('[data-test="link-save"]').trigger('click');
    await flushPromises();
    const link = calls.find((c) => c.url.includes('/link'));
    expect(link.method).toBe('DELETE');
  });
});

describe('InboxView — affordances match what the server allows', () => {
  it('hides Forward from a non-owner instead of 403ing them after they type', async () => {
    // Graph's forward resolves the id against the CALLER's mailbox, so the
    // server 403s a non-owner with a permission no role grant can fix.
    globalThis.fetch = vi.fn(async (url, init = {}) => {
      calls.push({ url, method: (init.method || 'GET').toUpperCase(), body: init.body ? JSON.parse(init.body) : null });
      if (url.endsWith('/api/outlook/folders')) return mkResponse(FOLDERS);
      if (url.includes('/api/outlook/messages?')) return mkResponse({ items: MESSAGES, has_more: false, next_offset: 1 });
      if (url.includes('/thread')) return mkResponse(THREAD);
      if (url.includes('/body')) return mkResponse({ fetched: true });
      if (url.includes(`/api/outlook/messages/${MSG_ID}`)) {
        return mkResponse({ ...DETAIL, viewer_is_owner: false });
      }
      return mkResponse({});
    });
    const wrapper = await mountAndOpen();
    expect(wrapper.find('[data-test="inbox-forward"]').exists()).toBe(false);
    // Reply is not owner-gated — a shared-inbox user can still answer.
    expect(wrapper.find('[data-test="inbox-reply"]').exists()).toBe(true);
  });

  it('hides Link… from a technician (POST /link is office-roles-only)', async () => {
    useAuthStore().user = { role: 'technician' };
    const wrapper = await mountAndOpen();
    expect(wrapper.find('[data-test="inbox-link-open"]').exists()).toBe(false);
  });
});

describe('InboxView — draft honesty (1.5)', () => {
  it('says where the draft actually is, not just "saved"', async () => {
    const wrapper = mount(InboxView, { global: globalConfig });
    await flushPromises();
    await wrapper.find('[data-test="inbox-new"]').trigger('click');
    await wrapper.find('[data-test="compose-subject"]').setValue('Half written');
    await wrapper.find('[data-test="compose-save-draft"]').trigger('click');
    await flushPromises();
    const status = wrapper.find('.compose-status').text();
    expect(status).toContain('Outlook Drafts');
    expect(status).toContain('after the next sync');
  });

  it('does not create a second Graph draft when nothing changed', async () => {
    const wrapper = mount(InboxView, { global: globalConfig });
    await flushPromises();
    await wrapper.find('[data-test="inbox-new"]').trigger('click');
    await wrapper.find('[data-test="compose-subject"]').setValue('Half written');
    await wrapper.find('[data-test="compose-save-draft"]').trigger('click');
    await flushPromises();
    calls.length = 0;
    await wrapper.find('[data-test="compose-save-draft"]').trigger('click');
    await flushPromises();
    expect(calls.filter((c) => c.url.endsWith('/api/outlook/drafts'))).toHaveLength(0);
  });
});

describe('InboxView — search scope (1.1)', () => {
  it('searches across ALL folders, not just the selected one', async () => {
    const wrapper = mount(InboxView, { global: globalConfig });
    await flushPromises();  // mounts with the Inbox folder selected
    calls.length = 0;
    await wrapper.find('[data-test="inbox-search"]').setValue('archive thing');
    await wrapper.find('[data-test="inbox-search"]').trigger('input');
    vi.advanceTimersByTime(400);
    await flushPromises();
    const search = calls.find((c) => c.url.includes('q='));
    expect(search.url).not.toContain('folder_id=');
  });
});
