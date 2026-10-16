/**
 * MobileInboxView send payloads (D2).
 *
 * Mobile compose & reply used to POST { to: "<string>", body, ... } which
 * SendMailIn (extra="forbid", to: list, body_html) rejected with 422 every
 * time. These tests pin the outgoing payload to the SendMailIn shape AND the
 * value edge cases that still 422'd or corrupted the body (separator-only
 * recipient, unescaped HTML metacharacters).
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: apiGet, post: apiPost, patch: apiPatch, put: vi.fn(), del: vi.fn() }),
}));
const toastAdd = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
// The lead-intake button's permission check; these specs don't mount Pinia.
vi.mock('../../composables/usePermission', () => ({ usePermission: () => ({ hasPermission: () => false }) }));

import MobileInboxView from '../MobileInboxView.vue';

const stubs = {
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :data-test="$attrs[\'data-test\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea :data-test="$attrs[\'data-test\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  Dialog: { props: ['visible'], template: '<div v-if="visible"><slot /><slot name="footer" /></div>' },
  Button: {
    props: ['label', 'icon', 'loading', 'disabled', 'severity', 'text', 'size'],
    emits: ['click'],
    template: '<button :data-test="$attrs[\'data-test\']" :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  EmailBodyFrame: { template: '<div />' },
};

function mountView() {
  return mount(MobileInboxView, { global: { stubs, directives: { tooltip: {} } } });
}

async function typeInto(wrapper, testId, value) {
  const el = wrapper.find(`[data-test="${testId}"]`);
  await el.setValue(value);
}

describe('MobileInboxView compose payload (D2)', () => {
  beforeEach(() => {
    apiGet.mockReset().mockResolvedValue([]);
    apiPost.mockReset().mockResolvedValue({ ok: true });
    apiPatch.mockReset().mockResolvedValue({});
  });

  it('POSTs to /send with array to, body_html, no stray fields', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mi-compose"]').trigger('click'); // startCompose
    await typeInto(w, 'mi-compose-to', 'a@x.com, b@y.com');
    await typeInto(w, 'mi-compose-cc', 'c@z.com');
    await typeInto(w, 'mi-compose-subject', 'Hello');
    await typeInto(w, 'mi-compose-body', 'line one\nline two');
    await w.find('[data-test="mi-compose-send"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledTimes(1);
    const [url, payload] = apiPost.mock.calls[0];
    expect(url).toBe('/api/outlook/send');
    expect(payload.to).toEqual(['a@x.com', 'b@y.com']); // array, split
    expect(payload.cc).toEqual(['c@z.com']);
    expect(payload.subject).toBe('Hello');
    expect(payload.body_html).toBe('line one<br>line two'); // body_html, not body
    expect(payload).not.toHaveProperty('body'); // stray field gone
    expect(Object.keys(payload).sort()).toEqual(['body_html', 'cc', 'subject', 'to']);
  });

  it('omits cc entirely when the cc field is empty', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mi-compose"]').trigger('click');
    await typeInto(w, 'mi-compose-to', 'a@x.com');
    await typeInto(w, 'mi-compose-subject', 'Hi');
    await typeInto(w, 'mi-compose-body', 'body');
    await w.find('[data-test="mi-compose-send"]').trigger('click');
    await flushPromises();

    const payload = apiPost.mock.calls[0][1];
    expect(payload).not.toHaveProperty('cc');
  });

  it('does not send when required fields are missing (guard holds)', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mi-compose"]').trigger('click');
    await typeInto(w, 'mi-compose-to', 'a@x.com'); // no subject/body
    await w.find('[data-test="mi-compose-send"]').trigger('click');
    await flushPromises();
    expect(apiPost).not.toHaveBeenCalled();
  });

  it('escapes HTML metacharacters in the body (no mangled/injected markup)', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mi-compose"]').trigger('click');
    await typeInto(w, 'mi-compose-to', 'a@x.com');
    await typeInto(w, 'mi-compose-subject', 'Quote');
    await typeInto(w, 'mi-compose-body', 'cost < $500 & > 0\nnext');
    await w.find('[data-test="mi-compose-send"]').trigger('click');
    await flushPromises();
    const payload = apiPost.mock.calls[0][1];
    expect(payload.body_html).toBe('cost &lt; $500 &amp; &gt; 0<br>next');
  });

  it('does NOT send a separator-only recipient (would 422 on to: min_length=1)', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mi-compose"]').trigger('click');
    await typeInto(w, 'mi-compose-to', ' ; , ');
    await typeInto(w, 'mi-compose-subject', 'Hi');
    await typeInto(w, 'mi-compose-body', 'body');
    await w.find('[data-test="mi-compose-send"]').trigger('click');
    await flushPromises();
    expect(apiPost).not.toHaveBeenCalled();
  });
});

describe('MobileInboxView reply payload (D2)', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset().mockResolvedValue({ ok: true });
    apiPatch.mockReset().mockResolvedValue({});
  });

  it('reply POSTs array to (from sender), body_html, in_reply_to', async () => {
    // URL-routed, not positional: the mount-time sync-health fetch made
    // mockResolvedValueOnce chains consume the wrong responses.
    apiGet.mockImplementation((url) => {
      if (url.includes('/sync-health')) return Promise.resolve({ status: 'healthy', problems: [] });
      if (url.includes('/messages/m1/body')) return Promise.resolve({ fetched: false, reason: 'graph_error' });
      if (url.includes('/messages/m1')) {
        return Promise.resolve({ id: 'm1', subject: 'Quote', from_address: 'cust@x.com', is_read: true, body_preview: 'hi' });
      }
      return Promise.resolve([{ id: 'm1', subject: 'Quote', from_address: 'cust@x.com', is_read: true }]);
    });
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mi-msg-row"]').trigger('click'); // openMessage
    await flushPromises();
    await w.find('[data-test="mi-reply-open"]').trigger('click'); // startReply
    await typeInto(w, 'mi-reply-body', 'my reply');
    await w.find('[data-test="mi-reply-send"]').trigger('click');
    await flushPromises();

    const sendCall = apiPost.mock.calls.find((c) => c[0] === '/api/outlook/send');
    expect(sendCall).toBeTruthy();
    const payload = sendCall[1];
    expect(payload.to).toEqual(['cust@x.com']); // array from sender
    expect(payload.subject).toBe('Re: Quote');
    expect(payload.body_html).toContain('my reply');
    expect(payload).not.toHaveProperty('body');
    expect(payload.in_reply_to).toBe('m1');
  });
});


describe('MobileInboxView pagination / load-more (D7)', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset().mockResolvedValue({ ok: true });
    apiPatch.mockReset().mockResolvedValue({});
  });

  it('appends the next page and hides Load more when has_more is false', async () => {
    // URL-routed (see reply test): message pages by offset, sync-health inert.
    apiGet.mockImplementation((url) => {
      if (url.includes('/sync-health')) return Promise.resolve({ status: 'healthy', problems: [] });
      if (url.includes('offset=50')) {
        return Promise.resolve({ items: [{ id: 'b', subject: 'B', is_read: true }], has_more: false, next_offset: 100 });
      }
      return Promise.resolve({ items: [{ id: 'a', subject: 'A', is_read: true }], has_more: true, next_offset: 50 });
    });
    const w = mountView();
    await flushPromises();
    expect(w.findAll('[data-test="mi-msg-row"]')).toHaveLength(1);
    const more = w.find('[data-test="mi-load-more"]');
    expect(more.exists()).toBe(true);

    await more.trigger('click');
    await flushPromises();

    const msgCalls = apiGet.mock.calls.filter((c) => c[0].includes('/api/outlook/messages'));
    expect(msgCalls[1][0]).toContain('offset=50');
    expect(w.findAll('[data-test="mi-msg-row"]')).toHaveLength(2);
    expect(w.find('[data-test="mi-load-more"]').exists()).toBe(false);
  });

  it('does not show Load more when the first page is the last', async () => {
    apiGet.mockResolvedValue({ items: [{ id: 'a', subject: 'A', is_read: true }], has_more: false, next_offset: 50 });
    const w = mountView();
    await flushPromises();
    expect(w.find('[data-test="mi-load-more"]').exists()).toBe(false);
  });
});


describe('MobileInboxView sync-health banner', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset().mockResolvedValue({ ok: true });
    apiPatch.mockReset().mockResolvedValue({});
  });

  it('shows the banner when sync is broken', async () => {
    apiGet.mockImplementation((url) => {
      if (url.includes('/sync-health')) {
        return Promise.resolve({ status: 'unhealthy', problems: ['Inbox frozen'], newest_sync_at: null });
      }
      return Promise.resolve({ items: [], has_more: false, next_offset: 0 });
    });
    const w = mountView();
    await flushPromises();
    const banner = w.find('[data-test="mi-sync-health-banner"]');
    expect(banner.exists()).toBe(true);
    expect(banner.text()).toContain('Email is not syncing');
  });

  it('stays hidden when healthy — and when the health call itself fails', async () => {
    apiGet.mockImplementation((url) => {
      if (url.includes('/sync-health')) return Promise.reject(new Error('boom'));
      return Promise.resolve({ items: [], has_more: false, next_offset: 0 });
    });
    const w = mountView();
    await flushPromises();
    expect(w.find('[data-test="mi-sync-health-banner"]').exists()).toBe(false);
  });
});

/**
 * Mobile P1/P2 parity — search, link badges, conversation strip, forward,
 * follow-up task, AI reply. Mobile is where these were missing entirely
 * before; the desktop-only shape is exactly what shipped the D2 class of bug.
 */
