/**
 * PdfTemplateEditorView — line-items options + settings normalization.
 *
 * The editor's config now actually drives the rendered PDFs (pdf_generator
 * consumes it), so these pins cover the operator-facing contract:
 * - legacy saved templates (show_unit_price/show_tax era) get the new
 *   line-item keys defaulted on load, so toggles bind to real booleans
 * - Show Category column/grouped modes reflect in the preview table
 * - the non-taxable marker option only appears for invoice templates
 *   (estimate lines carry no taxable flag server-side)
 * - Save PUTs the normalized config back
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { mount, flushPromises } from '@vue/test-utils';

const apiGet = vi.fn();
const apiPut = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, put: apiPut }),
}));
vi.mock('primevue/usetoast', () => ({
  useToast: () => ({ add: vi.fn() }),
}));

import PdfTemplateEditorView from '../PdfTemplateEditorView.vue';

const stubs = {
  Button: {
    emits: ['click'],
    template: '<button :data-testid="$attrs[\'data-testid\']" @click="$emit(\'click\')"><slot /></button>',
    inheritAttrs: false,
  },
  ColorPicker: { props: ['modelValue'], template: '<input />' },
  ProgressSpinner: { template: '<div data-testid="spinner"></div>' },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />',
    inheritAttrs: false,
  },
  InputNumber: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input :data-testid="$attrs[\'data-testid\']" :value="modelValue" @input="$emit(\'update:modelValue\', Number($event.target.value))" />',
    inheritAttrs: false,
  },
  Select: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue', 'change'],
    template: `<select :data-testid="$attrs['data-testid']" :value="modelValue"
      @change="$emit('update:modelValue', $event.target.value); $emit('change', $event)">
      <option v-for="o in options" :key="o.value ?? o.key ?? o" :value="o.value ?? o.key ?? o">{{ o.label ?? o }}</option>
    </select>`,
    inheritAttrs: false,
  },
  ToggleSwitch: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: '<input type="checkbox" :data-testid="$attrs[\'data-testid\']" :checked="modelValue" @change="$emit(\'update:modelValue\', $event.target.checked)" />',
    inheritAttrs: false,
  },
};

function templatePayload(type, lineItemSettings) {
  return {
    id: null,
    template_type: type,
    brand_color: '#0057a8',
    font_family: 'Helvetica',
    header_content: null,
    footer_content: null,
    logo_url: null,
    updated_at: null,
    blocks: [
      { id: 'logo', type: 'logo', order: 1, visible: true, styles: {}, settings: {} },
      { id: 'company_info', type: 'company_info', order: 2, visible: true, styles: {}, settings: {} },
      { id: 'customer_info', type: 'customer_info', order: 3, visible: true, styles: {}, settings: {} },
      { id: 'line_items', type: 'line_items', order: 4, visible: true, styles: {}, settings: lineItemSettings },
      { id: 'totals', type: 'totals', order: 5, visible: true, styles: {}, settings: {} },
      { id: 'notes', type: 'notes', order: 6, visible: true, styles: {}, settings: {} },
      { id: 'terms', type: 'terms', order: 7, visible: true, styles: {}, settings: {} },
      { id: 'signature', type: 'signature', order: 8, visible: type === 'estimate', styles: {}, settings: {} },
    ],
  };
}

async function mountEditor(lineItemSettings = {}) {
  apiGet.mockImplementation((url) => {
    const type = url.split('/').pop();
    return Promise.resolve(templatePayload(type, lineItemSettings));
  });
  const wrapper = mount(PdfTemplateEditorView, { global: { stubs } });
  await flushPromises();
  return wrapper;
}

function selectLineItemsBlock(wrapper) {
  const item = wrapper.findAll('.block-item').find((b) => b.text().includes('Line Items'));
  return item.trigger('click');
}

beforeEach(() => {
  apiGet.mockReset();
  apiPut.mockReset();
});

describe('PdfTemplateEditorView — line-item options', () => {
  it('loads the estimate template on mount and lists all 8 blocks', async () => {
    const wrapper = await mountEditor();
    expect(apiGet).toHaveBeenCalledWith('/api/pdf-templates/estimate');
    expect(wrapper.findAll('.block-item')).toHaveLength(8);
  });

  it('defaults the new line-item keys on legacy saved templates', async () => {
    // legacy era settings only — no show_category/category_display keys
    const wrapper = await mountEditor({ show_unit_price: true, show_tax: true });
    await selectLineItemsBlock(wrapper);
    const toggle = wrapper.find('[data-testid="li-show-category"]');
    expect(toggle.exists()).toBe(true);
    expect(toggle.element.checked).toBe(false);
    // stale toggles are gone
    expect(wrapper.text()).not.toContain('Show Unit Price');
    expect(wrapper.text()).not.toContain('Show Tax');
  });

  it('shows a Category column in the preview when enabled in column mode', async () => {
    const wrapper = await mountEditor();
    await selectLineItemsBlock(wrapper);
    expect(wrapper.find('[data-testid="preview-cat-th"]').exists()).toBe(false);

    await wrapper.find('[data-testid="li-show-category"]').setValue(true);
    expect(wrapper.find('[data-testid="preview-cat-th"]').exists()).toBe(true);
    expect(wrapper.find('[data-testid="preview-line-items"]').text()).toContain('Door');
  });

  it('renders grouped category sections when display=grouped', async () => {
    const wrapper = await mountEditor({ show_category: true, category_display: 'column' });
    await selectLineItemsBlock(wrapper);
    await wrapper.find('[data-testid="li-category-display"]').setValue('grouped');

    expect(wrapper.find('[data-testid="preview-cat-th"]').exists()).toBe(false);
    const rows = wrapper.findAll('[data-testid="preview-cat-row"]');
    expect(rows.map((r) => r.text())).toEqual(['Door', 'Parts', 'Labor']);
  });

  it('offers the non-taxable marker only on invoice templates, and previews it', async () => {
    const wrapper = await mountEditor();
    await selectLineItemsBlock(wrapper);
    expect(wrapper.find('[data-testid="li-taxable-marker"]').exists()).toBe(false);

    await wrapper.find('[data-testid="template-type-select"]').setValue('invoice');
    await flushPromises();
    expect(apiGet).toHaveBeenCalledWith('/api/pdf-templates/invoice');

    await selectLineItemsBlock(wrapper);
    const marker = wrapper.find('[data-testid="li-taxable-marker"]');
    expect(marker.exists()).toBe(true);

    await marker.setValue(true);
    const preview = wrapper.find('[data-testid="preview-line-items"]');
    expect(preview.text()).toContain('non-taxable');
    // only the labor sample line is non-taxable
    expect(preview.findAll('.preview-nontax')).toHaveLength(1);
  });

  it('saves the normalized config back with the new keys', async () => {
    apiPut.mockResolvedValue({ status: 'saved' });
    const wrapper = await mountEditor({ show_unit_price: true });
    await selectLineItemsBlock(wrapper);
    await wrapper.find('[data-testid="li-show-category"]').setValue(true);
    await wrapper.find('[data-testid="save-btn"]').trigger('click');
    await flushPromises();

    expect(apiPut).toHaveBeenCalledTimes(1);
    const [url, body] = apiPut.mock.calls[0];
    expect(url).toBe('/api/pdf-templates/estimate');
    const li = body.blocks.find((b) => b.type === 'line_items');
    expect(li.settings.show_category).toBe(true);
    expect(li.settings.category_display).toBe('column');
    expect(li.settings.show_taxable_marker).toBe(false);
  });
});
