/**
 * ReferralsView speaks routers/referrals.py (GDXA-1693).
 *
 * The page posted {referrer_customer, referred_name, status, reward, notes},
 * which ReferralCreateIn refuses with a 422 every time; offered contacted and
 * declined, which the PATCH refuses with a 400; and read three columns the
 * list never returns. Every earlier check asserted the view against itself.
 * These tests read the router source, so a change on either side fails here.
 */
import { flushPromises, mount } from '@vue/test-utils';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: mockGet, post: mockPost, patch: mockPatch }),
}));

import ReferralsView from '../ReferralsView.vue';

const PY = readFileSync(
  join(__dirname, '..', '..', '..', '..', 'routers', 'referrals.py'),
  'utf8',
);

/** Field lines of a pydantic model: { name, required }. */
function modelFields(name) {
  const at = PY.indexOf(`class ${name}(`);
  expect(at, `${name} not found`).toBeGreaterThan(-1);
  const body = PY.slice(at).split('\n').slice(1);
  const fields = [];
  for (const line of body) {
    if (line.trim() && !line.startsWith(' ')) break;
    const m = line.match(/^ {4}(\w+): ([^=]+?)(?: = (.*))?$/);
    if (!m || m[1] === 'model_config') continue;
    const dflt = m[3] || '';
    fields.push({ name: m[1], required: !/default=|^None\b/.test(dflt) });
  }
  return fields;
}

function allowedStatuses() {
  const m = PY.match(/ALLOWED_STATUSES = \{([^}]*)\}/);
  expect(m, 'ALLOWED_STATUSES not found').toBeTruthy();
  return [...m[1].matchAll(/"(\w+)"/g)].map((x) => x[1]).sort();
}

const passthrough = { template: '<div><slot /><slot name="start" /><slot name="end" /><slot name="footer" /></div>' };
const DataTable = {
  props: ['value'],
  template: `<div data-testid="table">
    <div v-for="row in value" :key="row.id" class="row">{{ row.referee_name }}|{{ row.status }}</div>
    <slot v-if="!value || !value.length" name="empty" />
  </div>`,
};

function mountView() {
  return mount(ReferralsView, {
    global: {
      stubs: {
        Toolbar: passthrough,
        Dialog: passthrough,
        DataTable,
        Column: true,
        Tag: true,
        Button: { props: ['label'], template: '<button>{{ label }}</button>' },
        ProgressSpinner: true,
        AutoComplete: true,
        InputText: true,
        Select: true,
        'router-link': { template: '<a><slot /></a>' },
      },
      directives: { tooltip: {} },
    },
  });
}

const ROWS = [
  { id: 'r1', referrer_id: 'c1', referrer_name: 'Alex Advocate', referee_name: 'Pat Lee', referee_phone: '555-0100', status: 'pending', reward_given: 0, created_at: '2026-10-01T00:00:00Z' },
  { id: 'r2', referrer_id: 'c1', referrer_name: 'Alex Advocate', referee_name: 'Sam Roe', referee_phone: '555-0101', status: 'rewarded', reward_given: 1, created_at: '2026-10-02T00:00:00Z' },
];

function routeGet(rows = ROWS) {
  mockGet.mockImplementation((url) => {
    if (url === '/api/referrals') return Promise.resolve({ items: rows });
    return Promise.reject(new Error(`unexpected ${url}`));
  });
}