describe('MobileInboxView P1/P2 surfaces', () => {
  const MSG = {
    id: 'm-1',
    subject: 'Broken spring',
    from_address: 'alice@example.com',
    to_addresses: ['office@gdx.com'],
    direction: 'inbound',
    received_at: '2026-07-20T12:00:00Z',
    body_preview: 'door wont open',
    is_read: true,
    has_attachments: false,
    linked_customer_id: 'c-1',
    linked_customer_name: 'Acme Doors',
    linked_job_id: 'j-1',
    linked_job_label: 'JOB-2026-014',
    conversation_id: 'conv-9',
    // Forward is owner-only (Graph resolves the id against the caller's own
    // mailbox), so the button only renders for the mailbox owner.
    viewer_is_owner: true,
  };
  const SIBLING = { ...MSG, id: 'm-2', subject: 'Re: Broken spring', from_address: 'doug@gdx.com' };

  beforeEach(() => {
    apiGet.mockReset().mockImplementation(async (url) => {
      if (url.includes('/thread')) return [MSG, SIBLING];
      if (url.includes('/body')) return { fetched: true, content_type: 'text', body_html: 'door wont open' };
      if (url.includes('/api/outlook/messages?')) return { items: [MSG], has_more: false, next_offset: 1 };
      return MSG;
    });
    apiPost.mockReset().mockResolvedValue({ ok: true, draft_text: 'We can be there Tuesday.' });
    apiPatch.mockReset().mockResolvedValue({});
  });

  it('search sends q= to the server', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const w = mountView();
    await flushPromises();
    apiGet.mockClear();
    const box = w.find('[data-test="mi-search"]');
    await box.setValue('spring');
    await box.trigger('input');
    vi.advanceTimersByTime(400);
    await flushPromises();
    expect(apiGet.mock.calls.some(([u]) => u.includes('q=spring'))).toBe(true);
    vi.useRealTimers();
  });

  it('shows customer/job labels on a row', async () => {
    const w = mountView();
    await flushPromises();
    const links = w.find('[data-test="mi-row-links"]');
    expect(links.exists()).toBe(true);
    expect(links.text()).toContain('Acme Doors');
    expect(links.text()).toContain('JOB-2026-014');
  });

  it('opening a message loads the conversation strip', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mi-msg-row"]').trigger('click');
    await flushPromises();
    expect(apiGet.mock.calls.some(([u]) => u.includes('/thread'))).toBe(true);
    expect(w.findAll('[data-test="mi-thread-row"]').length).toBe(1);
  });

  it('forward posts to the Graph forward endpoint with an array recipient', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mi-msg-row"]').trigger('click');
    await flushPromises();
    await w.find('[data-test="mi-forward-open"]').trigger('click');
    await typeInto(w, 'mi-forward-to', 'sub@vendor.com');
    apiPost.mockClear();
    await w.find('[data-test="mi-forward-send"]').trigger('click');
    await flushPromises();
    const [url, body] = apiPost.mock.calls[0];
    expect(url).toContain('/forward');
    expect(body.to).toEqual(['sub@vendor.com']);
  });

  it('a forward that loses the connection says it may have gone, with no generic toast', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mi-msg-row"]').trigger('click');
    await flushPromises();
    await w.find('[data-test="mi-forward-open"]').trigger('click');
    await typeInto(w, 'mi-forward-to', 'sub@vendor.com');
    apiPost.mockClear().mockRejectedValueOnce(new TypeError('Failed to fetch'));
    toastAdd.mockClear();
    await w.find('[data-test="mi-forward-send"]').trigger('click');
    await flushPromises();
    expect(apiPost.mock.calls[0][2].suppressErrorToast({ status: 502 })).toBe(true);
    expect(toastAdd).toHaveBeenCalledTimes(1);
    expect(toastAdd.mock.calls[0][0].detail).toContain('Check Sent Items');
  });

  it('create task posts to create-task for the open message', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mi-msg-row"]').trigger('click');
    await flushPromises();
    apiPost.mockClear();
    await w.find('[data-test="mi-create-task"]').trigger('click');
    await flushPromises();
    expect(apiPost.mock.calls[0][0]).toContain('/create-task');
  });

  it('AI draft fills the reply box above the quoted original', async () => {
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mi-msg-row"]').trigger('click');
    await flushPromises();
    await w.find('[data-test="mi-reply-open"]').trigger('click');
    await flushPromises();
    await w.find('[data-test="mi-ai-draft"]').trigger('click');
    await flushPromises();
    const body = w.find('[data-test="mi-reply-body"]').element.value;
    expect(body.startsWith('We can be there Tuesday.')).toBe(true);
    expect(body).toContain('alice@example.com wrote:');
  });
});

