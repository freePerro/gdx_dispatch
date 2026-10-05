import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia, setActivePinia } from 'pinia';
import InboxView from '../InboxView.vue';
import PhoneComCallsView from '../PhoneComCallsView.vue';
import PhoneComColdLeadsView from '../PhoneComColdLeadsView.vue';
import { useAuthStore } from '../../stores/auth';

const mockLeadIntakeForm = {
  name: 'LeadIntakeForm',
  props: ['visible', 'initialEmail', 'initialPhone', 'initialName', 'initialNotes', 'originRef'],
  template: `
    <div v-if="visible" class="mock-lead-intake-form"
      :data-email="initialEmail"
      :data-phone="initialPhone"
      :data-name="initialName"
      :data-notes="initialNotes"
      :data-origin="originRef">
      Mock Lead Form
    </div>
  `,
};

const COMMON_STUBS = {
  LeadIntakeForm: mockLeadIntakeForm,
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Tree: { template: '<div />' },
  ContextMenu: { template: '<div />', methods: { show() {}, hide() {} } },
  Menu: { template: '<div />', methods: { toggle() {}, hide() {} } },
  Popover: { template: '<div />', methods: { toggle() {}, hide() {} } },
  Dialog: { template: '<div v-if="visible"><slot /><slot name="footer" /></div>', props: ['visible'] },
  TreeSelect: { template: '<div />' },
  Select: { template: '<select />' },
  DatePicker: { template: '<input />' },
  InputText: { template: '<input />' },
  Tag: { template: '<span />' },
  ProgressSpinner: { template: '<div />' },
  // Renders every column's #body for the first row, so row buttons exist.
  DataTable: {
    props: ['value'],
    provide() { return { stubRow: () => (this.value || [])[0] }; },
    template: '<table><tbody><tr v-if="(value || []).length"><slot /></tr></tbody></table>',
  },
  Column: {
    inject: ['stubRow'],
    template: '<td><slot name="body" :data="stubRow()" /></td>',
  },
  Button: {
    props: ['label', 'icon', 'loading', 'disabled'],
    emits: ['click'],
    template: '<button :data-test="$attrs[\'data-test\']" @click="$emit(\'click\', $event)"><slot>{{ label }}</slot></button>',
    inheritAttrs: false,
  },
  EmailBodyFrame: { template: '<div />' },
  EmailAttachments: { template: '<div />' },
};

