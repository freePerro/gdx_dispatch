/**
 * #657 — a consumed chat push link stays consumed when the user presses Back.
 *
 * Uses the REAL vue-router with createWebHistory: the defect this pins is in
 * how the router and the browser history interact, which a mocked router
 * cannot see. The first draft of the fix stripped the query with
 * history.replaceState; vue-router keeps its own copy of the URL in
 * history.state and wrote the query back onto the entry on the next push, so
 * Back re-mounted the view with ?job= still set — the chat reopened and
 * mark-read fired again. Stripping through router.replace does not.
 */
import 'fake-indexeddb/auto';
import { describe, it, expect, vi } from 'vitest';
import { mount, flushPromises, RouterLinkStub } from '@vue/test-utils';
import { defineComponent, h, ref } from 'vue';
import { createRouter, createWebHistory, RouterView } from 'vue-router';

const getMock = vi.fn();
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useApi', () => ({
  useApi: () => ({ get: getMock, post: vi.fn(), patch: vi.fn(), postQueued: vi.fn() }),
}));
vi.mock('../../composables/useTenantTimezone', () => ({
  useTenantTimezone: () => ({ zonedDateKey: () => '2026-09-13' }),
}));
vi.mock('../../composables/usePartsSeenCutoff', () => ({
  markJobSeen: vi.fn(),
  countUnseenForJob: () => 0,
}));
vi.mock('../../composables/usePhotoQueue', () => ({
  usePhotoQueue: () => ({
    pendingPhotos: ref(0),
    uploadingPhotos: ref(false),
    capturePhoto: vi.fn(),
    drainPhotos: vi.fn(),
  }),
}));

const stubs = {
  // The customer name on these screens is a router-link to /mobile/customers/:id.
  // These specs mock vue-router wholesale, so RouterLink is not registered.
  RouterLink: RouterLinkStub,
  MobileChatDialog: {
    props: { visible: Boolean, job: { type: Object, default: null }, markRead: Boolean },
    template: '<div v-if="visible" data-testid="stub-chat" />',
  },
  Button: { props: ['label'], emits: ['click'], template: '<button @click="$emit(\'click\')">{{ label }}</button>' },
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
  Dialog: { template: '<div><slot /></div>' },
  SelectButton: { template: '<div />' },
  Select: { template: '<div />' },
  InputText: { template: '<input />' },
  DatePicker: { template: '<div />' },
  MobileJobCloseoutDialog: { template: '<div />' },
  MobileInvoiceDialog: { template: '<div />' },
  MobileQuoteBuilderDialog: { template: '<div />' },
  MobileCustomerQuoteDialog: { template: '<div />' },
  MobileChangeOrderDialog: { template: '<div />' },
};

getMock.mockImplementation(async (url) => {
  const u = String(url);
  if (u.includes('/api/mobile/dispatch/threads')) {
    return { threads: [{ job_id: 'job-9', job_title: 'Spring replacement', unread_count: 1 }] };
  }
  if (u.includes('/api/mobile/job/')) {
    return {
      job: { id: 'job-123', title: 'Spring replacement', dispatch_status: 'assigned', customer: { id: 'c1', name: 'Acme' } },
      notes: [],
      photos: [],
    };
  }
  return [];
});

const settle = async () => {
  await flushPromises();
  await new Promise((r) => setTimeout(r, 0));
  await flushPromises();
};

async function start(url) {
  window.history.replaceState(null, '', url);
  const router = createRouter({
    history: createWebHistory(),
    routes: [
      { path: '/mobile/dispatch', component: () => import('../MobileDispatchView.vue') },
      { path: '/mobile/jobs/:id', component: () => import('../MobileJobDetailView.vue') },
      { path: '/elsewhere', component: { template: '<div>elsewhere</div>' } },
    ],
  });
  const Root = defineComponent({ setup: () => () => h(RouterView) });
  const w = mount(Root, { global: { plugins: [router], stubs } });
  await router.isReady();
  await settle();
  return { router, w };
}

async function awayAndBack(router) {
  await router.push('/elsewhere');
  await settle();
  const landed = new Promise((resolve) => {
    const off = router.afterEach(() => {
      off();
      resolve();
    });
  });
  router.back();
  await landed;
  await settle();
}

describe('Back after following a push link', () => {
  it('dispatcher: the thread does not reopen', async () => {
    const { router, w } = await start('/mobile/dispatch?job=job-9');
    expect(w.find('[data-testid="stub-chat"]').exists()).toBe(true);
    expect(router.currentRoute.value.fullPath).toBe('/mobile/dispatch');

    await awayAndBack(router);

    expect(router.currentRoute.value.fullPath).toBe('/mobile/dispatch');
    expect(w.find('[data-testid="stub-chat"]').exists()).toBe(false);
    w.unmount();
  });

  it('tech: the job chat does not reopen', async () => {
    const { router, w } = await start('/mobile/jobs/job-123?chat=1');
    expect(w.find('[data-testid="stub-chat"]').exists()).toBe(true);
    expect(router.currentRoute.value.fullPath).toBe('/mobile/jobs/job-123');

    await awayAndBack(router);

    expect(router.currentRoute.value.fullPath).toBe('/mobile/jobs/job-123');
    expect(w.find('[data-testid="stub-chat"]').exists()).toBe(false);
    w.unmount();
  });
});