describe('MobileInboxView attachments', () => {
  // The picker is its own spec; here it is a stub that reports the context it
  // was given and hands back one estimate and one device file when tapped.
  const PICKED = [
    { kind: 'estimate_pdf', id: 'e-1', name: 'Estimate E-1' },
    { kind: 'upload', name: 'door.jpg', content_type: 'image/jpeg', content_base64: 'QQ==', size_bytes: 1 },
  ];
  const Picker = {
    props: ['modelValue', 'jobId', 'customerId', 'disabled'],
    emits: ['update:modelValue'],
    template: '<button data-test="pick" :data-job="jobId" :data-customer="customerId" @click="$emit(\'update:modelValue\', items)">pick</button>',
    data: () => ({ items: PICKED }),
  };
  const MSG = {
    id: 'm-1', subject: 'Quote', from_address: 'alice@example.com', received_at: '2026-10-01T00:00:00Z',
    body_preview: 'hi', is_read: true, linked_customer_id: 'c-1', linked_job_id: 'j-1', viewer_is_owner: true,
  };
  const REFS = [
    { kind: 'estimate_pdf', id: 'e-1' },
    { kind: 'upload', name: 'door.jpg', content_type: 'image/jpeg', content_base64: 'QQ==' },
  ];

  function mountWithPicker() {
    return mount(MobileInboxView, { global: { stubs: { ...stubs, EmailAttachmentPicker: Picker }, directives: { tooltip: {} } } });
  }

  async function openMessage(w) {
    await flushPromises();
    await w.find('[data-test="mi-msg-row"]').trigger('click');
    await flushPromises();
  }

  beforeEach(() => {
    apiGet.mockReset().mockImplementation(async (url) => {
      if (url.includes('/thread')) return [MSG];
      if (url.includes('/body')) return { fetched: true, content_type: 'text', body_html: 'hi' };
      if (url.includes('/api/outlook/messages?')) return { items: [MSG], has_more: false, next_offset: 1 };
      return MSG;
    });
    apiPost.mockReset().mockResolvedValue({ ok: true });
  });

  it('a new message sends its picks as attachment_refs', async () => {
    const w = mountWithPicker();
    await flushPromises();
    await w.find('[data-test="mi-compose"]').trigger('click');
    await typeInto(w, 'mi-compose-to', 'a@x.com');
    await typeInto(w, 'mi-compose-subject', 'Options');
    await typeInto(w, 'mi-compose-body', 'Both quotes attached');
    await w.find('[data-test="pick"]').trigger('click');
    await w.find('[data-test="mi-compose-send"]').trigger('click');
    await flushPromises();
    const [url, payload] = apiPost.mock.calls[0];
    expect(url).toBe('/api/outlook/send');
    expect(payload.attachment_refs).toEqual(REFS);
  });

  it('a reply scopes the picker to the linked job and customer and sends the picks', async () => {
    const w = mountWithPicker();
    await openMessage(w);
    await w.find('[data-test="mi-reply-open"]').trigger('click');
    const pick = w.find('[data-test="pick"]');
    expect(pick.attributes('data-job')).toBe('j-1');
    expect(pick.attributes('data-customer')).toBe('c-1');
    await pick.trigger('click');
    await w.find('[data-test="mi-reply-send"]').trigger('click');
    await flushPromises();
    const [, payload] = apiPost.mock.calls[0];
    expect(payload.in_reply_to).toBe('m-1');
    expect(payload.attachment_refs).toEqual(REFS);
  });

  it('a forward carries added files alongside the originals', async () => {
    const w = mountWithPicker();
    await openMessage(w);
    await w.find('[data-test="mi-forward-open"]').trigger('click');
    await typeInto(w, 'mi-forward-to', 'sub@vendor.com');
    await w.find('[data-test="pick"]').trigger('click');
    expect(w.text()).toContain('plus the files added here');
    await w.find('[data-test="mi-forward-send"]').trigger('click');
    await flushPromises();
    const [url, payload] = apiPost.mock.calls[0];
    expect(url).toBe('/api/outlook/messages/m-1/forward');
    expect(payload.attachment_refs).toEqual(REFS);
  });

  it('send is blocked while the picks are over the email limit', async () => {
    const w = mountWithPicker();
    await flushPromises();
    await w.find('[data-test="mi-compose"]').trigger('click');
    await typeInto(w, 'mi-compose-to', 'a@x.com');
    await typeInto(w, 'mi-compose-subject', 'Big');
    await typeInto(w, 'mi-compose-body', 'x');
    w.findComponent(Picker).vm.$emit('update:modelValue', [{ kind: 'upload', name: 'big', size_bytes: 19_000_000 }]);
    await flushPromises();
    expect(w.find('[data-test="mi-compose-send"]').attributes('disabled')).toBeDefined();
  });
});


