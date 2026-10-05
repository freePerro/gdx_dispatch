import { describe, expect, it, vi, beforeEach } from 'vitest';
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import MobilePhoneView from '../MobilePhoneView.vue';

const { apiGet, apiPost, perms } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  perms: { value: new Set(['leads.write']) },
}));

// The Lead button shows only for a key the intake route accepts.
vi.mock('../../composables/usePermission', () => ({
  usePermission: () => ({ hasPermission: (k) => perms.value.has(k) }),
}));

vi.mock('../../composables/useApi', () => ({
  useApi: () => ({
    get: apiGet,
    post: apiPost,
  }),
}));

const stubs = {
  RouterLink: RouterLinkStub,
  Dialog: {
    props: ['visible'],
    template: '<div v-if="visible" class="p-dialog-stub"><slot /></div>',
  },
  Button: {
    props: ['label', 'icon'],
    template: '<button type="button" @click="$emit(\'click\')">{{ label }}</button>',
  },
  LeadIntakeForm: {
    props: ['visible', 'initialPhone', 'initialName', 'initialNotes', 'originRef'],
    template: `
      <div v-if="visible" data-test="lead-intake-stub">
        <span data-test="prop-phone">{{ initialPhone }}</span>
        <span data-test="prop-name">{{ initialName }}</span>
        <span data-test="prop-notes">{{ initialNotes }}</span>
        <span data-test="prop-origin-ref">{{ originRef }}</span>
      </div>
    `,
  },
};