function mkResponse(body, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    headers: { get: () => 'application/json' },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

// Fixtures below carry the fields the real APIs return — the message detail
// has body_preview and no sender name; a call row has caller_cnam and
// has_voicemail but no transcript; a cold lead has caller_cnam.
let granted;

describe('Comms Desktop Lead Intake Integration', () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    vi.clearAllMocks();
    granted = new Set(['leads.intake']);
    vi.spyOn(useAuthStore(), 'hasPermission').mockImplementation((k) => granted.has(k));
  });

  it('InboxView: clicking Create lead opens LeadIntakeForm with prefilled email and origin', async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url) => {
      const u = typeof url === 'string' ? url : url.toString();
      if (u.includes('/api/outlook/folders')) {
        return mkResponse([{ id: 'f1', graph_folder_id: 'g-inbox', display_name: 'Inbox', unread_count: 0 }]);
      }
      if (u.includes('/api/outlook/messages/msg-123/body')) {
        return mkResponse({ html: '<p>Need a quote</p>' });
      }
      if (u.includes('/api/outlook/messages/msg-123/thread')) {
        return mkResponse([]);
      }
      if (u.includes('/api/outlook/messages/msg-123')) {
        return mkResponse({
          id: 'msg-123',
          from_address: 'client@example.com',
          linked_customer_name: 'John Client',
          subject: 'Broken Spring',
          body_preview: 'My garage door won\'t open.',
        });
      }
      if (u.includes('/api/outlook/messages')) {
        return mkResponse({
          items: [{ id: 'msg-123', from_address: 'client@example.com', subject: 'Broken Spring' }],
          total: 1,
        });
      }
      return mkResponse({});
    });

    try {
      const w = mount(InboxView, {
        global: {
          stubs: COMMON_STUBS,
          mocks: {
            $route: { query: {} },
            $router: { push: vi.fn() },
          },
        },
      });
      await flushPromises();

      // Trigger selection of msg-123 by calling openMessage
      const vm = w.vm;
      await vm.openMessage({ id: 'msg-123' });
      await flushPromises();

      const createLeadBtn = w.find('[data-test="inbox-create-lead"]');
      expect(createLeadBtn.exists()).toBe(true);
      await createLeadBtn.trigger('click');
      await flushPromises();

      const form = w.findComponent(mockLeadIntakeForm);
      expect(form.exists()).toBe(true);
      expect(form.attributes('data-email')).toBe('client@example.com');
      expect(form.attributes('data-name')).toBe('John Client');
      expect(form.attributes('data-notes')).toContain('Broken Spring');
      expect(form.attributes('data-notes')).toContain('My garage door won\'t open.');
      expect(form.attributes('data-origin')).toBe('outlook_message:msg-123');
    } finally {
      globalThis.fetch = origFetch;
    }
  });

  it('PhoneComCallsView: clicking Create Lead opens LeadIntakeForm with prefilled phone and origin', async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url) => {
      const u = typeof url === 'string' ? url : url.toString();
      if (u.includes('/api/phone-com/own-numbers')) {
        return mkResponse({ items: [] });
      }
      if (u.includes('/api/phone-com/calls/call-789/voicemail-transcript')) {
        return mkResponse({ transcript: 'Looking for a new opener' });
      }
      if (u.includes('/api/phone-com/calls')) {
        return mkResponse({
          items: [{
            id: 'call-789',
            direction: 'in',
            from_number: '+15551234567',
            to_number: '+15559876543',
            caller_cnam: 'Jane Caller',
            has_voicemail: true,
          }],
          total: 1,
        });
      }
      return mkResponse({});
    });

    try {
      const w = mount(PhoneComCallsView, {
        global: {
          stubs: COMMON_STUBS,
          directives: { tooltip: () => {} },
        },
      });
      await flushPromises();

      const vm = w.vm;
      await vm.openLeadFromCall({
        id: 'call-789',
        direction: 'in',
        from_number: '+15551234567',
        to_number: '+15559876543',
        caller_cnam: 'Jane Caller',
        has_voicemail: true,
      });
      await flushPromises();

      const form = w.findComponent(mockLeadIntakeForm);
      expect(form.exists()).toBe(true);
      expect(form.attributes('data-phone')).toBe('+15551234567');
      expect(form.attributes('data-name')).toBe('Jane Caller');
      expect(form.attributes('data-notes')).toContain('Looking for a new opener');
      expect(form.attributes('data-origin')).toBe('phone_com_call:call-789');
    } finally {
      globalThis.fetch = origFetch;
    }
  });

  it('PhoneComColdLeadsView: clicking Create lead opens LeadIntakeForm with prefilled caller data', async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url) => {
      const u = typeof url === 'string' ? url : url.toString();
      if (u.includes('/api/phone-com/cold-leads')) {
        return mkResponse({
          items: [{
            from_number: '+15557654321',
            caller_cnam: 'Acme Corp',
            call_count: 2,
            voicemail_snippet: 'Can you call me back regarding pricing?',
          }],
          total: 1,
        });
      }
      return mkResponse({});
    });

    try {
      const w = mount(PhoneComColdLeadsView, {
        global: {
          stubs: COMMON_STUBS,
          directives: { tooltip: () => {} },
        },
      });
      await flushPromises();

      const vm = w.vm;
      vm.openLeadFromColdLead({
        from_number: '+15557654321',
        caller_cnam: 'Acme Corp',
        voicemail_snippet: 'Can you call me back regarding pricing?',
      });
      await flushPromises();

      const form = w.findComponent(mockLeadIntakeForm);
      expect(form.exists()).toBe(true);
      expect(form.attributes('data-phone')).toBe('+15557654321');
      expect(form.attributes('data-name')).toBe('Acme Corp');
      expect(form.attributes('data-notes')).toContain('Can you call me back regarding pricing?');
      expect(form.attributes('data-origin')).toBe('cold_lead:+15557654321');
    } finally {
      globalThis.fetch = origFetch;
    }
  });

  it('hides every desktop Create lead button from a role without leads.intake or leads.write', async () => {
    granted = new Set();
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url) => {
      const u = typeof url === 'string' ? url : url.toString();
      if (u.includes('/api/phone-com/calls')) {
        return mkResponse({ items: [{ id: 'c1', direction: 'in', from_number: '+15551234567' }], total: 1 });
      }
      if (u.includes('/api/phone-com/cold-leads')) {
        return mkResponse({ items: [{ from_number: '+15557654321', call_count: 1 }], total: 1 });
      }
      return mkResponse({ items: [] });
    });
    try {
      const opts = { global: { stubs: COMMON_STUBS, directives: { tooltip: () => {} } } };
      const calls = mount(PhoneComCallsView, opts);
      const cold = mount(PhoneComColdLeadsView, opts);
      await flushPromises();
      expect(calls.find('[data-test="pc-create-lead"]').exists()).toBe(false);
      expect(cold.find('[data-test="pc-cl-create-lead"]').exists()).toBe(false);
    } finally {
      globalThis.fetch = origFetch;
    }
  });

  it('InboxView: on a message the shop sent, the lead email is the recipient, not the shop', async () => {
    const origFetch = globalThis.fetch;
    globalThis.fetch = vi.fn(async (url) => {
      const u = typeof url === 'string' ? url : url.toString();
      if (u.includes('/api/outlook/folders')) return mkResponse([]);
      if (u.includes('/api/outlook/messages/msg-out/body') || u.includes('/api/outlook/messages/msg-out/thread')) return mkResponse([]);
      if (u.includes('/api/outlook/messages/msg-out')) {
        return mkResponse({
          id: 'msg-out',
          from_address: 'Office@Shop.example',
          mailbox_address: 'office@shop.example',
          to_addresses: ['office@shop.example', 'customer@example.com'],
          subject: 'Your quote',
        });
      }
      return mkResponse({ items: [], total: 0 });
    });
    try {
      const w = mount(InboxView, {
        global: { stubs: COMMON_STUBS, mocks: { $route: { query: {} }, $router: { push: vi.fn() } } },
      });
      await flushPromises();
      await w.vm.openMessage({ id: 'msg-out' });
      await flushPromises();
      await w.find('[data-test="inbox-create-lead"]').trigger('click');
      await flushPromises();

      expect(w.findComponent(mockLeadIntakeForm).attributes('data-email')).toBe('customer@example.com');
    } finally {
      globalThis.fetch = origFetch;
    }
  });
});