describe('MobileInboxView send that Outlook did not confirm', () => {
  const UNCONFIRMED = "Outlook didn't confirm the send — check Sent Items before trying again, it may already have gone.";

  beforeEach(() => {
    apiGet.mockReset().mockResolvedValue([]);
    apiPost.mockReset().mockRejectedValue(Object.assign(new Error(UNCONFIRMED), { status: 502 }));
    toastAdd.mockReset();
  });

  it('shows the server reason and no generic "try again" toast', async () => {
    // A generic "Please try again" beside "it may already have gone" invites
    // the duplicate send; the call opts out of it and shows the reason itself.
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mi-compose"]').trigger('click');
    await typeInto(w, 'mi-compose-to', 'a@x.com');
    await typeInto(w, 'mi-compose-subject', 'Hi');
    await typeInto(w, 'mi-compose-body', 'body');
    await w.find('[data-test="mi-compose-send"]').trigger('click');
    await flushPromises();

    const { suppressErrorToast } = apiPost.mock.calls[0][2];
    expect(suppressErrorToast({ status: 502 })).toBe(true);
    expect(suppressErrorToast({ status: 401 })).toBe(false); // expired session still reaches login
    expect(toastAdd).toHaveBeenCalledTimes(1);
    const shown = toastAdd.mock.calls[0][0];
    expect(shown.detail).toBe(UNCONFIRMED);
    expect(shown.summary).not.toMatch(/failed/i);
  });

  it('a reply opts out of the generic toast too', async () => {
    apiGet.mockImplementation((url) => {
      if (url.includes('/sync-health')) return Promise.resolve({ status: 'healthy', problems: [] });
      if (url.includes('/messages/m1/body')) return Promise.resolve({ fetched: false, reason: 'graph_error' });
      if (url.includes('/messages/m1')) {
        return Promise.resolve({ id: 'm1', subject: 'Quote', from_address: 'cust@x.com', is_read: true, body_preview: 'hi' });
      }
      return Promise.resolve([{ id: 'm1', subject: 'Quote', from_address: 'cust@x.com', is_read: true }]);
    });
    const w = mountView();
    await flushPromises();
    await w.find('[data-test="mi-msg-row"]').trigger('click');
    await flushPromises();
    await w.find('[data-test="mi-reply-open"]').trigger('click');
    await typeInto(w, 'mi-reply-body', 'my reply');
    await w.find('[data-test="mi-reply-send"]').trigger('click');
    await flushPromises();

    const sendCall = apiPost.mock.calls.find((c) => c[0] === '/api/outlook/send');
    expect(sendCall[2].suppressErrorToast({ status: 504 })).toBe(true);
    expect(toastAdd.mock.calls.at(-1)[0].detail).toBe(UNCONFIRMED);
  });
});
