/**
 * Customer portal — job photos (Doug 2026-08-12: "make the photos customer
 * facing").
 *
 * The tech photographs every job; the customer had never been shown one.
 *
 * Pinned:
 *  1. The photos link comes from the job list's own photo_count — no request
 *     per row — and only appears when the job actually has photos.
 *  2. Opening it reads the PORTAL route, never the staff document download
 *     (which needs a staff Bearer token no customer will ever hold).
 *  3. Each photo is fetched authenticated and rendered from a blob, because an
 *     <img src> cannot carry the portal token.
 *  4. The link lives in the FIRST column. This table does not stack on narrow
 *     screens (PrimeVue 4 dropped responsiveLayout="stack"), so a trailing
 *     column is off-screen on a phone — which is where customers read this.
 *     Caught on a real Pixel, not by a test.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

const toastAdd = vi.fn();

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: {}, params: {} }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

import CustomerPortalView from '../CustomerPortalView.vue';

const SRC = readFileSync(join(__dirname, '..', 'CustomerPortalView.vue'), 'utf8');

const JOBS = [
  { id: 'job-1', title: 'Spring replacement', lifecycle_stage: 'completed', photo_count: 2 },
  { id: 'job-2', title: 'Tune up', lifecycle_stage: 'completed', photo_count: 0 },
];
const PHOTOS = [
  { id: 'ph-1', kind: 'before', caption: null, url: '/portal/jobs/job-1/photos/ph-1' },
  { id: 'ph-2', kind: 'after', caption: 'New spring installed', url: '/portal/jobs/job-1/photos/ph-2' },
];

const stubs = {
  Button: {
    props: ['label', 'icon', 'severity', 'text', 'size', 'loading', 'disabled'],
    emits: ['click'],
    template: '<button :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')">{{ label }}</button>',
    inheritAttrs: false,
  },
  DataTable: {
    props: ['value'],
    template: '<div><template v-for="(row, i) in value" :key="i"><slot name="default" /><div class="row"><slot /></div></template></div>',
  },
  Column: {
    props: ['field', 'header'],
    template: '<div><slot name="body" :data="$parent.$props.value ? $parent.$props.value[0] : {}" /></div>',
  },
  Dialog: {
    props: ['visible'],
    template: '<div v-if="visible" :data-testid="$attrs[\'data-testid\']"><slot /></div>',
    inheritAttrs: false,
  },
  Image: {
    props: ['src', 'alt'],
    template: '<img :src="src" :alt="alt" />',
  },
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot name="footer" /></div>' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<div><slot /></div>' },
  TabPanels: { template: '<div><slot /></div>' },
  TabPanel: { template: '<div><slot /></div>' },
  Tag: { props: ['value'], template: '<span>{{ value }}</span>' },
  Message: { template: '<div><slot /></div>' },
  InputText: { template: '<input />' },
  Password: { template: '<input type="password" />' },
  Checkbox: { template: '<input type="checkbox" />' },
  Divider: { template: '<hr />' },
  ProgressSpinner: { template: '<div class="spinner" />' },
  Toast: { template: '<div />' },
};

beforeEach(() => {
  vi.clearAllMocks();
  sessionStorage.clear();
  localStorage.clear();
});

describe('customer portal — job photos', () => {
  it('reads the portal photo route, never the staff document download', async () => {
    // Source-level: the request shape is the security boundary here, and a
    // mounted assertion on a heavy view would hide it behind stubs.
    expect(SRC).toMatch(/authedFetch\(`\/portal\/jobs\/\$\{job\.id\}\/photos`\)/);
    expect(SRC).not.toMatch(/api\/documents\/\$\{[^}]+\}\/download/);
  });

  it('fetches each photo with the portal bearer token and renders a blob', () => {
    const fn = SRC.slice(SRC.indexOf('async function openJobPhotos'));
    const body = fn.slice(0, fn.indexOf('\n}'));
    expect(body).toMatch(/Authorization: `Bearer \$\{jwt\.value\}`/);
    expect(body).toMatch(/URL\.createObjectURL/);
  });

  it('frees the blobs when the dialog closes', () => {
    expect(SRC).toMatch(/watch\(jobPhotosVisible[\s\S]{0,120}clearJobPhotoImages\(\)/);
    expect(SRC).toMatch(/function clearJobPhotoImages[\s\S]{0,160}revokeObjectURL/);
  });

  it('puts the photos link in the FIRST column, where a phone can reach it', () => {
    const jobsTable = SRC.slice(SRC.indexOf('data-testid="jobs-table"'));
    const firstColumn = jobsTable.slice(0, jobsTable.indexOf('header="Status"'));
    // The link must sit inside the Job column, before Status.
    expect(firstColumn).toMatch(/job-photos-\$\{?/);
    expect(firstColumn).toMatch(/openJobPhotos\(data\)/);
  });

  it('offers the link only when the job has photos', () => {
    const jobsTable = SRC.slice(SRC.indexOf('data-testid="jobs-table"'));
    const firstColumn = jobsTable.slice(0, jobsTable.indexOf('header="Status"'));
    expect(firstColumn).toMatch(/v-if="data\.photo_count"/);
  });

  it('mounts and loads photos for a job through the portal route', async () => {
    const fetchMock = vi.fn(async (url) => {
      const u = String(url);
      if (u.includes('/portal/context')) {
        return { ok: true, json: async () => ({ company: { name: 'GDX' } }) };
      }
      if (u.includes('/portal/jobs/job-1/photos/')) {
        return { ok: true, blob: async () => new Blob(['x'], { type: 'image/jpeg' }) };
      }
      if (u.includes('/portal/jobs/job-1/photos')) {
        return { ok: true, json: async () => PHOTOS };
      }
      if (u.includes('/portal/jobs')) return { ok: true, json: async () => JOBS };
      return { ok: true, json: async () => [] };
    });
    vi.stubGlobal('fetch', fetchMock);
    global.URL.createObjectURL = vi.fn(() => 'blob:photo');
    global.URL.revokeObjectURL = vi.fn();
    sessionStorage.setItem('gdx_portal_jwt', 'portal-token');

    const wrapper = mount(CustomerPortalView, { global: { stubs } });
    await flushPromises();

    // Drive the handler directly — the table stubs can't reproduce PrimeVue's
    // row rendering, and what matters is which URL the customer's click hits.
    await wrapper.vm.openJobPhotos({ id: 'job-1', title: 'Spring replacement' });
    await flushPromises();

    const called = fetchMock.mock.calls.map(([u]) => String(u));
    expect(called.some((u) => u.includes('/portal/jobs/job-1/photos/ph-1'))).toBe(true);
    expect(called.some((u) => u.includes('/api/documents/'))).toBe(false);
    expect(wrapper.vm.jobPhotoImages.length).toBe(2);
    expect(wrapper.vm.jobPhotoImages[1].label).toBe('New spring installed');
  });
});