describe('MobilePhoneView — Lead intake prefill', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset();
    perms.value = new Set(['leads.write']);
  });

  it('clicking row lead button opens LeadIntakeForm prefilled with call info', async () => {
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/phone-com/calls?')) {
        return {
          items: [
            {
              id: 'call-101',
              direction: 'in',
              from_number: '+15551234567',
              to_number: '+15559876543',
              customer_name: 'John Doe',
              started_at: '2026-09-28T10:00:00Z',
              duration_s: 45,
            },
          ],
          total: 1,
        };
      }
      return {};
    });

    const wrapper = mount(MobilePhoneView, {
      global: {
        plugins: [PrimeVue],
        stubs,
        directives: { tooltip: {} },
      },
    });

    await flushPromises();

    const rowBtn = wrapper.find('[data-test="mp-row-create-lead"]');
    expect(rowBtn.exists()).toBe(true);

    await rowBtn.trigger('click');
    await flushPromises();

    const leadStub = wrapper.find('[data-test="lead-intake-stub"]');
    expect(leadStub.exists()).toBe(true);
    expect(wrapper.find('[data-test="prop-phone"]').text()).toBe('+15551234567');
    expect(wrapper.find('[data-test="prop-name"]').text()).toBe('John Doe');
    expect(wrapper.find('[data-test="prop-origin-ref"]').text()).toBe('phone_com_call:call-101');
    expect(wrapper.find('[data-test="prop-notes"]').text()).toContain('Inbound call from +15551234567');
  });

  it('clicking detail lead button opens LeadIntakeForm prefilled with transcript', async () => {
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/phone-com/calls?')) {
        return {
          items: [
            {
              id: 'call-202',
              direction: 'in',
              from_number: '+15552223333',
              to_number: '+15559876543',
              customer_name: 'Jane Smith',
              has_voicemail: true,
              started_at: '2026-09-28T11:00:00Z',
            },
          ],
          total: 1,
        };
      }
      if (url === '/api/phone-com/calls/call-202') {
        return {
          id: 'call-202',
          direction: 'in',
          from_number: '+15552223333',
          to_number: '+15559876543',
          customer_name: 'Jane Smith',
          has_voicemail: true,
          started_at: '2026-09-28T11:00:00Z',
        };
      }
      if (url === '/api/phone-com/calls/call-202/voicemail-transcript') {
        return { transcript: 'Need broken spring replaced on garage door.' };
      }
      return {};
    });

    const wrapper = mount(MobilePhoneView, {
      global: {
        plugins: [PrimeVue],
        stubs,
        directives: { tooltip: {} },
      },
    });

    await flushPromises();

    // Open call detail
    await wrapper.find('[data-test="mp-call-row"]').trigger('click');
    await flushPromises();

    const detailBtn = wrapper.find('[data-test="mp-create-lead"]');
    expect(detailBtn.exists()).toBe(true);

    await detailBtn.trigger('click');
    await flushPromises();

    const leadStub = wrapper.find('[data-test="lead-intake-stub"]');
    expect(leadStub.exists()).toBe(true);
    expect(wrapper.find('[data-test="prop-phone"]').text()).toBe('+15552223333');
    expect(wrapper.find('[data-test="prop-name"]').text()).toBe('Jane Smith');
    expect(wrapper.find('[data-test="prop-origin-ref"]').text()).toBe('phone_com_call:call-202');
    expect(wrapper.find('[data-test="prop-notes"]').text()).toContain('Need broken spring replaced');
  });

  function mockOneCall(call) {
    apiGet.mockImplementation(async (url) =>
      url.startsWith('/api/phone-com/calls?') ? { items: [call], total: 1 } : {});
  }
  const mountView = async () => {
    const w = mount(MobilePhoneView, { global: { plugins: [PrimeVue], stubs, directives: { tooltip: {} } } });
    await flushPromises();
    return w;
  };

  it('no customer match: the caller ID name prefills, but never a CNAM that is just a place', async () => {
    mockOneCall({ id: 'c1', direction: 'in', from_number: '+15550001111', to_number: '+15559876543',
      caller_cnam: 'Pat Garage', customer_name: null, started_at: '2026-09-28T10:00:00Z' });
    let w = await mountView();
    await w.find('[data-test="mp-row-create-lead"]').trigger('click');
    expect(w.find('[data-test="prop-name"]').text()).toBe('Pat Garage');
    w.unmount();

    mockOneCall({ id: 'c2', direction: 'in', from_number: '+15550001111', to_number: '+15559876543',
      caller_cnam: 'SEBEKA MN', customer_name: null, started_at: '2026-09-28T10:00:00Z' });
    w = await mountView();
    await w.find('[data-test="mp-row-create-lead"]').trigger('click');
    expect(w.find('[data-test="prop-name"]').text()).toBe('');
    w.unmount();
  });

  it('an office user with no leads key sees no Lead button', async () => {
    perms.value = new Set();
    mockOneCall({ id: 'c3', direction: 'in', from_number: '+15550001111', to_number: '+15559876543',
      started_at: '2026-09-28T10:00:00Z' });
    const w = await mountView();
    expect(w.find('[data-test="mp-call-row"]').exists()).toBe(true);
    expect(w.find('[data-test="mp-row-create-lead"]').exists()).toBe(false);
  });

  it('detail dialog on an unmatched voicemail: caller ID name from the row, transcript in notes', async () => {
    const row = { id: 'c4', direction: 'in', from_number: '+15550002222', to_number: '+15559876543',
      caller_cnam: 'Robin Doors', customer_name: null, has_voicemail: true, started_at: '2026-09-28T10:00:00Z' };
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/phone-com/calls?')) return { items: [row], total: 1 };
      // The real detail response has no caller_cnam (get_call_detail).
      if (url === '/api/phone-com/calls/c4') { const { caller_cnam, ...d } = row; return d; }
      if (url === '/api/phone-com/calls/c4/voicemail-transcript') return { transcript: 'Opener stopped working.' };
      return {};
    });
    const w = await mountView();
    await w.find('[data-test="mp-call-row"]').trigger('click');
    await flushPromises();
    await w.find('[data-test="mp-create-lead"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-test="prop-name"]').text()).toBe('Robin Doors');
    expect(w.find('[data-test="prop-notes"]').text()).toContain('Opener stopped working.');
  });

  it('row button on a voicemail fetches the transcript it has not loaded yet', async () => {
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/phone-com/calls?')) return { items: [{ id: 'c5', direction: 'in',
        from_number: '+15550003333', to_number: '+15559876543', has_voicemail: true,
        started_at: '2026-09-28T10:00:00Z' }], total: 1 };
      if (url === '/api/phone-com/calls/c5/voicemail-transcript') return { transcript: 'Spring snapped this morning.' };
      return {};
    });
    const w = await mountView();
    await w.find('[data-test="mp-row-create-lead"]').trigger('click');
    await flushPromises();
    expect(w.find('[data-test="prop-notes"]').text()).toContain('Spring snapped this morning.');
  });
});
