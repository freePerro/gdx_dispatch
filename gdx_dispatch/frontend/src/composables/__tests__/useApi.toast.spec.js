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
});
