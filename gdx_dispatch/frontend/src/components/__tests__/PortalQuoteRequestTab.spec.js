/**
 * Customer portal — "Request a quote" (Doug 2026-10-07: a form to request a
 * quote for new doors, tracked by job name, any number of doors; the
 * precursor to letting AI build the estimate).
 *
 * Pinned:
 *  1. Nothing is sent without a job name and a size on every door.
 *  2. The request is posted as JSON with the doors only — no form-side keys —
 *     and photos go up AFTER it, one per call, against the right door index.
 *  3. A photo that fails does not lose the request: it is listed with a
 *     Retry, and a successful retry clears it.
 *  4. "Your requests" shows each request's status, and a door already at the
 *     4-photo cap offers no Add photos.
 *  5. Add photos on a sent request uploads straight away.
 *  6. Change request PATCHes the doors with where each one sat before, and a
 *     photo added while changing goes to the door's new position.
 *  7. Withdraw posts once confirmed; the list shows when a request was
 *     withdrawn or changed, and offers neither action once it can't be taken.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import PortalQuoteRequestTab from '../PortalQuoteRequestTab.vue';

// The dialog itself is only proven in a browser; here the spec decides.
let confirmAnswer = true;
const confirmDestructive = vi.fn((opts) => { if (confirmAnswer) opts.accept(); });
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmDestructive, confirmAsync: vi.fn() }),
}));

const vModel = (tag = 'input') => ({
  props: ['modelValue'],
  emits: ['update:modelValue'],
  inheritAttrs: false,
  template: `<${tag} :data-testid="$attrs['data-testid']" :value="modelValue ?? ''" @input="$emit('update:modelValue', $event.target.value)" />`,
});

const stubs = {
  ConfirmDialog: true,
  Button: {
    props: ['label', 'loading'],
    emits: ['click'],
    inheritAttrs: false,
    template: '<button :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')">{{ label }}</button>',
  },
  Card: { template: '<div><slot name="title" /><slot name="subtitle" /><slot name="content" /></div>' },
  Message: { inheritAttrs: false, template: '<div :data-testid="$attrs[\'data-testid\']"><slot /></div>' },
  Tag: { props: ['value'], inheritAttrs: false, template: '<span :data-testid="$attrs[\'data-testid\']">{{ value }}</span>' },
  InputText: vModel(),
  Textarea: vModel('textarea'),
  Select: vModel(),
  InputNumber: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    inheritAttrs: false,
    template: `<input :data-testid="$attrs['data-testid']" :value="modelValue ?? ''" @input="$emit('update:modelValue', $event.target.value === '' ? null : Number($event.target.value))" />`,
  },
};

function photo(name = 'door.jpg') {
  return new File(['x'], name, { type: 'image/jpeg' });
}

async function pickFiles(w, files) {
  const input = w.get('[data-testid="qr-file-input"]');
  Object.defineProperty(input.element, 'files', { value: files, configurable: true });
  await input.trigger('change');
  await flushPromises();
}

function makeFetcher({ list = [], uploadFails = 0 } = {}) {
  let failuresLeft = uploadFails;
  return vi.fn(async (path, opts = {}) => {
    if (path === '/portal/quote-requests' && opts.method === 'POST') {
      const body = JSON.parse(opts.body);
      return { id: 'req-1', job_name: body.job_name, doors: [], status: 'Received' };
    }
    if (path === '/portal/quote-requests') return list;
    if (opts.method === 'PATCH') {
      const body = JSON.parse(opts.body);
      return { id: path.split('/').pop(), job_name: body.job_name, doors: [], status: 'Received' };
    }
    if (path.endsWith('/withdraw')) return { id: 'r1', status: 'Withdrawn' };
    if (path.endsWith('/photos')) {
      if (failuresLeft > 0) {
        failuresLeft -= 1;
        throw new Error('Use a JPEG, PNG or WebP photo');
      }
      return { id: 'ph-1' };
    }
    throw new Error(`unexpected ${path}`);
  });
}

async function mountTab(fetcher) {
  const w = mount(PortalQuoteRequestTab, { props: { fetcher }, global: { stubs } });
  await flushPromises();
  return w;
}

async function fillDoor(w, i, { wf = '16', hf = '7' } = {}) {
  await w.get(`[data-testid="qr-door-width-ft-${i}"]`).setValue(wf);
  await w.get(`[data-testid="qr-door-height-ft-${i}"]`).setValue(hf);
}

beforeEach(() => {
  vi.clearAllMocks();
  confirmAnswer = true;
});

describe('portal quote request tab', () => {
  it('sends nothing without a job name and a size on every door', async () => {
    const fetcher = makeFetcher();
    const w = await mountTab(fetcher);
    await w.get('[data-testid="qr-submit"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="qr-job-name-error"]').exists()).toBe(true);
    expect(w.find('[data-testid="qr-door-error-0"]').exists()).toBe(true);
    expect(fetcher.mock.calls.filter(([, o]) => o?.method === 'POST')).toHaveLength(0);
  });

  it('posts the doors as JSON, then uploads each photo against its door', async () => {
    const fetcher = makeFetcher();
    const w = await mountTab(fetcher);
    await w.get('[data-testid="qr-job-name"]').setValue('  Lake cabin  ');
    await fillDoor(w, 0);
    await w.get('[data-testid="qr-add-door"]').trigger('click');
    await fillDoor(w, 1, { wf: '9', hf: '8' });
    await w.get('[data-testid="qr-door-qty-1"]').setValue('2');
    await w.get('[data-testid="qr-door-photos-1"]').trigger('click');
    await pickFiles(w, [photo('a.jpg')]);
    expect(w.find('[data-testid="qr-door-file-1-0"]').text()).toContain('a.jpg');

    await w.get('[data-testid="qr-submit"]').trigger('click');
    await flushPromises();

    const post = fetcher.mock.calls.find(([p, o]) => p === '/portal/quote-requests' && o?.method === 'POST');
    const body = JSON.parse(post[1].body);
    expect(body.job_name).toBe('Lake cabin');
    expect(body.doors).toHaveLength(2);
    expect(body.doors[1]).toMatchObject({ width_ft: 9, height_ft: 8, quantity: 2, width_in: 0, height_in: 0 });
    for (const d of body.doors) {
      expect(d).not.toHaveProperty('_key');
      expect(d).not.toHaveProperty('_files');
      expect(d).not.toHaveProperty('source_index');
    }

    const uploads = fetcher.mock.calls.filter(([p]) => p === '/portal/quote-requests/req-1/photos');
    expect(uploads).toHaveLength(1);
    const form = uploads[0][1].body;
    expect(form.get('door_index')).toBe('1');
    expect(form.get('file').name).toBe('a.jpg');

    expect(w.get('[data-testid="qr-sent"]').text()).toContain('Lake cabin');
    // The form is cleared for the next request.
    expect(w.findAll('[data-testid^="qr-door-width-ft-"]')).toHaveLength(1);
  });

  it('keeps the request when a photo fails, and lets the customer retry it', async () => {
    const fetcher = makeFetcher({ uploadFails: 1 });
    const w = await mountTab(fetcher);
    await w.get('[data-testid="qr-job-name"]').setValue('Garage');
    await fillDoor(w, 0);
    await w.get('[data-testid="qr-door-photos-0"]').trigger('click');
    await pickFiles(w, [photo('b.heic')]);
    await w.get('[data-testid="qr-submit"]').trigger('click');
    await flushPromises();

    expect(w.get('[data-testid="qr-sent"]').exists()).toBe(true);
    const failures = w.get('[data-testid="qr-upload-failures"]');
    expect(failures.text()).toContain('b.heic');
    expect(failures.text()).toContain('Use a JPEG, PNG or WebP photo');

    await failures.findAll('button').find((b) => b.text() === 'Retry').trigger('click');
    await flushPromises();
    expect(w.find('[data-testid="qr-upload-failures"]').exists()).toBe(false);
  });

  it('asks how many rather than sending a blank quantity as 1', async () => {
    const fetcher = makeFetcher();
    const w = await mountTab(fetcher);
    await w.get('[data-testid="qr-job-name"]').setValue('Garage');
    await fillDoor(w, 0);
    await w.get('[data-testid="qr-door-qty-0"]').setValue('');
    await w.get('[data-testid="qr-submit"]').trigger('click');
    await flushPromises();
    expect(w.get('[data-testid="qr-door-error-0"]').text()).toContain('how many');
    expect(fetcher.mock.calls.filter(([, o]) => o?.method === 'POST')).toHaveLength(0);
  });

  it('caps a request at 10 doors', async () => {
    const w = await mountTab(makeFetcher());
    for (let i = 0; i < 12; i += 1) {
      const add = w.find('[data-testid="qr-add-door"]');
      if (add.exists()) await add.trigger('click');
    }
    expect(w.findAll('[data-testid^="qr-door-width-ft-"]')).toHaveLength(10);
    expect(w.find('[data-testid="qr-add-door"]').exists()).toBe(false);
  });

  it('lists sent requests with their status; a full door offers no Add photos', async () => {
    const list = [{
      id: 'r1',
      job_name: 'Cabin',
      status: 'Being priced',
      created_at: '2026-10-07T12:00:00Z',
      site_address: null,
      doors: [
        { index: 0, quantity: 2, size_label: '16\' 0" x 7\' 0"', material: 'steel', photo_ids: ['a', 'b', 'c', 'd'] },
        { index: 1, quantity: 1, size_label: '9\' 0" x 8\' 0"', material: 'not_sure', photo_ids: [] },
      ],
    }];
    const w = await mountTab(makeFetcher({ list }));
    expect(w.get('[data-testid="qr-request-status-r1"]').text()).toBe('Being priced');
    expect(w.get('[data-testid="qr-request-door-r1-0"]').text()).toContain('2 × 16\' 0" x 7\' 0"');
    expect(w.get('[data-testid="qr-request-door-r1-0"]').text()).toContain('Steel');
    expect(w.find('[data-testid="qr-request-add-photos-r1-0"]').exists()).toBe(false);
    expect(w.find('[data-testid="qr-request-add-photos-r1-1"]').exists()).toBe(true);
  });

  it('uploads straight away when photos are added to a sent request', async () => {
    const list = [{
      id: 'r1', job_name: 'Cabin', status: 'Received', created_at: null,
      doors: [{ index: 0, quantity: 1, size_label: '8\' 0" x 7\' 0"', photo_ids: [] }],
    }];
    const fetcher = makeFetcher({ list });
    const w = await mountTab(fetcher);
    await w.get('[data-testid="qr-request-add-photos-r1-0"]').trigger('click');
    await pickFiles(w, [photo('c.jpg'), photo('d.jpg')]);
    const uploads = fetcher.mock.calls.filter(([p]) => p === '/portal/quote-requests/r1/photos');
    expect(uploads).toHaveLength(2);
    expect(uploads[0][1].body.get('door_index')).toBe('0');
    // The list is re-read so the photo count is the server's.
    expect(fetcher.mock.calls.filter(([p, o]) => p === '/portal/quote-requests' && !o)).toHaveLength(2);
  });

  const sent = (over = {}) => ({
    id: 'r1', job_name: 'Cabin', status: 'Received', created_at: '2026-10-07T12:00:00Z',
    site_address: '9 Lake Rd', notes: null, edited_at: null, withdrawn_at: null,
    can_edit: true, can_withdraw: true,
    doors: [
      { index: 0, quantity: 1, width_ft: 16, width_in: 0, height_ft: 7, height_in: 0, size_label: '16\' 0" x 7\' 0"', photo_ids: ['p0'] },
      { index: 1, quantity: 1, width_ft: 9, width_in: 0, height_ft: 7, height_in: 0, size_label: '9\' 0" x 7\' 0"', photo_ids: [] },
    ],
    ...over,
  });

  it('changes a sent request: doors carry where they sat, new photos go to the new position', async () => {
    const fetcher = makeFetcher({ list: [sent()] });
    const w = await mountTab(fetcher);
    await w.get('[data-testid="qr-request-edit-r1"]').trigger('click');
    expect(w.get('[data-testid="qr-form-title"]').text()).toContain('Cabin');
    expect(w.get('[data-testid="qr-job-name"]').element.value).toBe('Cabin');
    expect(w.get('[data-testid="qr-door-existing-photos-0"]').text()).toContain('1 photo already sent');

    // Remove the first door (it has a photo): the customer is warned.
    await w.get('[data-testid="qr-door-remove-0"]').trigger('click');
    expect(w.find('[data-testid="qr-edit-photo-warning"]').exists()).toBe(true);
    await w.get('[data-testid="qr-add-door"]').trigger('click');
    await fillDoor(w, 1, { wf: '8', hf: '7' });
    await w.get('[data-testid="qr-door-photos-1"]').trigger('click');
    await pickFiles(w, [photo('new.jpg')]);
    await w.get('[data-testid="qr-job-name"]').setValue('Cabin, revised');
    await w.get('[data-testid="qr-submit"]').trigger('click');
    await flushPromises();

    const patch = fetcher.mock.calls.find(([, o]) => o?.method === 'PATCH');
    expect(patch[0]).toBe('/portal/quote-requests/r1');
    const body = JSON.parse(patch[1].body);
    expect(body.job_name).toBe('Cabin, revised');
    expect(body.doors.map((d) => d.source_index)).toEqual([1, null]);
    expect(body.doors[0]).toMatchObject({ width_ft: 9, height_ft: 7 });
    for (const d of body.doors) expect(d).not.toHaveProperty('_existingPhotos');

    const uploads = fetcher.mock.calls.filter(([p]) => p === '/portal/quote-requests/r1/photos');
    expect(uploads).toHaveLength(1);
    expect(uploads[0][1].body.get('door_index')).toBe('1');
    expect(w.get('[data-testid="qr-sent"]').text()).toContain('saved');
    expect(w.get('[data-testid="qr-form-title"]').text()).toContain('Request a quote');
  });

  it('counts photos already sent against the per-door cap when editing', async () => {
    const full = sent();
    full.doors[0].photo_ids = ['p0', 'p1', 'p2'];
    const fetcher = makeFetcher({ list: [full] });
    const w = await mountTab(fetcher);
    await w.get('[data-testid="qr-request-edit-r1"]').trigger('click');
    await w.get('[data-testid="qr-door-photos-0"]').trigger('click');
    await pickFiles(w, [photo('a.jpg'), photo('b.jpg'), photo('c.jpg'), photo('d.jpg')]);
    await w.get('[data-testid="qr-submit"]').trigger('click');
    await flushPromises();
    const uploads = fetcher.mock.calls.filter(([p]) => p === '/portal/quote-requests/r1/photos');
    expect(uploads).toHaveLength(1);
  });

  it('withdraws nothing when the customer keeps the request', async () => {
    confirmAnswer = false;
    const fetcher = makeFetcher({ list: [sent()] });
    const w = await mountTab(fetcher);
    await w.get('[data-testid="qr-request-withdraw-r1"]').trigger('click');
    await flushPromises();
    expect(confirmDestructive).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls.filter(([p]) => p.endsWith('/withdraw'))).toHaveLength(0);
  });

  it('withdraws a request once confirmed', async () => {
    const fetcher = makeFetcher({ list: [sent()] });
    const w = await mountTab(fetcher);
    await w.get('[data-testid="qr-request-withdraw-r1"]').trigger('click');
    await flushPromises();
    expect(confirmDestructive.mock.calls[0][0].message).toContain('Cabin');
    const calls = fetcher.mock.calls.filter(([p]) => p === '/portal/quote-requests/r1/withdraw');
    expect(calls).toHaveLength(1);
    expect(calls[0][1].method).toBe('POST');
    expect(w.get('[data-testid="qr-sent"]').text()).toContain('withdrawn');
  });

  it('shows when a request was withdrawn or changed, and offers no action it cannot take', async () => {
    const list = [
      sent({
        status: 'Withdrawn', can_edit: false, can_withdraw: false,
        edited_at: '2026-10-07T13:00:00Z', withdrawn_at: '2026-10-07T14:30:00Z',
      }),
      sent({ id: 'r2', status: 'Quote ready', can_edit: false, can_withdraw: false }),
    ];
    const w = await mountTab(makeFetcher({ list }));
    expect(w.get('[data-testid="qr-request-withdrawn-r1"]').text()).toMatch(/Withdrawn .*2026/);
    expect(w.get('[data-testid="qr-request-edited-r1"]').text()).toMatch(/Changed .*2026/);
    expect(w.find('[data-testid="qr-request-edit-r1"]').exists()).toBe(false);
    expect(w.find('[data-testid="qr-request-withdraw-r1"]').exists()).toBe(false);
    expect(w.find('[data-testid="qr-request-add-photos-r1-1"]').exists()).toBe(false);
    expect(w.find('[data-testid="qr-request-edit-r2"]').exists()).toBe(false);
    expect(w.find('[data-testid="qr-request-withdraw-r2"]').exists()).toBe(false);
    expect(w.get('[data-testid="qr-request-quote-ready-r2"]').text()).toContain('Estimates tab');
    expect(w.find('[data-testid="qr-request-quote-ready-r1"]').exists()).toBe(false);
    expect(w.find('[data-testid="qr-request-edited-r2"]').exists()).toBe(false);
  });
});
