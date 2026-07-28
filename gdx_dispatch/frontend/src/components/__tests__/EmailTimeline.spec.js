/**
 * EmailTimeline — the customer/job Email tab (P2.1).
 *
 * The endpoints it reads (by-customer / by-job) shipped in Phase 5 with no
 * caller at all; this component is the caller. Locks: the right URL per
 * scope, the empty state (which is the NORMAL state for an untagged
 * customer), and that a 403 from a module-gated tenant reads as "nothing
 * here", not as a red error.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';

import EmailTimeline from '../EmailTimeline.vue';

const STUBS = {
  EmailBodyFrame: { template: '<div class="stub-body" />' },
  EmailAttachments: { template: '<div class="stub-atts" />' },
  RouterLink: { template: '<a><slot /></a>' },
};

function mkResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok, status,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

const ROWS = [{
  id: 'm-1',
  subject: 'Quote for 2 doors',
  from_address: 'alice@example.com',
  to_addresses: ['office@gdx.com'],
  direction: 'inbound',
  received_at: '2026-07-20T12:00:00Z',
  body_preview: 'Please send a quote',
  is_read: true,
  has_attachments: true,
  linked_job_id: 'j-1',
  linked_job_label: 'JOB-2026-014',
}];

let originalFetch;
let calls;

beforeEach(() => {
  setActivePinia(createPinia());
  originalFetch = globalThis.fetch;
  calls = [];
  globalThis.localStorage.setItem('access_token', 'fake.jwt.token');
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  globalThis.localStorage.clear();
});

function fetchWith(body, opts = {}) {
  return vi.fn(async (url) => {
    calls.push(url);
    return mkResponse(body, opts);
  });
}

describe('EmailTimeline', () => {
  it('reads by-job when a jobId is given', async () => {
    globalThis.fetch = fetchWith(ROWS);
    mount(EmailTimeline, { props: { jobId: 'j-1' }, global: { stubs: STUBS } });
    await flushPromises();
    expect(calls[0]).toContain('/api/outlook/messages/by-job/j-1');
  });

  it('reads by-customer when only a customerId is given', async () => {
    globalThis.fetch = fetchWith(ROWS);
    mount(EmailTimeline, { props: { customerId: 'c-1' }, global: { stubs: STUBS } });
    await flushPromises();
    expect(calls[0]).toContain('/api/outlook/messages/by-customer/c-1');
  });

  it('renders a row with sender and subject', async () => {
    globalThis.fetch = fetchWith(ROWS);
    const w = mount(EmailTimeline, { props: { jobId: 'j-1' }, global: { stubs: STUBS } });
    await flushPromises();
    const row = w.find('[data-test="email-timeline-row"]');
    expect(row.text()).toContain('Quote for 2 doors');
    expect(row.text()).toContain('alice@example.com');
  });

  it('expanding a row live-fetches the body', async () => {
    globalThis.fetch = vi.fn(async (url) => {
      calls.push(url);
      if (url.includes('/body')) return mkResponse({ fetched: true, content_type: 'html', body_html: '<p>x</p>' });
      return mkResponse(ROWS);
    });
    const w = mount(EmailTimeline, { props: { jobId: 'j-1' }, global: { stubs: STUBS } });
    await flushPromises();
    await w.find('[data-test="email-timeline-row"]').trigger('click');
    await flushPromises();
    expect(calls.some((u) => u.includes('/api/outlook/messages/m-1/body'))).toBe(true);
  });

  it('shows a plain empty state, not an error, when nothing is linked', async () => {
    globalThis.fetch = fetchWith([]);
    const w = mount(EmailTimeline, { props: { customerId: 'c-9' }, global: { stubs: STUBS } });
    await flushPromises();
    expect(w.find('[data-test="email-timeline-empty"]').exists()).toBe(true);
    expect(w.find('[data-test="email-timeline-error"]').exists()).toBe(false);
  });

  it('treats a module-gated 403 as "no email here", not an error banner', async () => {
    globalThis.fetch = fetchWith({ detail: 'module disabled' }, { ok: false, status: 403 });
    const w = mount(EmailTimeline, { props: { customerId: 'c-9' }, global: { stubs: STUBS } });
    await flushPromises();
    expect(w.find('[data-test="email-timeline-error"]').exists()).toBe(false);
    expect(w.find('[data-test="email-timeline-empty"]').exists()).toBe(true);
  });
});
