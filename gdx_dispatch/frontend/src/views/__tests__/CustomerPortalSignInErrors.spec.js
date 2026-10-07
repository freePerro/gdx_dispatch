/**
 * Customer portal sign-in — every failure says what actually happened.
 *
 * Walking /customer-portal on 2026-10-06, a 429 from the per-IP sign-in limit
 * showed a customer with the RIGHT password "Invalid email or password.", and
 * a rejected link request still said "a sign-in link is on its way".
 *
 * Pinned:
 *  1. Password sign-in: 429 → wait-a-minute, 401 → invalid credentials, any
 *     other failure → a generic retry message (never "invalid").
 *  2. "Email me a sign-in link": only a 2xx shows the sent note; a 429 or any
 *     other failure shows an error instead.
 *  3. Opening a sign-in link: only a 401 calls the link invalid or expired —
 *     not a 429, a server error, or a network failure.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const route = vi.hoisted(() => ({ query: {}, params: {} }));

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({
  useRoute: () => route,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import CustomerPortalView from '../CustomerPortalView.vue';

const field = {
  props: ['modelValue'],
  emits: ['update:modelValue'],
  inheritAttrs: false,
  template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
};

const stubs = {
  Button: {
    props: ['label'],
    emits: ['click'],
    inheritAttrs: false,
    template: '<button :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Message: { inheritAttrs: false, template: '<div :data-testid="$attrs[\'data-testid\']"><slot /></div>' },
  InputText: field,
  Password: field,
  Checkbox: { template: '<input type="checkbox" />' },
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot name="footer" /></div>' },
  Divider: { template: '<hr />' },
  Dialog: { template: '<div />' },
  Tabs: true, TabList: true, Tab: true, TabPanels: true, TabPanel: true,
  DataTable: true, Column: true, Tag: true, Image: true, Textarea: true, Toast: true,
};

function respond(status, body = {}) {
  return Promise.resolve({ ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) });
}

async function mountSignedOut() {
  const wrapper = mount(CustomerPortalView, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

async function signIn(wrapper) {
  await wrapper.find('[data-testid="login-email"]').setValue('customer@example.com');
  await wrapper.find('[data-testid="login-password"]').setValue('right-password');
  await wrapper.find('[data-testid="login-submit"]').trigger('click');
  await flushPromises();
}

async function requestLink(wrapper) {
  await wrapper.find('[data-testid="login-email"]').setValue('customer@example.com');
  await wrapper.find('[data-testid="request-link-btn"]').trigger('click');
  await flushPromises();
}

beforeEach(() => {
  route.query = {};
  sessionStorage.clear();
  localStorage.clear();
});
afterEach(() => { vi.unstubAllGlobals(); });

describe('password sign-in failures', () => {
  it('a 429 says to wait, not that the password is wrong', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respond(429, { detail: 'Rate limit exceeded. Please slow down.' })));
    const wrapper = await mountSignedOut();
    await signIn(wrapper);
    const err = wrapper.find('[data-testid="login-error"]').text();
    expect(err).toContain('Too many sign-in attempts');
    expect(err).not.toContain('Invalid email or password');
  });

  it('a 401 says the credentials are invalid', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respond(401, { detail: 'Invalid email or password' })));
    const wrapper = await mountSignedOut();
    await signIn(wrapper);
    expect(wrapper.find('[data-testid="login-error"]').text()).toBe('Invalid email or password.');
  });

  it('a server error is not reported as a wrong password', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respond(500)));
    const wrapper = await mountSignedOut();
    await signIn(wrapper);
    expect(wrapper.find('[data-testid="login-error"]').text()).toBe('Could not sign in. Please try again.');
  });
});

describe('"Email me a sign-in link"', () => {
  it('shows the sent note only when the request succeeded', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respond(200, { ok: true })));
    const wrapper = await mountSignedOut();
    await requestLink(wrapper);
    expect(wrapper.find('[data-testid="request-link-sent"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="login-error"]').exists()).toBe(false);
  });

  it('a 429 shows wait-a-minute and no sent note', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respond(429)));
    const wrapper = await mountSignedOut();
    await requestLink(wrapper);
    expect(wrapper.find('[data-testid="request-link-sent"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="login-error"]').text()).toContain('Too many sign-in attempts');
  });

  it('any other failure shows an error and no sent note', async () => {
    vi.stubGlobal('fetch', vi.fn(() => respond(500)));
    const wrapper = await mountSignedOut();
    await requestLink(wrapper);
    expect(wrapper.find('[data-testid="request-link-sent"]').exists()).toBe(false);
    expect(wrapper.find('[data-testid="login-error"]').text()).toBe('Could not send a sign-in link. Please try again.');
  });
});

describe('opening a sign-in link', () => {
  it.each([
    [401, 'This sign-in link is invalid or has expired.'],
    [429, 'Too many sign-in attempts. Wait a minute, then open your link again.'],
    [500, 'Could not sign you in. Please open your link again.'],
  ])('a %i says what happened', async (status, message) => {
    route.query = { token: 'abc' };
    vi.stubGlobal('fetch', vi.fn(() => respond(status)));
    const wrapper = await mountSignedOut();
    expect(wrapper.text()).toContain(message);
  });

  it('a network failure does not call the link invalid', async () => {
    route.query = { token: 'abc' };
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))));
    const wrapper = await mountSignedOut();
    expect(wrapper.text()).toContain('Could not sign you in. Please open your link again.');
    expect(wrapper.text()).not.toContain('invalid or has expired');
  });
});
