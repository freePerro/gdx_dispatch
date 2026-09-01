/**
 * Settings → Estimates: tenant-editable INVOICE and RECEIPT email templates
 * (issue #351).
 *
 * These are MOUNT tests, for the same reason SettingsBrandingContact.spec.js
 * gives: a regex over the .vue source proves only that someone typed a
 * data-testid. Only a mount proves the four controls render, are populated
 * from GET /api/estimates-features, and survive Save — the silent-write shape
 * (button works, payload drops the field) is a real failure this catches.
 *
 * The placeholder assertions matter too: what the empty field SHOWS is what a
 * blank field SENDS (routers/invoices.py defaults), so the two must not drift.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';
import { createPinia } from 'pinia';

const apiGet = vi.fn();
const apiPatch = vi.fn();
const apiPost = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, patch: apiPatch, post: apiPost, delete: vi.fn(), put: vi.fn() }),
}));
vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('../../composables/useDestructiveConfirm', () => ({
  useDestructiveConfirm: () => ({ confirmDestructive: vi.fn(async () => true) }),
}));
vi.mock('../../composables/useTenantModules', () => ({
  useTenantModules: () => ({ loadTenantModules: vi.fn() }),
}));
vi.mock('../../composables/useIdleLogout', () => ({
  getIdleTimeoutMin: () => 30,
  setIdleTimeoutMin: vi.fn(),
}));

import SettingsView from '../SettingsView.vue';

const FEATURES = {
  estimates_allow_line_margin_override: true,
  estimates_default_terms: '',
  estimate_email_subject_template: '',
  estimate_email_body_template: '',
  estimate_deposit_pct: 50,
  estimates_hide_line_prices: false,
  estimate_expiry_days: 60,
  invoice_email_subject_template: 'Bill {{invoice_number}} from {{company_name}}',
  invoice_email_body_template: 'Hi {{customer_name}},\n\nInvoice body here.',
  receipt_email_subject_template: 'Thanks — Invoice {{invoice_number}} from {{company_name}}',
  receipt_email_body_template: 'Hi {{customer_name}},\n\nReceipt body here.',
};

// Exactly the ctx keys _prepare_invoice_email renders (routers/invoices.py).
const CTX_KEYS = [
  'customer_name', 'job_title', 'invoice_number', 'company_name', 'total',
  'balance_due', 'balance_line', 'paid_line', 'due_line',
];

// Pass-through stubs so the Estimates TabPanel renders its slot. InputText,
// Textarea and Button stay REAL so the inputs carry their data-testid and
// v-model, and Save is a clickable <button>.
const passthrough = { template: '<div><slot /></div>' };
const stubs = {
  Tabs: passthrough, TabList: passthrough, TabPanels: passthrough,
  Tab: passthrough, TabPanel: passthrough,
  Card: { template: '<div><slot name="title" /><slot name="content" /><slot /></div>' },
  Dialog: { template: '<div><slot /></div>' },
  DataTable: true, Column: true, Toolbar: true, Badge: true, Tag: true,
  Divider: true, ProgressSpinner: true, Password: true,
  Select: true, ToggleSwitch: true, InputNumber: true,
  AIAssistantIntegrationCard: true, GoogleMapsIntegrationCard: true,
  PhoneComIntegrationCard: true, OutlookIntegrationCard: true,
  SimpleFINCard: true, OutlookConnectButton: true, MarginTiersPanel: true,
};

async function mountSettings() {
  const wrapper = mount(SettingsView, { global: { stubs, plugins: [createPinia()] } });
  await flushPromises();
  return wrapper;
}

const TESTIDS = {
  invoice_email_subject_template: 'inv-email-subject-template',
  invoice_email_body_template: 'inv-email-body-template',
  receipt_email_subject_template: 'rcpt-email-subject-template',
  receipt_email_body_template: 'rcpt-email-body-template',
};

beforeEach(() => {
  apiGet.mockReset();
  apiPatch.mockReset();
  apiPost.mockReset();
  // Only the features loader resolves; every other loader rejects and is
  // swallowed by the onMounted Promise.allSettled — a partially-configured
  // tenant, which is the common case.
  apiGet.mockImplementation((url) =>
    url === '/api/estimates-features'
      ? Promise.resolve({ ...FEATURES })
      : Promise.reject(new Error(`unmocked GET ${url}`)),
  );
  apiPatch.mockResolvedValue({ ...FEATURES });
  apiPost.mockResolvedValue({});
});

describe('Estimates card — invoice and receipt email templates', () => {
  it('renders an input for all four templates, populated from the server', async () => {
    const wrapper = await mountSettings();
    for (const [key, testid] of Object.entries(TESTIDS)) {
      const el = wrapper.find(`[data-testid="${testid}"]`);
      expect(el.exists(), testid).toBe(true);
      expect(el.element.value, testid).toBe(FEATURES[key]);
    }
    // Subjects are single-line inputs; bodies are textareas (newlines matter).
    expect(wrapper.find('[data-testid="inv-email-subject-template"]').element.tagName).toBe('INPUT');
    expect(wrapper.find('[data-testid="inv-email-body-template"]').element.tagName).toBe('TEXTAREA');
    expect(wrapper.find('[data-testid="rcpt-email-subject-template"]').element.tagName).toBe('INPUT');
    expect(wrapper.find('[data-testid="rcpt-email-body-template"]').element.tagName).toBe('TEXTAREA');
  });

  it('sits right under the estimate template fields, in the same card', async () => {
    const wrapper = await mountSettings();
    const ids = Array.from(wrapper.element.querySelectorAll('[data-testid]'))
      .map((el) => el.getAttribute('data-testid'));
    const order = [
      'est-email-subject-template', 'est-email-body-template',
      'inv-email-subject-template', 'inv-email-body-template',
      'rcpt-email-subject-template', 'rcpt-email-body-template',
      'estimates-features-save',
    ].map((id) => ids.indexOf(id));
    expect(order.every((i) => i >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });

  it('shows the platform defaults as placeholders, verbatim from routers/invoices.py', async () => {
    const wrapper = await mountSettings();
    expect(wrapper.find('[data-testid="inv-email-subject-template"]').attributes('placeholder'))
      .toBe('Invoice {{invoice_number}} from {{company_name}}');
    expect(wrapper.find('[data-testid="inv-email-body-template"]').attributes('placeholder'))
      .toBe('Hi {{customer_name}},\n\nPlease see the attached invoice ({{invoice_number}}) for {{job_title}}.\nTotal: {{total}}{{balance_line}}{{due_line}}\n\nThanks,\n{{company_name}}');
    expect(wrapper.find('[data-testid="rcpt-email-subject-template"]').attributes('placeholder'))
      .toBe('Payment received — Invoice {{invoice_number}} from {{company_name}}');
    expect(wrapper.find('[data-testid="rcpt-email-body-template"]').attributes('placeholder'))
      .toBe('Hi {{customer_name}},\n\nThank you for your payment on {{job_title}}. Invoice {{invoice_number}} is paid — a copy is attached for your records.\nTotal: {{total}}{{paid_line}}{{balance_line}}\n\nWe appreciate your business!\n\nThanks,\n{{company_name}}');
  });

  it('documents every placeholder the renderer supports, and the bounce-matching rule', async () => {
    const wrapper = await mountSettings();
    const text = wrapper.text();
    for (const key of CTX_KEYS) {
      expect(text, key).toContain(`{{${key}}}`);
    }
    expect(text).toContain('Invoice Email — Subject');
    expect(text).toContain('Invoice Email — Body');
    expect(text).toContain('Receipt Email — Subject');
    expect(text).toContain('Receipt Email — Body');
    expect(text).toMatch(/bounced email/);
  });

  it('sends all four edited templates in the PATCH payload', async () => {
    const wrapper = await mountSettings();
    await wrapper.find('[data-testid="inv-email-subject-template"]').setValue('Statement {{invoice_number}} from {{company_name}}');
    await wrapper.find('[data-testid="inv-email-body-template"]').setValue('Hi {{customer_name}},\n\nNew invoice copy.');
    await wrapper.find('[data-testid="rcpt-email-subject-template"]').setValue('Paid — Invoice {{invoice_number}} from {{company_name}}');
    await wrapper.find('[data-testid="rcpt-email-body-template"]').setValue('Hi {{customer_name}},\n\nNew receipt copy.');
    await wrapper.find('[data-testid="estimates-features-save"]').trigger('click');
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledWith(
      '/api/estimates-features',
      expect.objectContaining({
        invoice_email_subject_template: 'Statement {{invoice_number}} from {{company_name}}',
        invoice_email_body_template: 'Hi {{customer_name}},\n\nNew invoice copy.',
        receipt_email_subject_template: 'Paid — Invoice {{invoice_number}} from {{company_name}}',
        receipt_email_body_template: 'Hi {{customer_name}},\n\nNew receipt copy.',
        // The pre-existing estimate fields ride along untouched.
        estimate_email_subject_template: '',
        estimate_deposit_pct: 50,
      }),
      expect.anything(),
    );
  });

  it('a cleared template is sent as "" — the only way back to the platform default', async () => {
    const wrapper = await mountSettings();
    await wrapper.find('[data-testid="rcpt-email-subject-template"]').setValue('');
    await wrapper.find('[data-testid="estimates-features-save"]').trigger('click');
    await flushPromises();

    expect(apiPatch).toHaveBeenCalledWith(
      '/api/estimates-features',
      expect.objectContaining({ receipt_email_subject_template: '' }),
      expect.anything(),
    );
  });
});
