/**
 * Tests for useApi toast behavior — the contract that 175 callsites
 * across views/ rely on. Prior to 2026-05-09 the `successMessage` option
 * was silently dropped, producing "did it save?" silent-success bugs.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { defineComponent, h, nextTick } from 'vue';
import { mount } from '@vue/test-utils';
import { createPinia } from 'pinia';
import { createMemoryHistory, createRouter } from 'vue-router';
import PrimeVue from 'primevue/config';
import ToastService from 'primevue/toastservice';
import Toast from 'primevue/toast';
import { useApi } from '../useApi';

function mkResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    headers: { get: () => null },
    json: async () => body,
    text: async () => (body === undefined ? '' : JSON.stringify(body)),
  };
}

function makeHarness(testFn) {
  const Captured = { api: null };
  const Harness = defineComponent({
    setup() {
      Captured.api = useApi();
      return () => h(Toast);
    },
  });
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { render: () => null } },
      { path: '/login', component: { render: () => null } },
    ],
  });
  const wrapper = mount(Harness, {
    global: {
      plugins: [createPinia(), router, PrimeVue, ToastService],
    },
  });
  return { wrapper, api: Captured.api, router };
}

describe('useApi toast contract', () => {
  let fetchMock;
  let toastSpy;

  beforeEach(() => {
    fetchMock = vi.fn();
    global.fetch = fetchMock;
    toastSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    // JSDOM persists document.body across tests in a single file; clear toast container
    document.body.innerHTML = '';
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('fires success toast when successMessage option is provided on POST', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({ id: 1 }));
    const { wrapper, api } = makeHarness();

    await api.post('/api/foo', { x: 1 }, { successMessage: 'Foo created' });
    await nextTick();
    await nextTick();

    expect(document.body.innerHTML).toContain('Foo created');
  });

  it('does NOT fire success toast when successMessage is omitted', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({ id: 1 }));
    const { wrapper, api } = makeHarness();

    await api.post('/api/foo', { x: 1 });
    await nextTick();
    await nextTick();

    // Should never have rendered any toast for this success path
    const summaries = ['Foo created', 'Saved', 'Created'];
    for (const s of summaries) expect(document.body.innerHTML).not.toContain(s);
  });

  it('fires error toast on 500 response', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse({ detail: 'boom' }, { ok: false, status: 500 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 })); // client-error report
    const { api } = makeHarness();

    await expect(api.post('/api/foo', {}, { successMessage: 'X' })).rejects.toThrow();
    await nextTick();
    await nextTick();

    expect(document.body.innerHTML).toContain('Something went wrong');
  });

  it('fires permission-denied toast on 403', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse({ detail: 'Missing permission: [billing.edit]' }, { ok: false, status: 403 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));
    const { api } = makeHarness();

    await expect(api.del('/api/invoice/1')).rejects.toThrow();
    await nextTick();
    await nextTick();

    expect(document.body.innerHTML).toContain('Permission denied');
    expect(document.body.innerHTML).toContain('billing.edit');
  });

  it('names the key and points at /role-permissions for the plugin proxy refusal', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse({ detail: 'Missing permission: plugin.acme.write' }, { ok: false, status: 403 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));
    const { api } = makeHarness();

    await expect(api.put('/api/plugins/acme/x', {})).rejects.toThrow();
    await nextTick();
    await nextTick();

    expect(document.body.innerHTML).toContain('plugin.acme.write');
    expect(document.body.innerHTML).toContain('/role-permissions');
  });

  it('gives the generic /role-permissions advice for a bare 403', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse({ detail: 'Forbidden' }, { ok: false, status: 403 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));
    const { api } = makeHarness();

    await expect(api.get('/api/foo')).rejects.toThrow();
    await nextTick();
    await nextTick();

    expect(document.body.innerHTML).toContain("You don't have permission to do that.");
    expect(document.body.innerHTML).toContain('/role-permissions');
  });

  it('a non-core 403 toasts its own message without the /role-permissions advice', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse({ detail: "Module 'acme' is not enabled" }, { ok: false, status: 403 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));
    const { api } = makeHarness();

    await expect(api.get('/api/plugins/acme/items')).rejects.toThrow();
    await nextTick();
    await nextTick();

    expect(document.body.innerHTML).toContain('Permission denied');
    expect(document.body.innerHTML).toContain("Module 'acme' is not enabled");
    expect(document.body.innerHTML).not.toContain('/role-permissions');
  });

  it("a plugin's own bare Forbidden gets no /role-permissions advice", async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse({ detail: 'Forbidden' }, { ok: false, status: 403 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));
    const { api } = makeHarness();

    await expect(api.get('/api/plugins/acme/items')).rejects.toThrow();
    await nextTick();
    await nextTick();

    expect(document.body.innerHTML).toContain("You don't have permission to do that.");
    expect(document.body.innerHTML).not.toContain('/role-permissions');
  });

  it("a plugin's own Missing permission text is not taken for core's", async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse({ detail: 'Missing permission: acme.approve (ask the plugin owner)' }, { ok: false, status: 403 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));
    const { api } = makeHarness();

    await expect(api.post('/api/plugins/acme/approve', {})).rejects.toThrow();
    await nextTick();
    await nextTick();

    expect(document.body.innerHTML).toContain('Missing permission: acme.approve (ask the plugin owner)');
    expect(document.body.innerHTML).not.toContain('/role-permissions');
  });

  it("a require_role refusal shows its reason, not raw JSON", async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse(
        { detail: { error_type: 'insufficient_role', detail: "role 'tech' not in ['admin']", required_roles: ['admin'] } },
        { ok: false, status: 403 },
      ))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));
    const { api } = makeHarness();

    await expect(api.get('/api/admin/db')).rejects.toThrow();
    await nextTick();
    await nextTick();

    expect(document.body.innerHTML).toContain("role 'tech' not in ['admin']");
    expect(document.body.innerHTML).not.toContain('error_type');
    expect(document.body.innerHTML).not.toContain('/role-permissions');
  });

  it('a JSON-array 403 detail does not toast raw JSON', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse({ detail: [{ msg: 'x' }] }, { ok: false, status: 403 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));
    const { api } = makeHarness();

    await expect(api.get('/api/foo')).rejects.toThrow();
    await nextTick();
    await nextTick();

    expect(document.body.innerHTML).toContain("You don't have permission to do that.");
    expect(document.body.innerHTML).not.toContain('&quot;msg&quot;');
    expect(document.body.innerHTML).not.toContain('"msg"');
  });

  it('a non-JSON 403 body (not core) gets no /role-permissions advice', async () => {
    fetchMock
      .mockResolvedValueOnce({
        ok: false,
        status: 403,
        headers: { get: () => null },
        json: async () => { throw new SyntaxError('Unexpected token <'); },
        text: async () => '<html>403</html>',
      })
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));
    const { api } = makeHarness();

    await expect(api.get('/api/foo')).rejects.toThrow();
    await nextTick();
    await nextTick();

    expect(document.body.innerHTML).toContain("You don't have permission to do that.");
    expect(document.body.innerHTML).not.toContain('/role-permissions');
  });

  it('respects suppressErrorToast option', async () => {
    fetchMock
      .mockResolvedValueOnce(mkResponse({ detail: 'boom' }, { ok: false, status: 500 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));
    const { api } = makeHarness();

    await expect(
      api.post('/api/foo', {}, { suppressErrorToast: true })
    ).rejects.toThrow();
    await nextTick();
    await nextTick();

    expect(document.body.innerHTML).not.toContain('Something went wrong');
  });

  it('a suppressErrorToast predicate silences only the errors it accepts', async () => {
    const only403 = (err) => err?.status === 403;
    // Each failure is followed by the transport's client-error report POST,
    // which takes the next queued response.
    fetchMock
      .mockResolvedValueOnce(mkResponse({ detail: 'Admin access required' }, { ok: false, status: 403 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }))
      .mockResolvedValueOnce(mkResponse({ detail: 'boom' }, { ok: false, status: 500 }))
      .mockResolvedValueOnce(mkResponse({}, { ok: true, status: 200 }));
    const { api } = makeHarness();

    await expect(api.get('/api/probe', { suppressErrorToast: only403 })).rejects.toThrow();
    await nextTick();
    await nextTick();
    expect(document.body.innerHTML).not.toContain('Permission denied');

    await expect(api.get('/api/probe', { suppressErrorToast: only403 })).rejects.toThrow();
    await nextTick();
    await nextTick();
    expect(document.body.innerHTML).toContain('Something went wrong');
  });
});

describe('useApi delete alias toast contract (contract-gap Tier 1.1)', () => {
  let fetchMock;

  beforeEach(() => {
    fetchMock = vi.fn();
    global.fetch = fetchMock;
    vi.spyOn(console, 'error').mockImplementation(() => {});
    document.body.innerHTML = '';
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('delete(url, {}, { successMessage }) fires the toast — the PayrollView shape', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({ ok: true }));
    const { wrapper, api } = makeHarness();

    await api.delete('/api/payroll/entries/e1', {}, { successMessage: 'Entry deleted' });
    await nextTick();
    await nextTick();

    expect(document.body.innerHTML).toContain('Entry deleted');
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/payroll/entries/e1');
    expect(init.method).toBe('DELETE');
    wrapper.unmount();
  });

  it('delete(url) plain still deletes and shows no toast', async () => {
    fetchMock.mockResolvedValueOnce(mkResponse({ ok: true }));
    const { wrapper, api } = makeHarness();

    await api.delete('/api/vendors/v1');
    await nextTick();

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/vendors/v1');
    expect(init.method).toBe('DELETE');
    wrapper.unmount();
  });
});
