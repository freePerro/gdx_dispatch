import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import InboxView from '../InboxView.vue';

const STUBS = {
  AppLayout: { template: '<div><slot /></div>' },
  Tree: { template: '<div class="stub-tree"></div>' },
  ContextMenu: { template: '<div></div>', methods: { show() {}, hide() {} } },
  Menu: { template: '<div></div>', methods: { toggle() {}, hide() {} } },
  OverlayPanel: { template: '<div></div>', methods: { toggle() {}, hide() {} } },
  Dialog: {
    template: '<div v-if="visible" class="stub-dialog"><slot /><slot name="footer" /></div>',
    props: ['visible'],
  },
  TreeSelect: { template: '<div></div>' },
  Button: {
    template: '<button :data-test="$attrs[\'data-test\']" @click="$emit(\'click\', $event)"><slot>{{ label }}</slot></button>',
    props: ['label', 'icon', 'disabled', 'severity', 'outlined', 'size'],
    inheritAttrs: false,
  },
  InputText: {
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    props: ['modelValue'],
  },
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


const FAKE_FOLDERS = [
  {
    id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
    graph_folder_id: 'g-inbox',
    display_name: 'Inbox',
    parent_folder_id: null,
    well_known_name: 'inbox',
    total_count: 2,
    unread_count: 1,
    child_folder_count: 0,
    is_hidden: false,
    depth: 0,
    is_system: true,
    color: null,
    pinned: false,
    sort_order: 0,
  },
];

const FAKE_MESSAGES = [
  {
    id: '11111111-1111-1111-1111-111111111111',
    subject: 'First subject',
    from_address: 'alice@example.com',
    to_addresses: ['doug@gdx.com'],
    direction: 'in',
    received_at: '2026-04-28T12:00:00Z',
    body_preview: 'Hi Doug, please call me back.',
    is_read: false,
    has_attachments: false,
  },
  {
    id: '22222222-2222-2222-2222-222222222222',
    subject: 'Second subject',
    from_address: 'bob@example.com',
    to_addresses: ['doug@gdx.com'],
    direction: 'in',
    received_at: '2026-04-27T09:00:00Z',
    body_preview: 'About that estimate…',
    is_read: true,
    has_attachments: true,
  },
];


const FAKE_DETAIL = {
  ...FAKE_MESSAGES[0],
  cc_addresses: ['cc@example.com'],
  bcc_addresses: null,
  conversation_id: 'conv-1',
  internet_message_id: '<orig@msg>',
  body_r2_key: null,
};


function defaultFetch({ messages = FAKE_MESSAGES, folders = FAKE_FOLDERS, sendCapture = null } = {}) {
  return vi.fn(async (url, init) => {
    if (url.endsWith('/api/outlook/folders')) return mkResponse(folders);
    if (url.includes('/api/outlook/messages?') && url.includes('folder_id=')) return mkResponse(messages);
    if (url.includes('/api/outlook/messages?')) return mkResponse(messages);
    if (url.includes(`/api/outlook/messages/${FAKE_MESSAGES[0].id}`)) return mkResponse(FAKE_DETAIL);
    if (url.endsWith('/api/outlook/send')) {
      if (sendCapture) sendCapture.push(JSON.parse(init.body));
      return mkResponse({ ok: true, detail: null });
    }
    throw new Error(`unexpected url: ${url}`);
  });
}


let originalFetch;

beforeEach(() => {
  setActivePinia(createPinia());
  originalFetch = globalThis.fetch;
  globalThis.localStorage.setItem('access_token', 'fake.jwt.token');
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.localStorage.clear();
});


describe('InboxView', () => {
  it('renders folders + messages on mount', async () => {
    globalThis.fetch = defaultFetch();
    const wrapper = mount(InboxView, { global: globalConfig });
    await flushPromises();
    const folderRows = wrapper.findAll('[data-test="folder-row"]');
    expect(folderRows.length).toBeGreaterThanOrEqual(1);
    expect(folderRows[0].text()).toContain('Inbox');
    const msgRows = wrapper.findAll('[data-test="inbox-row"]');
    expect(msgRows).toHaveLength(2);
    expect(msgRows[0].text()).toContain('First subject');
  });

  it('renders empty state when no messages', async () => {
    globalThis.fetch = defaultFetch({ messages: [] });
    const wrapper = mount(InboxView, { global: globalConfig });
    await flushPromises();
    expect(wrapper.text()).toContain('No messages.');
  });

  it('opens detail pane when a row is clicked', async () => {
    globalThis.fetch = defaultFetch();
    const wrapper = mount(InboxView, { global: globalConfig });
    await flushPromises();
    await wrapper.findAll('[data-test="inbox-row"]')[0].trigger('click');
    await flushPromises();
    const detail = wrapper.find('[data-test="inbox-detail"]');
    expect(detail.exists()).toBe(true);
    expect(detail.text()).toContain('First subject');
    expect(detail.text()).toContain('alice@example.com');
  });

  it('reply prefills compose with Re: subject and original sender', async () => {
    globalThis.fetch = defaultFetch();
    const wrapper = mount(InboxView, { global: globalConfig });
    await flushPromises();
    await wrapper.findAll('[data-test="inbox-row"]')[0].trigger('click');
    await flushPromises();
    await wrapper.find('[data-test="inbox-reply"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-test="compose-to"]').element.value).toBe('alice@example.com');
    expect(wrapper.find('[data-test="compose-subject"]').element.value).toBe('Re: First subject');
  });

  it('reply posts to /api/outlook/send with in_reply_to=parent.id', async () => {
    const sendCalls = [];
    globalThis.fetch = defaultFetch({ sendCapture: sendCalls });
    const wrapper = mount(InboxView, { global: globalConfig });
    await flushPromises();
    await wrapper.findAll('[data-test="inbox-row"]')[0].trigger('click');
    await flushPromises();
    await wrapper.find('[data-test="inbox-reply"]').trigger('click');
    await flushPromises();
    await wrapper.find('[data-test="compose-send"]').trigger('click');
    await flushPromises();
    expect(sendCalls).toHaveLength(1);
    expect(sendCalls[0].in_reply_to).toBe(FAKE_MESSAGES[0].id);
  });

  it('new compose has no in_reply_to', async () => {
    const sendCalls = [];
    globalThis.fetch = defaultFetch({ sendCapture: sendCalls });
    const wrapper = mount(InboxView, { global: globalConfig });
    await flushPromises();
    await wrapper.find('[data-test="inbox-new"]').trigger('click');
    await wrapper.find('[data-test="compose-to"]').setValue('new@example.com');
    await wrapper.find('[data-test="compose-subject"]').setValue('Hello');
    await wrapper.find('[data-test="compose-body"]').setValue('Body text');
    await wrapper.find('[data-test="compose-send"]').trigger('click');
    await flushPromises();
    expect(sendCalls).toHaveLength(1);
    expect(sendCalls[0].in_reply_to).toBeUndefined();
  });
});
