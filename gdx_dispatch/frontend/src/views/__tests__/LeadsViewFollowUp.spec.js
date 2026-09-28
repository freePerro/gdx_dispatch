import { mount, flushPromises } from '@vue/test-utils';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setActivePinia, createPinia } from 'pinia';
import { createRouter, createMemoryHistory } from 'vue-router';
import PrimeVue from 'primevue/config';
import RealDatePicker from 'primevue/datepicker';
import LeadsView from '../LeadsView.vue';

// One shared spy, so a test can see which toast a view raised.
const { toastAdd } = vi.hoisted(() => ({ toastAdd: vi.fn() }));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: toastAdd }) }));
vi.mock('primevue/useconfirm', () => ({ useConfirm: () => ({ require: vi.fn() }) }));

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
const apiPut = vi.fn();
const apiDel = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({
    get: apiGet,
    post: apiPost,
    patch: apiPatch,
    put: apiPut,
    del: apiDel,
  }),
}));

const hasPermission = vi.fn(() => true);
vi.mock('../../stores/auth', () => ({
  useAuthStore: () => ({ hasPermission }),
}));

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Card: { template: '<div><slot name="title" /><slot /></div>' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<div><slot /></div>' },
  Column: {
    props: ['field', 'header'],
    template: '<div><slot name="body" :data="{}" /></div>',
  },
  Badge: { template: '<span />' },
  Tag: { template: '<span />' },
  ProgressSpinner: { template: '<div />' },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  InputNumber: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input type="number" :value="modelValue" @input="$emit(\'update:modelValue\', Number($event.target.value))" />',
  },
  Checkbox: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input type="checkbox" :checked="modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />',
  },
  DatePicker: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input class="datepicker-stub" :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Textarea: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<textarea :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
  },
  Select: {
    props: ['modelValue', 'options'],
    emits: ['update:modelValue', 'change'],
    template: `<select :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value); $emit(\'change\', $event.target.value)">
      <option v-for="opt in (options || [])" :key="opt.value || opt" :value="opt.value || opt">
        {{ opt.label || opt }}
      </option>
    </select>`,
  },
  DataTable: {
    props: ['value'],
    emits: ['row-click'],
    template: `<table><tbody>
        <tr v-for="(row, i) in (value || [])" :key="i" class="dt-row"
            @click="$emit('row-click', { data: row })">
          <td>{{ row.name }}</td>
          <td>{{ row.follow_up_date }}</td>
        </tr>
      </tbody><slot /></table>`,
  },
  Dialog: {
    props: ['visible'],
    template: `<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>`,
  },
  Button: {
    props: ['label', 'loading', 'disabled'],
    emits: ['click'],
    template: `<button :data-testid="$attrs['data-testid']" @click="$emit('click')">{{ label }}<slot /></button>`,
    inheritAttrs: false,
  },
};

const LEADS_DATA = [
  {
    id: 'lead-1',
    name: 'Alice Overdue',
    stage: 'new',
    follow_up_date: '2020-01-01',
    created_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'lead-2',
    name: 'Bob Future',
    stage: 'new',
    follow_up_date: '2099-01-01',
    created_at: '2026-01-02T00:00:00Z',
  },
];

function makeRouter(initialQuery = {}) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/leads', name: 'leads', component: LeadsView }],
  });
  router.push({ path: '/leads', query: initialQuery });
  return router;
}


// A server that keeps state: PATCH changes it, GET /api/leads reads it. The
// table re-reads after every inline save, so the tests need one to mean anything.
function statefulServer() {
  const db = new Map(LEADS_DATA.map((l) => [l.id, { ...l }]));
  const apply = (url, body) => Object.assign(db.get(url.split('/').pop()), body);
  apiGet.mockImplementation((url) => {
    if (url.startsWith('/api/leads/pipeline-summary')) return Promise.resolve({});
    if (url.startsWith('/api/leads/') && url.endsWith('/custom-fields')) return Promise.resolve([]);
    if (url.startsWith('/api/leads')) return Promise.resolve([...db.values()].map((l) => ({ ...l })));
    return Promise.resolve([]);
  });
  apiPatch.mockImplementation((url, body) => { apply(url, body); return Promise.resolve({}); });
  // Hold the next PATCH: it reaches the server only when released.
  const holdNextPatch = () => {
    let release;
    let fail;
    apiPatch.mockImplementationOnce((url, body) => new Promise((resolve, reject) => {
      release = () => { apply(url, body); resolve({}); };
      fail = () => reject(new Error('boom'));
    }));
    return { release: () => release(), fail: () => fail() };
  };
  return { db, holdNextPatch };
}

