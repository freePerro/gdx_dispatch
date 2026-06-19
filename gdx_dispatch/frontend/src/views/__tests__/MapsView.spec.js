/**
 * MapsView regression test — the hard-reload escape that S110 added
 * (onBeforeRouteLeave → window.location.assign) was REMOVED in the
 * AppLayout-into-App.vue refactor. The proper fix is structural:
 * AppLayout is mounted once at App.vue and stays mounted across
 * navigations, so the same-root-component-swap bug class that made
 * Google Maps' DOM mutations leave an orphaned section in <main> can no
 * longer fire — there is no AppLayout swap on nav, only a slot content
 * swap.
 *
 * This test now asserts the WORKAROUND IS GONE. If a future change
 * re-introduces the onBeforeRouteLeave guard, this test fails — that's
 * the signal to investigate why the SPA-stuck bug came back rather than
 * silently re-applying the patch.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount } from '@vue/test-utils';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

let capturedGuard = null;
vi.mock('vue-router', () => ({
  onBeforeRouteLeave: (fn) => { capturedGuard = fn; },
  useRoute: () => ({ path: '/maps', fullPath: '/maps' }),
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: vi.fn().mockResolvedValue({ tech_locations: [], routes: [] }) }),
}));

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<button><slot /></button>' },
  TabPanels: { template: '<div><slot /></div>' },
  TabPanel: { template: '<section><slot /></section>' },
  DataTable: { template: '<div><slot /></div>' },
  Column: { template: '<div />' },
  Select: { template: '<select />' },
  InputText: { template: '<input />' },
  Button: { template: '<button><slot /></button>' },
  ProgressSpinner: { template: '<div />' },
};

describe('MapsView — AppLayout refactor removed the hard-reload escape', () => {
  beforeEach(() => {
    capturedGuard = null;
  });

  it('does NOT register an onBeforeRouteLeave guard', async () => {
    const MapsView = (await import('../MapsView.vue')).default;
    mount(MapsView, { global: { stubs } });
    expect(capturedGuard).toBeNull();
  });

  it('source no longer imports onBeforeRouteLeave from vue-router', () => {
    const src = readFileSync(
      join(__dirname, '..', 'MapsView.vue'),
      'utf8',
    );
    expect(src).not.toMatch(/onBeforeRouteLeave/);
    // The hard-reload pattern (location.assign on outgoing nav) is also gone.
    expect(src).not.toMatch(/window\.location\.assign\s*\(\s*to\.fullPath/);
  });

  it('source no longer wraps in <AppLayout> (App.vue mounts the shell once)', () => {
    const src = readFileSync(
      join(__dirname, '..', 'MapsView.vue'),
      'utf8',
    );
    expect(src).not.toMatch(/<AppLayout[\s/>]/);
  });
});
