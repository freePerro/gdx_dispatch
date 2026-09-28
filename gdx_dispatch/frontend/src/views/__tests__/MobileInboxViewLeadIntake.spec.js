import { describe, expect, it, vi, beforeEach } from 'vitest';
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import MobileInboxView from '../MobileInboxView.vue';

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
    patch: vi.fn().mockResolvedValue({}),
  }),
}));

vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: vi.fn() }),
}));

const stubs = {
  RouterLink: RouterLinkStub,
  Dialog: {
    props: ['visible'],
    template: '<div v-if="visible" class="p-dialog-stub"><slot /><slot name="footer" /></div>',
  },
  Button: {
    props: ['label', 'icon'],
    template: '<button type="button" @click="$emit(\'click\')">{{ label }}</button>',
  },
  EmailBodyFrame: true,
  EmailAttachments: true,
  LeadIntakeForm: {
    props: ['visible', 'initialPhone', 'initialName', 'initialEmail', 'initialNotes', 'originRef'],
    template: `
      <div v-if="visible" data-test="lead-intake-stub">
        <span data-test="prop-phone">{{ initialPhone }}</span>
        <span data-test="prop-name">{{ initialName }}</span>
        <span data-test="prop-email">{{ initialEmail }}</span>
        <span data-test="prop-notes">{{ initialNotes }}</span>
        <span data-test="prop-origin-ref">{{ originRef }}</span>
      </div>
    `,
  },
};

function mockInbox() {
  apiGet.mockImplementation(async (url) => {
    if (url.startsWith('/api/outlook/messages?')) {
      return {
        items: [
          {
            id: 'msg-456',
            subject: 'Need quote for new roll-up door',
            linked_customer_name: 'Bob Builder',
            from_address: 'bob@example.com',
            received_at: '2026-09-28T12:00:00Z',
            is_read: false,
            has_attachments: false,
          },
        ],
        has_more: false,
        total: 1,
      };
    }
    if (url === '/api/outlook/messages/msg-456') {
      return {
        id: 'msg-456',
        subject: 'Need quote for new roll-up door',
        linked_customer_name: 'Bob Builder',
        from_address: 'bob@example.com',
        body_preview: 'Hello, looking for an estimate on replacing our commercial garage door.',
        received_at: '2026-09-28T12:00:00Z',
        is_read: false,
        has_attachments: false,
      };
    }
    if (url === '/api/outlook/messages/msg-456/body') {
      return { body_preview: 'Hello, looking for an estimate on replacing our commercial garage door.' };
    }
    if (url === '/api/outlook/messages/msg-456/conversation') {
      return { messages: [] };
    }
    if (url === '/api/outlook/sync-health') {
      return { status: 'healthy' };
    }
    return {};
  });
}

async function openMessage() {
  const wrapper = mount(MobileInboxView, { global: { plugins: [PrimeVue], stubs } });
  await flushPromises();
  await wrapper.find('.msg-card').trigger('click');
  await flushPromises();
  return wrapper;
}

describe('MobileInboxView — Lead intake prefill', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset();
    perms.value = new Set(['leads.write']);
  });

  it('clicking Lead button in message detail opens LeadIntakeForm with prefilled email and subject', async () => {
    mockInbox();

    const wrapper = mount(MobileInboxView, {
      global: {
        plugins: [PrimeVue],
        stubs,
      },
    });

    await flushPromises();

    // Click on message card to open detail
    await wrapper.find('.msg-card').trigger('click');
    await flushPromises();

    // Find the Lead button
    const leadBtn = wrapper.find('[data-test="mi-create-lead"]');
    expect(leadBtn.exists()).toBe(true);

    await leadBtn.trigger('click');
    await flushPromises();

    const leadStub = wrapper.find('[data-test="lead-intake-stub"]');
    expect(leadStub.exists()).toBe(true);
    expect(wrapper.find('[data-test="prop-email"]').text()).toBe('bob@example.com');
    expect(wrapper.find('[data-test="prop-name"]').text()).toBe('Bob Builder');
    expect(wrapper.find('[data-test="prop-origin-ref"]').text()).toBe('outlook_message:msg-456');
    expect(wrapper.find('[data-test="prop-notes"]').text()).toContain('Subject: Need quote for new roll-up door');
    expect(wrapper.find('[data-test="prop-notes"]').text()).toContain('commercial garage door');
  });

  it('a user with no leads key gets no Lead button (the server would 403 it)', async () => {
    perms.value = new Set();
    mockInbox();
    const wrapper = await openMessage();
    expect(wrapper.find('[data-test="mi-create-task"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="mi-create-lead"]').exists()).toBe(false);
  });

  it('an unlinked sender prefills the address and leaves the name for the user', async () => {
    mockInbox();
    const base = apiGet.getMockImplementation();
    apiGet.mockImplementation(async (url) => {
      const r = await base(url);
      return url === '/api/outlook/messages/msg-456' ? { ...r, linked_customer_name: null } : r;
    });
    const wrapper = await openMessage();
    await wrapper.find('[data-test="mi-create-lead"]').trigger('click');
    expect(wrapper.find('[data-test="prop-email"]').text()).toBe('bob@example.com');
    expect(wrapper.find('[data-test="prop-name"]').text()).toBe('');
  });

  it('a technician holding leads.intake gets it', async () => {
    perms.value = new Set(['leads.intake']);
    mockInbox();
    const wrapper = await openMessage();
    expect(wrapper.find('[data-test="mi-create-lead"]').exists()).toBe(true);
  });
});