describe('ReferralsView', () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
    mockPatch.mockReset();
  });

  it('posts exactly the fields ReferralCreateIn declares, with every required one', async () => {
    routeGet([]);
    mockPost.mockResolvedValue({ id: 'new' });
    const wrapper = mountView();
    await flushPromises();

    const vm = wrapper.vm;
    vm.openCreate();
    vm.referrerPick = { label: 'Alex Advocate', value: 'c1' };
    vm.form.referee_name = '  Pat Lee ';
    vm.form.referee_phone = ' 555-0100 ';
    vm.form.referee_email = 'pat@example.com';
    await vm.createReferral();
    await flushPromises();

    expect(mockPost).toHaveBeenCalledTimes(1);
    const [url, body] = mockPost.mock.calls[0];
    expect(url).toBe('/api/referrals');
    expect(body).toEqual({
      referrer_id: 'c1',
      referee_name: 'Pat Lee',
      referee_phone: '555-0100',
      referee_email: 'pat@example.com',
    });

    const fields = modelFields('ReferralCreateIn');
    const declared = fields.map((f) => f.name).sort();
    const required = fields.filter((f) => f.required).map((f) => f.name).sort();
    expect(required).toEqual(['referee_name', 'referee_phone', 'referrer_id']);
    for (const key of Object.keys(body)) expect(declared).toContain(key);
    for (const key of required) expect(body[key], key).toBeTruthy();
  });

  it('will not post without a picked referrer, a name and a phone', async () => {
    routeGet([]);
    const wrapper = mountView();
    await flushPromises();

    const vm = wrapper.vm;
    vm.openCreate();
    vm.form.referee_name = 'Pat Lee';
    vm.form.referee_phone = '555-0100';
    await vm.createReferral();
    vm.referrerPick = { label: 'Alex Advocate', value: 'c1' };
    vm.form.referee_phone = '   ';
    await vm.createReferral();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('offers only the statuses the router allows, never backwards', async () => {
    routeGet();
    const wrapper = mountView();
    await flushPromises();

    const vm = wrapper.vm;
    vm.openEdit(ROWS[0]);
    const offered = vm.editStatusOptions.map((o) => o.value);
    expect([...offered].sort()).toEqual(allowedStatuses());

    vm.openEdit({ ...ROWS[0], status: 'converted' });
    expect(vm.editStatusOptions.map((o) => o.value)).toEqual(['converted', 'rewarded']);

    const tabs = wrapper.find('[data-testid="referrals-tabs"]').text();
    expect(tabs).not.toMatch(/Contacted|Declined/);
    expect(tabs).toContain('Rewarded');
  });

  it('edits status only, through ReferralPatchIn', async () => {
    routeGet();
    mockPatch.mockResolvedValue({});
    const wrapper = mountView();
    await flushPromises();

    const vm = wrapper.vm;
    vm.openEdit(ROWS[0]);
    vm.editStatus = 'converted';
    await vm.saveStatus();
    await flushPromises();

    expect(mockPatch).toHaveBeenCalledWith('/api/referrals/r1', { status: 'converted' }, { successMessage: 'Referral updated' });
    const patchFields = modelFields('ReferralPatchIn').map((f) => f.name);
    expect(Object.keys(mockPatch.mock.calls[0][1])).toEqual(patchFields);
  });

  it('reads the list by the field names the API returns and names the referrer', async () => {
    routeGet();
    const wrapper = mountView();
    await flushPromises();

    expect(wrapper.text()).toContain('Pat Lee|pending');
    expect(wrapper.text()).toContain('Sam Roe|rewarded');
    // Names come with the list. A per-customer GET writes a data_accessed
    // audit row for every referrer, so the page must not make one.
    expect(mockGet.mock.calls.map((c) => c[0])).toEqual(['/api/referrals']);
    expect(wrapper.vm.counts).toMatchObject({ all: 2, pending: 1, rewarded: 1 });
  });

  it('every field the view reads is one the list returns', () => {
    const at = PY.indexOf('def _serialize_referral(');
    const fn = PY.slice(at, PY.indexOf('\ndef ', at + 1));
    for (const key of ['id', 'referrer_id', 'referee_name', 'referee_phone', 'status', 'reward_given', 'created_at']) {
      expect(fn, key).toContain(`"${key}":`);
    }
    const list = PY.slice(PY.indexOf('def list_referrals('), PY.indexOf('def create_referral('));
    expect(list).toContain('"referrer_name":');
  });
});