describe('LeadsView — Follow-up date, Due filter, Custom Fields, Start Estimate', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    apiGet.mockImplementation((url) => {
      if (url.startsWith('/api/leads/pipeline-summary')) {
        return Promise.resolve({ new: 2, contacted: 0, qualified: 0, quoted: 0, won: 0, lost: 0 });
      }
      if (url.startsWith('/api/landing-leads')) {
        return Promise.resolve([]);
      }
      if (url.startsWith('/api/leads/lead-1/custom-fields')) {
        // Stored as text server-side: "2", "true".
        return Promise.resolve([
          { field_key: 'door_count', label: 'Door count', field_type: 'number', value: '2' },
          { field_key: 'job_kind', label: 'Job kind', field_type: 'text', value: 'Repair' },
          { field_key: 'has_opener', label: 'Has opener', field_type: 'boolean', value: 'true' },
        ]);
      }
      if (url === '/api/leads/lead-9') {
        return Promise.resolve({ id: 'lead-9', name: 'Old Lead', stage: 'quoted' });
      }
      if (url.startsWith('/api/leads')) {
        return Promise.resolve(LEADS_DATA);
      }
      return Promise.resolve([]);
    });
  });

  it('loads leads with follow_up query parameter when route has follow_up query', async () => {
    const router = makeRouter({ follow_up: 'overdue' });
    await router.isReady();
    const w = mount(LeadsView, {
      global: { plugins: [router], stubs },
    });
    await flushPromises();

    const getLeadCalls = apiGet.mock.calls.filter(([url]) => url.startsWith('/api/leads'));
    expect(getLeadCalls.some(([url]) => url.includes('follow_up=overdue'))).toBe(true);
  });

  it('sorts overdue leads first in the datatable', async () => {
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, {
      global: { plugins: [router], stubs },
    });
    await flushPromises();

    const dtRows = w.findAll('.dt-row');
    expect(dtRows.length).toBe(2);
    // Overdue lead (Alice, 2020-01-01) should be first
    expect(dtRows[0].text()).toContain('Alice Overdue');
    expect(dtRows[1].text()).toContain('Bob Future');
  });

  it('loads and saves custom fields when editing a lead', async () => {
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, {
      global: { plugins: [router], stubs },
    });
    await flushPromises();

    // Click first row to open edit dialog
    const firstRow = w.find('.dt-row');
    await firstRow.trigger('click');
    await flushPromises();

    expect(apiGet).toHaveBeenCalledWith('/api/leads/lead-1/custom-fields');
    expect(w.find('[data-testid="lead-custom-fields-block"]').exists()).toBe(true);
    expect(w.find('[data-testid="custom-field-door_count"]').exists()).toBe(true);

    // Loaded answers come back typed, not as the stored text.
    expect(w.vm.customFieldOriginal).toEqual({ door_count: 2, job_kind: 'Repair', has_opener: true });
    // Change one answer and one lead field, then save.
    w.vm.customFieldValues.door_count = 3;
    w.vm.form.notes = 'wants a quote by Friday';
    apiPatch.mockResolvedValue({});
    apiPut.mockResolvedValue([]);
    const saveBtn = w.findAll('button').find((b) => b.text().includes('Save Lead'));
    expect(saveBtn).toBeDefined();
    await saveBtn.trigger('click');
    await flushPromises();

    // One "Lead updated", on the second write — not before it has landed.
    // Only the edited lead field is sent — nothing merely copied into the form.
    expect(apiPatch).toHaveBeenCalledWith(
      '/api/leads/lead-1',
      { notes: 'wants a quote by Friday' },
      {},
    );
    // Only the changed answer — the loaded ones are typed (2, true) and unchanged.
    expect(apiPut).toHaveBeenCalledWith(
      '/api/leads/lead-1/custom-fields',
      { values: { door_count: 3 } },
      { successMessage: 'Lead updated' },
    );
  });

  it('start-estimate in dialog calls POST /api/leads/:id/start-estimate and routes to /estimates/:id', async () => {
    const router = makeRouter();
    const pushSpy = vi.spyOn(router, 'push');
    await router.isReady();
    const w = mount(LeadsView, {
      global: { plugins: [router], stubs },
    });
    await flushPromises();

    // Click row
    await w.find('.dt-row').trigger('click');
    await flushPromises();

    apiPost.mockResolvedValue({
      estimate: { id: 'est-123' },
      customer: { status: 'matched', name: 'Alice Overdue' },
    });

    const startBtn = w.find('[data-testid="dialog-start-estimate"]');
    expect(startBtn.exists()).toBe(true);
    await startBtn.trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledWith('/api/leads/lead-1/start-estimate');
    expect(pushSpy).toHaveBeenCalledWith('/estimates/est-123');
  });

  it('?id= opens that lead (the estimate panel links here)', async () => {
    const router = makeRouter({ id: 'lead-2' });
    await router.isReady();
    const w = mount(LeadsView, { global: { plugins: [router], stubs } });
    await flushPromises();

    expect(w.find('.dlg').exists()).toBe(true);
    expect(apiGet).toHaveBeenCalledWith('/api/leads/lead-2/custom-fields');
  });

  it('?id= for a lead outside the loaded page fetches it directly', async () => {
    const router = makeRouter({ id: 'lead-9' });
    await router.isReady();
    const w = mount(LeadsView, { global: { plugins: [router], stubs } });
    await flushPromises();

    expect(apiGet).toHaveBeenCalledWith('/api/leads/lead-9');
    expect(w.find('.dlg').exists()).toBe(true);
    expect(apiGet).toHaveBeenCalledWith('/api/leads/lead-9/custom-fields');
  });

  it("one lead's intake answers never show on, or save onto, another lead", async () => {
    let releaseA;
    let releaseB;
    apiGet.mockImplementation((url) => {
      if (url === '/api/leads/lead-1/custom-fields') {
        return new Promise((r) => { releaseA = () => r([{ field_key: 'door_size', label: 'Door size', field_type: 'text', value: 'A-ANSWER' }]); });
      }
      if (url === '/api/leads/lead-2/custom-fields') {
        return new Promise((r) => { releaseB = () => r([{ field_key: 'door_size', label: 'Door size', field_type: 'text', value: 'B-ANSWER' }]); });
      }
      if (url.startsWith('/api/leads/pipeline-summary')) return Promise.resolve({});
      if (url.startsWith('/api/leads')) return Promise.resolve(LEADS_DATA);
      return Promise.resolve([]);
    });
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, { global: { plugins: [router], stubs } });
    await flushPromises();

    const [lead1, lead2] = ['lead-1', 'lead-2'].map((id) => w.vm.leads.find((l) => l.id === id));
    w.vm.openEdit(lead1);
    w.vm.openEdit(lead2); // lead-1's answers still in flight
    releaseA(); // ...and they arrive late
    await flushPromises();

    expect(w.vm.customFieldValues.door_size).toBeUndefined();
    expect(w.vm.customFieldsLoading).toBe(true); // Save stays disabled

    releaseB();
    await flushPromises();
    expect(w.vm.customFieldValues.door_size).toBe('B-ANSWER');
    expect(w.vm.customFieldsLoading).toBe(false);

    w.vm.customFieldValues.door_size = 'B-EDIT';
    apiPatch.mockResolvedValue({});
    apiPut.mockResolvedValue([]);
    await w.vm.saveLead();
    expect(apiPut).toHaveBeenCalledTimes(1);
    expect(apiPut.mock.calls[0][0]).toBe('/api/leads/lead-2/custom-fields');
    expect(apiPut.mock.calls[0][1]).toEqual({ values: { door_size: 'B-EDIT' } });
  });

  it('reopening the same lead keeps only the newest answers request (A, B, A)', async () => {
    const pending = [];
    apiGet.mockImplementation((url) => {
      if (url.endsWith('/custom-fields')) {
        return new Promise((r) => pending.push({ url, r }));
      }
      if (url.startsWith('/api/leads/pipeline-summary')) return Promise.resolve({});
      if (url.startsWith('/api/leads')) return Promise.resolve(LEADS_DATA);
      return Promise.resolve([]);
    });
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, { global: { plugins: [router], stubs } });
    await flushPromises();

    const [lead1, lead2] = ['lead-1', 'lead-2'].map((id) => w.vm.leads.find((l) => l.id === id));
    w.vm.openEdit(lead1);
    w.vm.openEdit(lead2);
    w.vm.openEdit(lead1);
    expect(pending.map((p) => p.url)).toEqual([
      '/api/leads/lead-1/custom-fields', '/api/leads/lead-2/custom-fields', '/api/leads/lead-1/custom-fields',
    ]);
    pending[0].r([{ field_key: 'door_size', field_type: 'text', value: 'STALE' }]);
    await flushPromises();
    expect(w.vm.customFieldsLoading).toBe(true);
    expect(w.vm.customFieldValues.door_size).toBeUndefined();

    pending[2].r([{ field_key: 'door_size', field_type: 'text', value: 'FRESH' }]);
    pending[1].r([{ field_key: 'door_size', field_type: 'text', value: 'B' }]);
    await flushPromises();
    expect(w.vm.customFieldsLoading).toBe(false);
    expect(w.vm.customFieldValues.door_size).toBe('FRESH');
  });

  it('an unanswered yes/no is never saved as "No", and untouched answers are not rewritten', async () => {
    apiGet.mockImplementation((url) => {
      if (url === '/api/leads/lead-1/custom-fields') {
        return Promise.resolve([
          { field_key: 'has_opener', field_type: 'boolean', value: null },
          { field_key: 'door_count', field_type: 'number', value: '2' },
        ]);
      }
      if (url.startsWith('/api/leads/pipeline-summary')) return Promise.resolve({});
      if (url.startsWith('/api/leads')) return Promise.resolve(LEADS_DATA);
      return Promise.resolve([]);
    });
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, { global: { plugins: [router], stubs } });
    await flushPromises();
    await w.find('.dt-row').trigger('click');
    await flushPromises();

    apiPatch.mockResolvedValue({});
    await w.vm.saveLead();
    // Nothing changed, so nothing is written — no PUT, no PATCH, no audit row.
    expect(apiPut).not.toHaveBeenCalled();
    expect(apiPatch).not.toHaveBeenCalled();
  });

  it('if the answers fail to save after the lead did, the table refreshes and the dialog stays open', async () => {
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, { global: { plugins: [router], stubs } });
    await flushPromises();
    await w.find('.dt-row').trigger('click');
    await flushPromises();

    w.vm.customFieldValues.door_count = 5;
    apiPatch.mockResolvedValue({});
    apiPut.mockRejectedValueOnce(new Error('422'));
    const listCallsBefore = apiGet.mock.calls.filter(([u]) => u === '/api/leads').length;
    await w.vm.saveLead();
    await flushPromises();

    expect(apiGet.mock.calls.filter(([u]) => u === '/api/leads').length).toBe(listCallsBefore + 1);
    expect(w.find('.dlg').exists()).toBe(true);
    expect(w.vm.saving).toBe(false);
  });

  it('"today" is the local calendar day, not the UTC one', async () => {
    // 10:30pm Central on Sep 28 is already Sep 29 in UTC.
    const prevTz = process.env.TZ;
    process.env.TZ = 'America/Chicago';
    vi.useFakeTimers({ toFake: ['Date'] });
    vi.setSystemTime(new Date('2026-09-29T03:30:00Z'));
    try {
      const router = makeRouter();
      await router.isReady();
      const w = mount(LeadsView, { global: { plugins: [router], stubs } });
      await flushPromises();

      expect(w.vm.isDueToday('2026-09-28')).toBe(true);
      expect(w.vm.isOverdue('2026-09-28', 'New')).toBe(false);
      expect(w.vm.isOverdue('2026-09-27', 'New')).toBe(true);
    } finally {
      vi.useRealTimers();
      if (prevTz === undefined) delete process.env.TZ;
      else process.env.TZ = prevTz;
    }
  });

  it('the real inline call-back picker takes no typing (pick-only)', async () => {
    // The real PrimeVue 4.5.5 picker reports a date on every keystroke that
    // parses ("2026-10-1" is Oct 1), and each report was a PATCH + audit row.
    // manualInput=false renders the input readonly, so a browser delivers no
    // keystrokes at all. jsdom can't prove that part — a scripted input event
    // ignores readonly — so this asserts the attribute on the real component.
    const realCellStubs = {
      ...stubs,
      DatePicker: RealDatePicker,
      DataTable: {
        props: ['value'],
        provide() { return { stubRow: () => (this.value || [])[0] }; },
        template: '<table><tbody><tr v-if="(value || []).length"><slot /></tr></tbody></table>',
      },
      Column: { inject: ['stubRow'], template: '<td><slot name="body" :data="stubRow()" /></td>' },
    };
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, { attachTo: document.body, global: { plugins: [router, PrimeVue], stubs: realCellStubs } });
    await flushPromises();

    const input = w.find('[data-testid="inline-follow-up-date"] input');
    expect(input.exists()).toBe(true);
    expect(input.attributes('readonly')).toBeDefined();

    // Picking through the real component: the shown day again writes nothing;
    // a different day writes once. (First row is lead-1, due 2020-01-01.)
    const picker = w.findComponent(RealDatePicker);
    const day = (d, m, y) => ({ day: d, month: m, year: y, selectable: true, otherMonth: false, today: false });
    apiPatch.mockResolvedValue({});
    picker.vm.onDateSelect(null, day(1, 0, 2020));
    picker.vm.onDateSelect(null, day(1, 0, 2020));
    await flushPromises();
    expect(apiPatch).not.toHaveBeenCalled();
    picker.vm.onDateSelect(null, day(2, 0, 2020));
    await flushPromises();
    expect(apiPatch).toHaveBeenCalledTimes(1);
    expect(apiPatch).toHaveBeenCalledWith('/api/leads/lead-1', { follow_up_date: '2020-01-02' }, expect.anything());
    w.unmount();
  });

  it('a text answer typed and emptied again is still unanswered — no write', async () => {
    apiGet.mockImplementation((url) => {
      if (url === '/api/leads/lead-1/custom-fields') {
        return Promise.resolve([{ field_key: 'notes_extra', field_type: 'text', value: null }]);
      }
      if (url.startsWith('/api/leads/pipeline-summary')) return Promise.resolve({});
      if (url.startsWith('/api/leads')) return Promise.resolve(LEADS_DATA);
      return Promise.resolve([]);
    });
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, { global: { plugins: [router], stubs } });
    await flushPromises();
    await w.find('.dt-row').trigger('click');
    await flushPromises();

    w.vm.customFieldValues.notes_extra = '';
    apiPatch.mockResolvedValue({});
    await w.vm.saveLead();
    expect(apiPut).not.toHaveBeenCalled();
  });

  it('flipping the due filter fast shows the rows of the filter now selected', async () => {
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, { global: { plugins: [router], stubs } });
    await flushPromises();

    let releaseOverdue;
    apiGet.mockImplementation((url) => {
      if (url === '/api/leads?follow_up=overdue') return new Promise((r) => { releaseOverdue = () => r([LEADS_DATA[0]]); });
      if (url === '/api/leads?follow_up=today') return Promise.resolve([LEADS_DATA[1]]);
      return Promise.resolve([]);
    });
    w.vm.dueFilter = 'overdue';
    const slow = w.vm.loadLeads();
    w.vm.dueFilter = 'today';
    await w.vm.loadLeads();
    releaseOverdue();
    await slow;

    expect(w.vm.leads.map((l) => l.id)).toEqual(['lead-2']);
    expect(w.vm.loading).toBe(false);
  });

  describe('inline call-back date', () => {
    async function mountWithServer() {
      const server = statefulServer();
      const router = makeRouter();
      await router.isReady();
      const w = mount(LeadsView, { global: { plugins: [router], stubs } });
      await flushPromises();
      const row = (id) => w.vm.leads.find((l) => l.id === id);
      return { w, server, row };
    }

    it('a saved pick shows what the server holds', async () => {
      const { w, server, row } = await mountWithServer();
      await w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      expect(server.db.get('lead-2').follow_up_date).toBe('2030-01-01');
      expect(row('lead-2').follow_up_date).toBe('2030-01-01');
      expect(w.vm.loading).toBe(false); // refreshed in place, no spinner swap
    });

    it('a failed pick shows the date the server kept', async () => {
      const { w, server, row } = await mountWithServer();
      const held = server.holdNextPatch();
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      await flushPromises();
      held.fail();
      await p;
      expect(row('lead-2').follow_up_date).toBe('2099-01-01');
    });

    it('one save per lead: the picker is disabled and a second pick is ignored while one is in flight', async () => {
      const { w, server, row } = await mountWithServer();
      const held = server.holdNextPatch();
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      await flushPromises();
      expect(w.vm.followUpSaving).toContain('lead-2');
      await w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2031, 0, 1));
      expect(apiPatch).toHaveBeenCalledTimes(1);
      held.release();
      await p;
      expect(w.vm.followUpSaving).not.toContain('lead-2');
      expect(server.db.get('lead-2').follow_up_date).toBe('2030-01-01');
    });

    it('a list read before the save landed is superseded (save answers first)', async () => {
      const { w, server, row } = await mountWithServer();
      const held = server.holdNextPatch();
      let releaseList;
      const staleList = [...server.db.values()].map((l) => ({ ...l })); // read now: 2099
      apiGet.mockImplementationOnce(() => new Promise((r) => { releaseList = () => r(staleList); }));
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      const reload = w.vm.loadLeads(); // e.g. a filter change, sent mid-save
      await flushPromises();
      held.release();
      await p;
      releaseList();
      await reload;
      expect(row('lead-2').follow_up_date).toBe('2030-01-01');
      // Re-picking the date the user chose still writes nothing.
      await w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      expect(apiPatch).toHaveBeenCalledTimes(1);
    });

    it('a list read before the save landed is superseded (list answers first)', async () => {
      const { w, server, row } = await mountWithServer();
      const held = server.holdNextPatch();
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      await w.vm.loadLeads(); // answers before the PATCH lands
      held.release();
      await p;
      expect(row('lead-2').follow_up_date).toBe('2030-01-01');
    });

    it('the edit dialog cannot save that lead while its inline save is in flight', async () => {
      const { w, server, row } = await mountWithServer();
      const held = server.holdNextPatch();
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      await flushPromises();
      w.vm.openEdit(row('lead-2'));
      await flushPromises();
      w.vm.form.follow_up_date = '2031-05-05';
      await w.vm.saveLead();
      expect(apiPatch).toHaveBeenCalledTimes(1); // only the inline one
      held.release();
      await p;
      // Afterwards the dialog saves normally, and its date is what stands.
      await w.vm.saveLead();
      expect(server.db.get('lead-2').follow_up_date).toBe('2031-05-05');
    });

    it('a notes-only dialog save after a failed inline pick sends only the notes', async () => {
      const { w, server, row } = await mountWithServer();
      // The dialog is modal, so the real order is: pick in the table, then open.
      const held = server.holdNextPatch();
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      await flushPromises();
      w.vm.openEdit(row('lead-2')); // copies the unconfirmed 2030
      await flushPromises();
      held.fail();
      await p;
      expect(w.vm.form.follow_up_date).toBe('2099-01-01');
      w.vm.form.notes = 'called, no answer';
      await w.vm.saveLead();
      expect(apiPatch).toHaveBeenLastCalledWith('/api/leads/lead-2', { notes: 'called, no answer' }, expect.anything());
      expect(server.db.get('lead-2').follow_up_date).toBe('2099-01-01');
    });

    it('under a due filter, a row whose new date leaves the filter leaves the table', async () => {
      const { w, server, row } = await mountWithServer();
      apiGet.mockImplementation((url) => {
        if (url === '/api/leads?follow_up=overdue') {
          return Promise.resolve([...server.db.values()].filter((l) => l.follow_up_date < '2026-01-01').map((l) => ({ ...l })));
        }
        if (url.startsWith('/api/leads/pipeline-summary')) return Promise.resolve({});
        return Promise.resolve([]);
      });
      w.vm.dueFilter = 'overdue';
      await w.vm.loadLeads();
      expect(w.vm.leads.map((l) => l.id)).toEqual(['lead-1']);
      await w.vm.updateLeadFollowUpDate(row('lead-1'), new Date(2030, 0, 1));
      expect(w.vm.leads.map((l) => l.id)).toEqual([]);
    });

    it('the spinner never sticks when the re-read supersedes a normal load', async () => {
      const { w, server, row } = await mountWithServer();
      const held = server.holdNextPatch();
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      await flushPromises();
      let releaseList;
      const list = [...server.db.values()].map((l) => ({ ...l }));
      apiGet.mockImplementationOnce(() => new Promise((r) => { releaseList = () => r(list); }));
      const normal = w.vm.loadLeads(); // e.g. the Due filter changed: spinner on
      expect(w.vm.loading).toBe(true);
      held.release();
      await p; // the quiet re-read is now the newest request
      releaseList();
      await normal;
      expect(w.vm.loading).toBe(false);
    });

    it('if the save and the re-read both fail, the row shows the date the server kept and nothing throws', async () => {
      const { w, server, row } = await mountWithServer();
      const held = server.holdNextPatch();
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 5));
      await flushPromises();
      apiGet.mockImplementationOnce(() => Promise.reject(new Error('net down')));
      held.fail();
      await expect(p).resolves.toBeUndefined();
      expect(row('lead-2').follow_up_date).toBe('2099-01-01');
    });

    it('the real picker in the cell is disabled while its save is in flight', async () => {
      const server = statefulServer();
      const realCellStubs = {
        ...stubs,
        DatePicker: RealDatePicker,
        DataTable: {
          props: ['value'],
          provide() { return { stubRow: () => (this.value || [])[0] }; },
          template: '<table><tbody><tr v-if="(value || []).length"><slot /></tr></tbody></table>',
        },
        Column: { inject: ['stubRow'], template: '<td><slot name="body" :data="stubRow()" /></td>' },
      };
      const router = makeRouter();
      await router.isReady();
      const w = mount(LeadsView, { attachTo: document.body, global: { plugins: [router, PrimeVue], stubs: realCellStubs } });
      await flushPromises();
      const picker = () => w.findComponent(RealDatePicker);
      const day = (d, m, y) => ({ day: d, month: m, year: y, selectable: true, otherMonth: false, today: false });

      const held = server.holdNextPatch();
      picker().vm.onDateSelect(null, day(2, 0, 2020)); // first row: lead-1
      await flushPromises();
      expect(picker().props('disabled')).toBe(true);
      picker().vm.onDateSelect(null, day(3, 0, 2020)); // ignored by the real component
      await flushPromises();
      expect(apiPatch).toHaveBeenCalledTimes(1);
      held.release();
      await flushPromises();
      expect(picker().props('disabled')).toBe(false);
      expect(server.db.get('lead-1').follow_up_date).toBe('2020-01-02');
      w.unmount();
    });

    it("a superseded re-read never puts a rejected pick back into the dialog (two leads, A fails, B lands)", async () => {
      const { w, server, row } = await mountWithServer();
      const heldA = server.holdNextPatch();
      const pA = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      await flushPromises();
      const heldB = server.holdNextPatch();
      const pB = w.vm.updateLeadFollowUpDate(row('lead-1'), new Date(2020, 0, 2));
      await flushPromises();
      w.vm.openEdit(row('lead-2')); // the form copies the unconfirmed 2030 pick
      await flushPromises();

      // Hold the next two list reads in order: A's re-read, then B's.
      const reads = [];
      const readNow = () => [...server.db.values()].map((l) => ({ ...l }));
      apiGet
        .mockImplementationOnce(() => new Promise((r) => reads.push(() => r(readNow()))))
        .mockImplementationOnce(() => new Promise((r) => reads.push(() => r(readNow()))));
      heldA.fail();
      await flushPromises(); // A's re-read is in flight
      heldB.release();
      await flushPromises(); // B's re-read is in flight, newer: A's will be superseded
      reads[0]();
      await pA;
      expect(w.vm.form.follow_up_date).toBe('2099-01-01');
      expect(row('lead-2').follow_up_date).toBe('2099-01-01');
      reads[1]();
      await pB;

      w.vm.form.notes = 'called, no answer';
      await w.vm.saveLead();
      expect(server.db.get('lead-2').follow_up_date).toBe('2099-01-01');
    });

    it('a rejected pick whose re-read was superseded by a read that then fails is not left on screen', async () => {
      const { w, server, row } = await mountWithServer();
      const held = server.holdNextPatch();
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      await flushPromises();
      const reads = [];
      apiGet
        .mockImplementationOnce(() => new Promise((r) => reads.push(() => r([...server.db.values()].map((l) => ({ ...l }))))))
        .mockImplementationOnce(() => new Promise((_, rej) => reads.push(() => rej(new Error('net down')))));
      held.fail();
      await flushPromises(); // this save's re-read is in flight
      const newer = w.vm.loadLeads().catch(() => {}); // a newer read, which will fail
      reads[0]();
      await p;
      reads[1]();
      await newer;
      expect(row('lead-2').follow_up_date).toBe('2099-01-01');
      expect(w.vm.loading).toBe(false);
    });

    it('the same lead stays locked until its re-read settles; a newer landed read is never overwritten', async () => {
      const { w, server, row } = await mountWithServer();
      const reads = [];
      apiGet.mockImplementationOnce(() => new Promise((r) => reads.push(() => r([...server.db.values()].map((l) => ({ ...l }))))));
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      await flushPromises(); // PATCH landed; the re-read (R1) is held
      expect(server.db.get('lead-2').follow_up_date).toBe('2030-01-01');
      expect(w.vm.followUpSaving).toContain('lead-2');

      // Re-pick and dialog save are both refused while R1 is out.
      await w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2031, 0, 1));
      w.vm.openEdit(row('lead-2'));
      w.vm.form.follow_up_date = '2032-01-01';
      await w.vm.saveLead();
      expect(apiPatch).toHaveBeenCalledTimes(1);

      // A newer read lands first (e.g. a filter change), then R1 answers late.
      server.db.get('lead-2').follow_up_date = '2033-03-03'; // changed elsewhere
      await w.vm.loadLeads();
      reads[0]();
      await p;
      expect(row('lead-2').follow_up_date).toBe('2033-03-03');
      expect(w.vm.followUpSaving).not.toContain('lead-2');
    });

    it('a save that landed but whose response was lost is not undone by a newer landed read', async () => {
      const { w, server, row } = await mountWithServer();
      // The PATCH reaches the server, then the client sees an error (a 502).
      apiPatch.mockImplementationOnce((url, body) => {
        Object.assign(server.db.get('lead-2'), body);
        return Promise.reject(new Error('502'));
      });
      w.vm.openEdit(row('lead-2'));
      await flushPromises(); // its answers GET is done before the read is held
      const reads = [];
      apiGet.mockImplementationOnce(() => new Promise((r) => reads.push(() => r([...server.db.values()].map((l) => ({ ...l }))))));
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      await flushPromises(); // this save's re-read is held
      await w.vm.loadLeads(); // a newer read lands first
      reads[0]();
      await p;
      expect(server.db.get('lead-2').follow_up_date).toBe('2030-01-01');
      expect(row('lead-2').follow_up_date).toBe('2030-01-01');
      expect(w.vm.form.follow_up_date).toBe('2030-01-01');
    });

    it('a read sent before the save settled is never trusted over it (own re-read fails)', async () => {
      const { w, server, row } = await mountWithServer();
      w.vm.openEdit(row('lead-2'));
      await flushPromises();
      const held = server.holdNextPatch();
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      w.vm.form.follow_up_date = '2030-01-01';
      await flushPromises();
      await w.vm.loadLeads(); // sent and answered while the PATCH is held: 2099
      apiGet.mockImplementationOnce(() => Promise.reject(new Error('net down')));
      held.release();
      await p; // server now 2030; this save's re-read failed
      expect(row('lead-2').follow_up_date).toBe('2030-01-01');
      expect(w.vm.form.follow_up_date).toBe('2030-01-01');
      w.vm.form.notes = 'called';
      await w.vm.saveLead();
      expect(server.db.get('lead-2').follow_up_date).toBe('2030-01-01');
    });

    it("another lead's re-read, sent mid-save, is not trusted over this save", async () => {
      const { w, server, row } = await mountWithServer();
      const heldB = server.holdNextPatch();
      const pB = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1)); // B
      await flushPromises();
      const pA = w.vm.updateLeadFollowUpDate(row('lead-1'), new Date(2020, 0, 2)); // A lands, re-reads: B still 2099
      await pA;
      apiGet.mockImplementationOnce(() => Promise.reject(new Error('net down')));
      heldB.release();
      await pB;
      expect(server.db.get('lead-2').follow_up_date).toBe('2030-01-01');
      expect(row('lead-2').follow_up_date).toBe('2030-01-01');
    });

    it('a dialog opened mid-save from a row read before the save landed never undoes it', async () => {
      const { w, server, row } = await mountWithServer();
      const heldB = server.holdNextPatch();
      const pB = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      await flushPromises();
      await w.vm.updateLeadFollowUpDate(row('lead-1'), new Date(2020, 0, 2)); // its re-read shows lead-2 at 2099
      w.vm.openEdit(row('lead-2')); // the form copies the stale 2099
      await flushPromises();
      expect(w.vm.form.follow_up_date).toBe('2099-01-01');
      heldB.release();
      await pB; // B's own re-read succeeds: 2030
      expect(w.vm.form.follow_up_date).toBe('2030-01-01'); // untouched date follows the server
      w.vm.form.notes = 'called';
      await w.vm.saveLead();
      expect(apiPatch).toHaveBeenLastCalledWith('/api/leads/lead-2', { notes: 'called' }, expect.anything());
      expect(server.db.get('lead-2').follow_up_date).toBe('2030-01-01');
    });

    it('a date the user edited in the dialog is left alone when an inline save settles, and is what saves', async () => {
      const { w, server, row } = await mountWithServer();
      const held = server.holdNextPatch();
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      await flushPromises();
      w.vm.openEdit(row('lead-2'));
      await flushPromises();
      w.vm.form.follow_up_date = '2031-05-05'; // typed by the user
      held.release();
      await p;
      expect(w.vm.form.follow_up_date).toBe('2031-05-05');
      await w.vm.saveLead();
      expect(server.db.get('lead-2').follow_up_date).toBe('2031-05-05');
    });

    it('a failed pick on a lead filtered out of the table leaves the dialog on the server date', async () => {
      const { w, server, row } = await mountWithServer();
      const held = server.holdNextPatch();
      const p = w.vm.updateLeadFollowUpDate(row('lead-2'), new Date(2030, 0, 1));
      await flushPromises();
      w.vm.openEdit(row('lead-2'));
      await flushPromises();
      apiGet.mockImplementation((url) => {
        if (url === '/api/leads?follow_up=overdue') return Promise.resolve([{ ...server.db.get('lead-1') }]);
        if (url.startsWith('/api/leads/pipeline-summary')) return Promise.resolve({});
        return Promise.resolve([]);
      });
      w.vm.dueFilter = 'overdue'; // lead-2 leaves the table
      held.fail();
      await p;
      expect(w.vm.leads.find((l) => l.id === 'lead-2')).toBeUndefined();
      expect(w.vm.form.follow_up_date).toBe('2099-01-01');
      expect(w.vm.formOriginal.follow_up_date).toBe('2099-01-01');
    });
  });

  it('clearing Estimated Value alone reports that it cannot, instead of a false "Lead updated"', async () => {
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, { global: { plugins: [router], stubs } });
    await flushPromises();
    w.vm.openEdit({ ...LEADS_DATA[1], stage: 'New', estimated_value: 1200 });
    await flushPromises();
    w.vm.form.estimated_value = '';
    await w.vm.saveLead();
    expect(apiPatch).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'warn', summary: 'Estimated value not cleared' }));
    expect(w.find('.dlg').exists()).toBe(true); // stays open so a 0 can be entered
  });

  it('Estimated Value accepts "$2,500" and refuses text it cannot read, instead of a false save', async () => {
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, { global: { plugins: [router], stubs } });
    await flushPromises();
    apiPatch.mockResolvedValue({});

    w.vm.openEdit({ ...LEADS_DATA[1], stage: 'New', estimated_value: 1200 });
    await flushPromises();
    w.vm.form.estimated_value = ' $ 2,500.50 ';
    await w.vm.saveLead();
    expect(apiPatch).toHaveBeenCalledWith('/api/leads/lead-2', { estimated_value: 2500.5 }, expect.anything());

    apiPatch.mockClear();
    w.vm.openEdit({ ...LEADS_DATA[1], stage: 'New', estimated_value: 1200 });
    await flushPromises();
    for (const bad of ['about 2k', '1e999', 'Infinity', '0x10']) {
      toastAdd.mockClear();
      w.vm.form.estimated_value = bad;
      await w.vm.saveLead();
      expect(apiPatch).not.toHaveBeenCalled();
      expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'warn', summary: 'Estimated value is not a number' }));
      expect(w.find('.dlg').exists()).toBe(true);
    }

    // A comma only as a thousands separator: "2,50" is a typo, not 250.
    for (const bad of ['2,50', '1,5', '12,34', '2 50', '12 34', '25$00', '$$5']) {
      w.vm.form.estimated_value = bad;
      await w.vm.saveLead();
      expect(apiPatch).not.toHaveBeenCalled();
    }

    // Backspaced down to "$" is blank, not 0.
    toastAdd.mockClear();
    w.vm.form.estimated_value = '$';
    await w.vm.saveLead();
    expect(apiPatch).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ severity: 'warn', summary: 'Estimated value not cleared' }));
  });

  it('a new lead with an unreadable Estimated Value is refused before it is created', async () => {
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, { global: { plugins: [router], stubs } });
    await flushPromises();
    w.vm.openCreate();
    w.vm.form.name = 'New Person';
    w.vm.form.estimated_value = '1e999';
    await w.vm.saveLead();
    expect(apiPost).not.toHaveBeenCalled();
    expect(toastAdd).toHaveBeenCalledWith(expect.objectContaining({ summary: 'Estimated value is not a number' }));
  });

  it('a retry after a failed answers save does not resend lead fields that already landed', async () => {
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, { global: { plugins: [router], stubs } });
    await flushPromises();
    await w.find('.dt-row').trigger('click');
    await flushPromises();
    w.vm.form.notes = 'x';
    w.vm.customFieldValues.door_count = 5;
    apiPatch.mockResolvedValue({});
    apiPut.mockRejectedValueOnce(new Error('422')).mockResolvedValueOnce([]);
    await w.vm.saveLead();
    await w.vm.saveLead();
    expect(apiPatch).toHaveBeenCalledTimes(1);
    expect(apiPut).toHaveBeenCalledTimes(2);
  });

  it('blanking an Estimated Value that is already 0 is no change: no warning, the dialog just closes', async () => {
    const router = makeRouter();
    await router.isReady();
    const w = mount(LeadsView, { global: { plugins: [router], stubs } });
    await flushPromises();
    w.vm.openEdit({ ...LEADS_DATA[1], stage: 'New', estimated_value: 0 });
    await flushPromises();
    w.vm.form.estimated_value = '';
    await w.vm.saveLead();
    expect(apiPatch).not.toHaveBeenCalled();
    expect(toastAdd).not.toHaveBeenCalledWith(expect.objectContaining({ summary: 'Estimated value not cleared' }));
    expect(w.find('.dlg').exists()).toBe(false);
  });
});
