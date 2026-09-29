/**
 * A detail view that loads its record in onMounted must be REMOUNTED when
 * navigation moves it to a different record of the same kind — and must NOT
 * be remounted when EstimateView replaces /estimates/new -> /estimates/:id
 * after autosaving a draft.
 *
 * The defect this guards: Duplicate Estimate pushed /estimates/<copy> while
 * the original estimate stayed on screen, so the PDF showed the copy and the
 * editor showed the original until the user left and came back.
 *
 * These tests mount a real router + the REAL outlet App.vue renders
 * (KeyedRouterView) and count onMounted calls — a mock of the router would
 * prove nothing about whether Vue actually remounts, and a hand-built outlet
 * would stay green if the app's key binding were deleted.
 */
import { describe, expect, it } from 'vitest';
import { defineComponent, h, onMounted } from 'vue';
import { mount, flushPromises } from '@vue/test-utils';
import { createMemoryHistory, createRouter, useRoute } from 'vue-router';
import KeyedRouterView from '../../components/KeyedRouterView.vue';
import { isRecordSwitch, keepMountedThroughNextNavigation, navigationRemounted } from '../viewRemount';

function buildHarness() {
  const loads = [];
  const scrolls = [];
  const Detail = defineComponent({
    setup() {
      const route = useRoute();
      // Mirrors EstimateView: the record id is read ONCE, at mount.
      onMounted(() => loads.push(route.params.id ?? 'new'));
      return () => h('div', { class: 'detail' }, `record ${route.params.id ?? 'new'}`);
    },
  });
  const Other = defineComponent({ render: () => h('div', 'other') });
  const router = createRouter({
    history: createMemoryHistory(),
    // The app's scrollBehavior asks navigationRemounted(to); record its answer
    // at the moment vue-router actually calls the hook (after afterEach).
    scrollBehavior(to) {
      scrolls.push([to.fullPath, navigationRemounted(to)]);
      return false;
    },
    routes: [
      { path: '/estimates/new', name: 'create', component: Detail },
      { path: '/estimates/:id', name: 'detail', component: Detail },
      { path: '/other', component: Other },
    ],
  });
  return { router, App: KeyedRouterView, loads, scrolls };
}

async function start(path) {
  const harness = buildHarness();
  harness.router.push(path);
  await harness.router.isReady();
  const wrapper = mount(harness.App, { global: { plugins: [harness.router] } });
  await flushPromises();
  return { ...harness, wrapper };
}

