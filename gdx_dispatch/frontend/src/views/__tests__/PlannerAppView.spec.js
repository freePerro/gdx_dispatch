/**
 * PlannerAppView — the GDX Planner Android app's one page: the planner, a
 * Note button, phone alerts for reminders, and an office-only screen for a
 * technician signed in by mistake.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { createPinia, setActivePinia } from 'pinia';

const push = vi.fn(() => Promise.resolve());
vi.mock('vue-router', () => ({ useRouter: () => ({ push }), useRoute: () => ({ query: {} }) }));
const keeper = vi.hoisted(() => vi.fn());
vi.mock('../../composables/useOfficeAlertsKeeper', () => ({ useOfficeAlertsKeeper: keeper }));

import PlannerAppView from '../PlannerAppView.vue';
import { useAuthStore } from '../../stores/auth';

const MenuStub = {
  name: 'Menu',
  props: ['model'],
  methods: { toggle() {} },
  template: '<ul class="menu"><li v-for="i in model.filter((x) => !x.separator)" :key="i.label"><button class="mi" @click="i.command()">{{ i.label }}</button></li></ul>',
};
const stubs = {
  Button: { props: ['label'], template: `<button class="pbtn" @click="$emit('click', $event)">{{ label }}</button>` },
  Menu: MenuStub,
  MobilePlannerView: { template: '<div class="planner-body" />' },
  OfficeAlertsToggle: { name: 'OfficeAlertsToggle', props: { planner: Boolean }, template: '<div class="alerts" />' },
  QuickCaptureSheet: { name: 'QuickCaptureSheet', props: ['visible'], emits: ['update:visible', 'saved'], template: '<div v-if="visible" class="capture" />' },
  MobileServerConfigDialog: { template: '<div />' },
};

let auth;
beforeEach(() => {
  setActivePinia(createPinia());
  sessionStorage.clear();
  auth = useAuthStore();
  auth.accessToken = 'opaque-test-token';
  push.mockClear();
  keeper.mockClear();
});

const mountView = () => mount(PlannerAppView, { global: { stubs } });

describe('PlannerAppView', () => {
  it('an office user gets the planner, planner-worded alerts and a Note button', async () => {
    auth.user = { name: 'Office', role: 'dispatcher' };
    const w = mountView();
    await flushPromises();
    expect(w.find('.planner-body').exists()).toBe(true);
    expect(w.findComponent({ name: 'OfficeAlertsToggle' }).props('planner')).toBe(true);
    expect(w.find('[data-test="planner-app-office-only"]').exists()).toBe(false);
    await w.find('[data-test="planner-app-note"]').trigger('click');
    expect(w.find('.capture').exists()).toBe(true);
  });

  it('keeps phone alerts matched to the signed-in user, as AppLayout does elsewhere', () => {
    auth.user = { name: 'Office', role: 'dispatcher' };
    mountView();
    expect(keeper).toHaveBeenCalledTimes(1);
  });

  it('a saved note refreshes the planner', async () => {
    auth.user = { name: 'Office', role: 'dispatcher' };
    const seen = vi.fn();
    window.addEventListener('gdx:planner-refresh', seen);
    const w = mountView();
    await w.find('[data-test="planner-app-note"]').trigger('click');
    w.findComponent({ name: 'QuickCaptureSheet' }).vm.$emit('saved');
    window.removeEventListener('gdx:planner-refresh', seen);
    expect(seen).toHaveBeenCalledTimes(1);
  });

  it('a technician is told the app is for office accounts, with a way out', async () => {
    auth.user = { name: 'Tech', role: 'technician' };
    const w = mountView();
    expect(w.find('[data-test="planner-app-office-only"]').text()).toContain('GDX Planner is for office accounts');
    expect(w.find('.planner-body').exists()).toBe(false);
    const signOut = w.find('[data-test="planner-app-office-only"] .pbtn');
    await signOut.trigger('click');
    expect(push).toHaveBeenCalledWith('/login');
    expect(auth.isAuthenticated).toBe(false);
  });

  it('Sign out in the menu is a chosen logout, which turns this phone\'s alerts off', async () => {
    auth.user = { name: 'Office', role: 'dispatcher' };
    const events = [];
    const onLogout = (e) => events.push(e.detail);
    window.addEventListener('gdx:auth-logout', onLogout);
    const w = mountView();
    await w.findAll('.mi').find((b) => b.text() === 'Sign out').trigger('click');
    window.removeEventListener('gdx:auth-logout', onLogout);
    expect(events).toEqual([{ chosen: true }]);
    expect(push).toHaveBeenCalledWith('/login');
  });
});
