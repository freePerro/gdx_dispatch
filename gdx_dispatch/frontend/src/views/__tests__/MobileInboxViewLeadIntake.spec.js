import { describe, expect, it, vi, beforeEach } from 'vitest';
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils';
import PrimeVue from 'primevue/config';
import MobileInboxView from '../MobileInboxView.vue';

const { apiGet, apiPost } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  apiPost: vi.fn(),
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

describe('MobileInboxView — Lead intake prefill', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset();
  });

  it('clicking Lead button in message detail opens LeadIntakeForm with prefilled email and subject', async () => {
    apiGet.mockImplementation(async (url) => {
      if (url.startsWith('/api/outlook/messages?')) {
        return {
          items: [
            {
              id: 'msg-456',
              subject: 'Need quote for new roll-up door',
              from_name: 'Bob Builder',
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
          from_name: 'Bob Builder',
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
});
