/**
 * 2026-08-31 — bottom-nav / FAB clearance is role-aware.
 *
 * AppLayout pads .layout-content for the quick-capture FAB only when the FAB
 * renders (non-technician roles on mobile), via `has-capture-fab` on the
 * layout root — and mirrors the same fact onto <body> because driver.js
 * mounts the tour popover outside the layout tree. Locks:
 *   - admin on mobile  → layout + body carry has-capture-fab
 *   - tech on mobile   → neither does
 *   - desktop          → body class off (no bottom nav at all)
 *   - unmount          → body class removed (no stale class across shells)
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { mount } from '@vue/test-utils';
import { nextTick } from 'vue';

const authState = { role: 'admin', user: { role: 'admin' } };
vi.mock('../../stores/auth', () => ({ useAuthStore: () => authState }));
vi.mock('../../composables/useTour', () => ({ useTour: () => ({ autoLaunchForUser: vi.fn() }) }));
vi.mock('vue-router', () => ({ useRoute: () => ({ path: '/jobs', matched: [] }) }));

const Stub = { template: '<div />' };
const stubs = {
  AppSidebar: Stub, AppTopbar: Stub, AppBottomNav: Stub, HelpDrawer: Stub,
  ConfirmDialog: Stub, NotificationsDrawer: Stub, Breadcrumb: Stub, BugReportButton: Stub,
  Drawer: { props: ['visible'], template: '<div><slot /></div>' },
};

import AppLayout from '../AppLayout.vue';

let originalWidth;
function setViewport(width) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, writable: true, value: width });
  window.dispatchEvent(new Event('resize'));
}

beforeEach(() => {
  originalWidth = window.innerWidth;
  document.body.classList.remove('has-capture-fab');
});
afterEach(() => {
  setViewport(originalWidth);
  document.body.classList.remove('has-capture-fab');
});

async function mountAt(width, role) {
  authState.role = role;
  authState.user = { role };
  setViewport(width);
  const wrapper = mount(AppLayout, { global: { stubs } });
  await nextTick();
  return wrapper;
}

describe('AppLayout — FAB-aware clearance class', () => {
  it('admin on mobile: layout root and <body> carry has-capture-fab', async () => {
    const w = await mountAt(390, 'admin');
    expect(w.find('.app-layout').classes()).toContain('mobile');
    expect(w.find('.app-layout').classes()).toContain('has-capture-fab');
    expect(document.body.classList.contains('has-capture-fab')).toBe(true);
    w.unmount();
  });

  it('technician on mobile: no FAB, so neither carries the class', async () => {
    const w = await mountAt(390, 'technician');
    expect(w.find('.app-layout').classes()).toContain('mobile');
    expect(w.find('.app-layout').classes()).not.toContain('has-capture-fab');
    expect(document.body.classList.contains('has-capture-fab')).toBe(false);
    w.unmount();
  });

  it('desktop: body class stays off (there is no bottom nav to clear)', async () => {
    const w = await mountAt(1280, 'admin');
    expect(w.find('.app-layout').classes()).not.toContain('mobile');
    expect(document.body.classList.contains('has-capture-fab')).toBe(false);
    w.unmount();
  });

  it('unmount removes the body class so it cannot leak into a no-shell route', async () => {
    const w = await mountAt(390, 'admin');
    expect(document.body.classList.contains('has-capture-fab')).toBe(true);
    w.unmount();
    expect(document.body.classList.contains('has-capture-fab')).toBe(false);
  });
});
