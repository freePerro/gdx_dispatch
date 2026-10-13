/**
 * What's new — the release-notes page, the post-update banner, and the
 * seen-tracking that decides when the banner shows.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import PrimeVue from 'primevue/config';

const { apiGet, apiPost, route } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  route: { path: '/release-notes', params: {}, query: {} },
}));

vi.mock('../../composables/useApi', () => ({
  createApiClient: () => ({ get: apiGet, post: apiPost }),
}));
vi.mock('vue-router', async () => {
  const { reactive: r } = await import('vue');
  const state = r(route);
  return {
    useRoute: () => state,
    useRouter: () => ({ push: vi.fn() }),
    __state: state,
  };
});

// eslint-disable-next-line import/first
import * as vueRouter from 'vue-router';
// eslint-disable-next-line import/first
import ReleaseNotesView from '../ReleaseNotesView.vue';
// eslint-disable-next-line import/first
import WhatsNewBanner from '../../components/WhatsNewBanner.vue';
// eslint-disable-next-line import/first
import { useAuthStore } from '../../stores/auth';
// eslint-disable-next-line import/first
import { compareVersions, hasSeenRelease, parseVersion, seenTourId } from '../../composables/useReleaseNotes';

const routeState = vueRouter.__state;

function release(version, extra = {}) {
  return {
    version,
    date: '2026-10-12',
    title: '',
    warning: '',
    sections: [{ title: 'Highlights', blocks: [{ kind: 'list', items: [`Item for ${version}`] }] }],
    changes: [],
    ...extra,
  };
}

const NOTES = {
  installed_version: '1.146.0',
  releases: [
    release('1.146.0', {
      title: 'Job-lane tax',
      changes: ['fix(tax): change orders follow the lane (#1073)', 'feat(time-off): vacation left (#1043)'],
    }),
    release('1.145.0'),
    release('1.41.0', { warning: 'Migration 056 writes wrong tax rates.', title: 'Do not deploy' }),
    ...Array.from({ length: 30 }, (_, i) => release(`1.${i}.0`)),
    release('1.2.0', {
      sections: [{ title: 'Highlights', blocks: [
        { kind: 'paragraph', text: '<b>not html</b>' },
        { kind: 'list', items: ['Customer portal launched'] },
      ] }],
    }),
  ],
};

const jwt = (role) => `h.${btoa(JSON.stringify({ role, sub: 'u1' })).replace(/=+$/, '')}.s`;

function signIn(role, permissions = []) {
  const auth = useAuthStore();
  auth.accessToken = jwt(role);
  auth.permissions = new Set(permissions);
  auth.permissionsLoaded = true;
}

function respond(map) {
  apiGet.mockImplementation((url) => (url in map ? Promise.resolve(map[url]) : Promise.reject(new Error(url))));
}

function mountView(options = {}) {
  return mount(ReleaseNotesView, {
    global: { plugins: [PrimeVue], stubs: { RouterLink: RouterLinkStub } },
    ...options,
  });
}

function mountBanner() {
  return mount(WhatsNewBanner, {
    global: { plugins: [PrimeVue], stubs: { RouterLink: RouterLinkStub } },
  });
}

beforeEach(() => {
  setActivePinia(createPinia());
  window.dispatchEvent(new Event('gdx:auth-logout'));
  localStorage.clear();
  sessionStorage.clear();
  apiGet.mockReset();
  apiPost.mockReset().mockResolvedValue({});
  routeState.path = '/release-notes';
  routeState.params = {};
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('ReleaseNotesView', () => {
  it('lists releases newest first with the installed badge, and pages older ones', async () => {
    signIn('technician');
    respond({ '/api/release-notes': NOTES });
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.get('[data-testid="release-notes-installed"]').text()).toContain('v1.146.0');
    const cards = wrapper.findAll('article.release');
    expect(cards).toHaveLength(20);
    expect(cards[0].attributes('data-testid')).toBe('release-1.146.0');
    expect(cards[0].find('[data-testid="release-installed-tag"]').exists()).toBe(true);
    expect(cards[1].find('[data-testid="release-installed-tag"]').exists()).toBe(false);

    await wrapper.get('[data-testid="release-notes-show-more"]').trigger('click');
    expect(wrapper.findAll('article.release').length).toBe(NOTES.releases.length);
  });

  it('shows a release warning and renders note text as text, never HTML', async () => {
    signIn('technician');
    respond({ '/api/release-notes': NOTES });
    const wrapper = mountView();
    await flushPromises();
    await wrapper.get('[data-testid="release-notes-search"]').setValue('');
    expect(wrapper.get('[data-testid="release-1.41.0"]').text()).toContain('Migration 056 writes wrong tax rates.');

    await wrapper.get('[data-testid="release-notes-search"]').setValue('portal');
    const card = wrapper.get('[data-testid="release-1.2.0"]');
    expect(card.find('b').exists()).toBe(false);
    expect(card.text()).toContain('<b>not html</b>');
  });

  it('searches every release, including the change lists, with all words required', async () => {
    signIn('technician');
    respond({ '/api/release-notes': NOTES });
    const wrapper = mountView();
    await flushPromises();
    const search = wrapper.get('[data-testid="release-notes-search"]');

    await search.setValue('customer PORTAL');
    expect(wrapper.findAll('article.release').map((a) => a.attributes('data-testid'))).toEqual(['release-1.2.0']);

    await search.setValue('vacation');
    expect(wrapper.findAll('article.release').map((a) => a.attributes('data-testid'))).toEqual(['release-1.146.0']);

    await search.setValue('portal vacation');
    expect(wrapper.find('[data-testid="release-notes-empty"]').text()).toContain('No release mentions');
  });

  it('expands and collapses the full change list', async () => {
    signIn('technician');
    respond({ '/api/release-notes': NOTES });
    const wrapper = mountView();
    await flushPromises();
    const toggle = wrapper.get('[data-testid="release-changes-toggle-1.146.0"]');
    expect(toggle.text()).toContain('Show all changes (2)');
    expect(wrapper.find('[data-testid="release-changes-1.146.0"]').exists()).toBe(false);
    await toggle.trigger('click');
    expect(wrapper.get('[data-testid="release-changes-1.146.0"]').text()).toContain('vacation left (#1043)');
    await toggle.trigger('click');
    expect(wrapper.find('[data-testid="release-changes-1.146.0"]').exists()).toBe(false);
  });

  it('a deep link to an old release shows it even past the first page', async () => {
    signIn('technician');
    routeState.params = { version: 'v1.2.0' };
    respond({ '/api/release-notes': NOTES });
    // The router resets the scroller to the top on an animation frame after
    // navigation; the view must scroll after that frame, or it is undone.
    const frames = [];
    vi.stubGlobal('requestAnimationFrame', (cb) => frames.push(cb));
    const wrapper = mountView({ attachTo: document.body });
    await flushPromises();
    const card = wrapper.get('[data-testid="release-1.2.0"]');
    expect(card.classes()).toContain('targeted');
    expect(Element.prototype.scrollIntoView).not.toHaveBeenCalled();
    while (frames.length) frames.shift()();
    await flushPromises();
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
    vi.unstubAllGlobals();
    wrapper.unmount();
  });

  it('a deep link to an unknown release says so and still lists the rest', async () => {
    signIn('technician');
    routeState.params = { version: '9.9.9' };
    respond({ '/api/release-notes': NOTES });
    const wrapper = mountView();
    await flushPromises();
    expect(wrapper.get('[data-testid="release-notes-missing"]').text()).toContain('v9.9.9');
    expect(wrapper.findAll('article.release').length).toBe(20);
  });

  it('opening the page records the installed notes as seen', async () => {
    signIn('technician');
    respond({ '/api/release-notes': NOTES });
    mountView();
    await flushPromises();
    expect(apiPost).toHaveBeenCalledWith('/api/me/tours/release-notes-v1-146-0/complete', {});
  });

  it('shows the update notice to settings.read holders only, checking once per session', async () => {
    signIn('admin', ['settings.read']);
    const update = { current: '1.146.0', latest: '1.147.0', update_available: true, notes_url: 'https://example.com/r', error: null };
    respond({ '/api/release-notes': NOTES, '/api/admin/update-check': update });
    const wrapper = mountView();
    await flushPromises();
    expect(wrapper.get('[data-testid="release-notes-update-available"]').text()).toContain('v1.147.0');
    mountView();
    await flushPromises();
    expect(apiGet.mock.calls.filter(([u]) => u === '/api/admin/update-check')).toHaveLength(1);
  });

  it('never calls the update check without settings.read', async () => {
    signIn('technician', ['jobs.read']);
    respond({ '/api/release-notes': NOTES });
    const wrapper = mountView();
    await flushPromises();
    expect(wrapper.find('[data-testid="release-notes-update-available"]').exists()).toBe(false);
    expect(apiGet.mock.calls.some(([u]) => u === '/api/admin/update-check')).toBe(false);
  });
});

describe('WhatsNewBanner', () => {
  beforeEach(() => {
    signIn('technician');
    routeState.path = '/dashboard';
  });

  it('shows once after an update and records the dismissal', async () => {
    respond({ '/api/release-notes': NOTES, '/api/me/tours': { available: true, progress: {} } });
    const wrapper = mountBanner();
    await flushPromises();
    expect(wrapper.get('[data-testid="whats-new-banner"]').text()).toContain('v1.146.0');
    expect(wrapper.getComponent(RouterLinkStub).props('to')).toBe('/release-notes/1.146.0');

    await wrapper.get('[data-testid="whats-new-banner-dismiss"]').trigger('click');
    await flushPromises();
    expect(wrapper.find('[data-testid="whats-new-banner"]').exists()).toBe(false);
    expect(apiPost).toHaveBeenCalledWith('/api/me/tours/release-notes-v1-146-0/complete', {});
  });

  it('stays hidden once the user has seen these notes, or newer ones (rollback)', async () => {
    for (const id of ['release-notes-v1-146-0', 'release-notes-v1-147-0']) {
      window.dispatchEvent(new Event('gdx:auth-logout'));
      respond({
        '/api/release-notes': NOTES,
        '/api/me/tours': { available: true, progress: { [id]: { status: 'completed' } } },
      });
      const wrapper = mountBanner();
      await flushPromises();
      expect(wrapper.find('[data-testid="whats-new-banner"]').exists()).toBe(false);
    }
  });

  it('stays hidden on a dev build and when the installed release has no notes', async () => {
    for (const installed of [null, '1.150.0']) {
      window.dispatchEvent(new Event('gdx:auth-logout'));
      respond({
        '/api/release-notes': { ...NOTES, installed_version: installed },
        '/api/me/tours': { available: true, progress: {} },
      });
      const wrapper = mountBanner();
      await flushPromises();
      expect(wrapper.find('[data-testid="whats-new-banner"]').exists()).toBe(false);
    }
  });

  it('hides when the user opens the notes page another way', async () => {
    respond({ '/api/release-notes': NOTES, '/api/me/tours': { available: true, progress: {} } });
    const wrapper = mountBanner();
    await flushPromises();
    expect(wrapper.find('[data-testid="whats-new-banner"]').exists()).toBe(true);
    routeState.path = '/release-notes';
    await flushPromises();
    expect(wrapper.find('[data-testid="whats-new-banner"]').exists()).toBe(false);
  });

  it('falls back to this device\'s record when the server has no tour table', async () => {
    localStorage.setItem('gdx_release_notes_seen_v1', JSON.stringify({ 'release-notes-v1-146-0': { status: 'completed' } }));
    respond({ '/api/release-notes': NOTES, '/api/me/tours': { available: false, progress: {} } });
    const wrapper = mountBanner();
    await flushPromises();
    expect(wrapper.find('[data-testid="whats-new-banner"]').exists()).toBe(false);
  });

  it('a dismiss whose save failed still keeps the banner away on this device', async () => {
    respond({ '/api/release-notes': NOTES, '/api/me/tours': { available: true, progress: {} } });
    apiPost.mockRejectedValue(new Error('offline'));
    const first = mountBanner();
    await flushPromises();
    await first.get('[data-testid="whats-new-banner-dismiss"]').trigger('click');
    await flushPromises();
    first.unmount();

    const second = mountBanner();
    await flushPromises();
    expect(second.find('[data-testid="whats-new-banner"]').exists()).toBe(false);
  });

  it('the next person to sign in on a shared device still gets the banner', async () => {
    respond({ '/api/release-notes': NOTES, '/api/me/tours': { available: true, progress: {} } });
    apiPost.mockRejectedValue(new Error('offline'));
    const first = mountBanner();
    await flushPromises();
    await first.get('[data-testid="whats-new-banner-dismiss"]').trigger('click');
    await flushPromises();
    first.unmount();
    window.dispatchEvent(new Event('gdx:auth-logout'));

    const second = mountBanner();
    await flushPromises();
    expect(second.find('[data-testid="whats-new-banner"]').exists()).toBe(true);
  });
});

describe('useReleaseNotes helpers', () => {
  it('parses and compares versions numerically', () => {
    expect(parseVersion('v1.10.0')).toEqual([1, 10, 0]);
    expect(parseVersion('latest')).toBeNull();
    expect(compareVersions([1, 10, 0], [1, 9, 9])).toBeGreaterThan(0);
  });

  it('builds tour ids the server accepts', () => {
    expect(seenTourId('1.146.0')).toBe('release-notes-v1-146-0');
    expect(seenTourId('1.146.0')).toMatch(/^[a-z0-9][a-z0-9_-]{0,63}$/);
  });

  it('counts only completed or skipped rows at or above the installed version', () => {
    const row = (status) => ({ status });
    expect(hasSeenRelease({ 'release-notes-v1-146-0': row('completed') }, '1.146.0')).toBe(true);
    expect(hasSeenRelease({ 'release-notes-v1-146-0': row('skipped') }, '1.146.0')).toBe(true);
    expect(hasSeenRelease({ 'release-notes-v1-146-0': row('started') }, '1.146.0')).toBe(false);
    expect(hasSeenRelease({ 'release-notes-v1-145-0': row('completed') }, '1.146.0')).toBe(false);
    expect(hasSeenRelease({ 'release-notes-v1-200-0': row('completed') }, '1.146.0')).toBe(true);
    expect(hasSeenRelease({ 'office-tour': row('completed') }, '1.146.0')).toBe(false);
  });
});