describe('view remount on record switch', () => {
  it('remounts (and so reloads) when the same view moves to a different record', async () => {
    const { router, wrapper, loads } = await start('/estimates/A');
    expect(loads).toEqual(['A']);
    await router.push('/estimates/B');
    await flushPromises();
    expect(loads).toEqual(['A', 'B']);
    expect(wrapper.text()).toBe('record B');
  });

  it('does NOT remount when the draft announces it is getting its own id', async () => {
    // EstimateView's autosave draft-create and manual Create.
    const { router, loads } = await start('/estimates/new');
    keepMountedThroughNextNavigation('/estimates/A');
    await router.replace('/estimates/A');
    await flushPromises();
    expect(loads).toEqual(['new']);
  });

  it('the keep-mounted mark covers ONE navigation only', async () => {
    const { router, loads } = await start('/estimates/new');
    keepMountedThroughNextNavigation('/estimates/A');
    await router.replace('/estimates/A');
    await flushPromises();
    await router.push('/estimates/B');
    await flushPromises();
    expect(loads).toEqual(['new', 'B']);
  });

  it('a mark for one URL does not exempt a navigation to another', async () => {
    const { router, loads } = await start('/estimates/new');
    keepMountedThroughNextNavigation('/estimates/DRAFT');
    await router.push('/estimates/B');
    await flushPromises();
    expect(loads).toEqual(['new', 'B']);
  });

  it('a mark whose navigation was aborted cannot exempt a later switch', async () => {
    const { router, loads } = await start('/estimates/A');
    router.beforeEach((to) => (to.path === '/estimates/DRAFT' ? false : true));
    keepMountedThroughNextNavigation('/estimates/DRAFT');
    await router.replace('/estimates/DRAFT');  // guard aborts it
    await router.push('/estimates/B');
    await flushPromises();
    expect(loads).toEqual(['A', 'B']);
  });

  it('an unrelated navigation cancelled by the marked one does not spend the mark', async () => {
    // A push to B is still waiting in an async guard when the draft POST
    // returns and the view flips to its own id. B's cancelled afterEach must
    // not clear the mark before the flip's afterEach reads it.
    const { router, loads } = await start('/estimates/new');
    let releaseB;
    router.beforeEach((to) => (to.path === '/estimates/B'
      ? new Promise((r) => { releaseB = () => r(true); })
      : true));
    const pendingB = router.push('/estimates/B');
    await flushPromises();
    keepMountedThroughNextNavigation('/estimates/DRAFT');
    const flip = router.replace('/estimates/DRAFT');
    releaseB();
    await Promise.all([pendingB, flip]);
    await flushPromises();
    expect(router.currentRoute.value.fullPath).toBe('/estimates/DRAFT');
    expect(loads).toEqual(['new']);
  });

  it('remounts when something else opens an estimate from the blank form', async () => {
    // Command palette from /estimates/new: the blank form must not stay up
    // over B, or its first edit autosaves blanks onto B.
    const { router, wrapper, loads } = await start('/estimates/new');
    await router.push('/estimates/B');
    await flushPromises();
    expect(loads).toEqual(['new', 'B']);
    expect(wrapper.text()).toBe('record B');
  });

  it('remounts when an open record moves to the blank create screen', async () => {
    // Topbar "New Estimate" from /estimates/A: A's form must not survive
    // into the new draft.
    const { router, wrapper, loads } = await start('/estimates/A');
    await router.push('/estimates/new');
    await flushPromises();
    expect(loads).toEqual(['A', 'new']);
    expect(wrapper.text()).toBe('record new');
  });

  it('does NOT remount on a query-only change', async () => {
    const { router, loads } = await start('/estimates/A');
    await router.replace('/estimates/A?tab=lines');
    await flushPromises();
    expect(loads).toEqual(['A']);
  });

  it('mounts normally when moving between different views', async () => {
    const { router, loads } = await start('/other');
    await router.push('/estimates/A');
    await flushPromises();
    expect(loads).toEqual(['A']);
  });
});

describe('scrolling agrees with remounting', () => {
  it('a record switch reports remounted (go to top)', async () => {
    const { router, scrolls } = await start('/estimates/A');
    await router.push('/estimates/B');
    await flushPromises();
    expect(scrolls.at(-1)).toEqual(['/estimates/B', true]);
  });

  it('the kept-mounted draft flip reports NOT remounted (keep the place)', async () => {
    const { router, scrolls } = await start('/estimates/new');
    keepMountedThroughNextNavigation('/estimates/A');
    await router.replace('/estimates/A');
    await flushPromises();
    expect(scrolls.at(-1)).toEqual(['/estimates/A', false]);
  });

  it('a query-only change reports NOT remounted', async () => {
    const { router, scrolls } = await start('/estimates/A');
    await router.replace('/estimates/A?tab=lines');
    await flushPromises();
    expect(scrolls.at(-1)).toEqual(['/estimates/A?tab=lines', false]);
  });
});

describe('isRecordSwitch', () => {
  const C = {};
  const r = (params) => ({ params, matched: [{ components: { default: C } }] });
  it('is true for same view leaving a record for a different one or for none', () => {
    expect(isRecordSwitch(r({ id: 'B' }), r({ id: 'A' }))).toBe(true);
    expect(isRecordSwitch(r({ id: 'A' }), r({ id: 'A' }))).toBe(false);
    expect(isRecordSwitch(r({ id: 'A' }), r({}))).toBe(true);
    expect(isRecordSwitch(r({}), r({ id: 'A' }))).toBe(true);
    expect(isRecordSwitch(r({}), r({}))).toBe(false);
    const other = { params: { id: 'A' }, matched: [{ components: { default: {} } }] };
    expect(isRecordSwitch(r({ id: 'B' }), other)).toBe(false);
  });
});
