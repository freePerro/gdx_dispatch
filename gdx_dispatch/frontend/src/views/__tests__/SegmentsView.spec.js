/**
 * SegmentsView tests — pins the segments dialog → API contract.
 *
 * The segments API accepts `{name, rules}` where `rules` is the object the
 * backend matcher evaluates (`{match, rules:[{field, operator, value}]}`).
 * The dialog must send exactly that on create (POST) and edit (PATCH), and
 * must reopen a saved segment showing its rules as saved (round-trip).
 */
import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SegmentsView from '../SegmentsView.vue';

vi.mock('primevue/usetoast', () => ({ useToast: () => ({ add: vi.fn() }) }));
vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }));

const apiGetMock = vi.fn();
const apiPostMock = vi.fn();
const apiPatchMock = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGetMock, post: apiPostMock, patch: apiPatchMock }),
}));

const CUSTOM_SEGMENT = {
  id: '11111111-2222-3333-4444-555555555555',
  name: 'Big spenders',
  is_builtin: false,
  rules: {
    match: 'any',
    rules: [{ field: 'lifetime_value', operator: 'greater_than', value: 5000 }],
  },
};

const BUILTIN_SEGMENT = {
  id: 'at-risk',
  name: 'At Risk',
  is_builtin: true,
  rules: { field: 'last_job_date', operator: 'older_than', value: '180 days' },
};

const stubs = {
  Toolbar: { template: '<div><slot name="start" /><slot name="end" /></div>' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<div><slot /></div>' },
  TabPanels: { template: '<div><slot /></div>' },
  TabPanel: { template: '<div><slot /></div>' },
  DatePicker: { template: '<input />' },
  ToggleSwitch: { template: '<input type="checkbox" />' },
  ProgressSpinner: { template: '<div />' },
  EmptyState: { template: '<div />' },
  Column: { template: '<div />' },
  DataTable: {
    props: ['value'],
    emits: ['row-click'],
    template: `<div><button v-for="row in value" :key="row.id" data-test="segment-row"
      @click="$emit('row-click', { data: row })">{{ row.name }}</button></div>`,
  },
  Dialog: { props: ['visible'], template: "<div v-if='visible'><slot /><slot name='footer' /></div>" },
  Select: {
    props: ['modelValue', 'options', 'optionLabel', 'optionValue'],
    emits: ['update:modelValue'],
    template: `<select :value="modelValue" @change="$emit('update:modelValue', $event.target.value)">
      <option v-for="o in options" :key="o[optionValue || 'value']" :value="o[optionValue || 'value']">{{ o[optionLabel || 'label'] }}</option>
    </select>`,
  },
  InputNumber: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<input type="number" :value="modelValue" @input="$emit('update:modelValue', Number($event.target.value))" />`,
  },
  InputText: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<input :value="modelValue" @input="$emit('update:modelValue', $event.target.value)" />`,
  },
  Button: {
    props: ['label', 'disabled'],
    emits: ['click'],
    template: '<button :disabled="disabled" @click="$emit(\'click\')">{{ label }}</button>',
  },
};

function mountView(segments) {
  apiGetMock.mockImplementation(async (url) => {
    if (url.startsWith('/api/segments')) return { items: segments };
    return { items: [] };
  });
  return mount(SegmentsView, { global: { stubs } });
}

async function setSelect(wrapper, testid, value) {
  const el = wrapper.find(`[data-testid="${testid}"]`);
  expect(el.exists(), testid).toBe(true);
  await el.setValue(value);
}

describe('SegmentsView dialog → API contract', () => {
  beforeEach(() => {
    apiGetMock.mockReset();
    apiPostMock.mockReset().mockResolvedValue({});
    apiPatchMock.mockReset().mockResolvedValue({});
  });

  it('creates a segment with a {name, rules} body the matcher can run', async () => {
    const wrapper = mountView([]);
    await flushPromises();

    await wrapper.find('[data-testid="segments-open-dialog"]').trigger('click');
    await wrapper.find('[data-testid="segments-dialog-name"]').setValue('Stale');
    await setSelect(wrapper, 'segments-dialog-match', 'all');
    await setSelect(wrapper, 'segments-rule-field-0', 'last_job_date');
    await setSelect(wrapper, 'segments-rule-operator-0', 'older_than');
    await wrapper.find('[data-testid="segments-rule-value-0"]').setValue('90');

    await wrapper.find('[data-testid="segments-dialog-save"]').trigger('click');
    await flushPromises();

    expect(apiPostMock).toHaveBeenCalledTimes(1);
    const [url, body] = apiPostMock.mock.calls[0];
    expect(url).toBe('/api/segments');
    expect(body).toEqual({
      name: 'Stale',
      rules: {
        match: 'all',
        rules: [{ field: 'last_job_date', operator: 'older_than', value: 90 }],
      },
    });
    expect(apiPatchMock).not.toHaveBeenCalled();
  });

  it('reopens a saved segment with its rules intact and PATCHes {name, rules}', async () => {
    const wrapper = mountView([CUSTOM_SEGMENT]);
    await flushPromises();

    await wrapper.find('[data-test="segment-row"]').trigger('click');
    await flushPromises();

    // round-trip: what was saved is what the dialog shows
    expect(wrapper.find('[data-testid="segments-dialog-name"]').element.value).toBe('Big spenders');
    expect(wrapper.find('[data-testid="segments-dialog-match"]').element.value).toBe('any');
    expect(wrapper.find('[data-testid="segments-rule-field-0"]').element.value).toBe('lifetime_value');
    expect(wrapper.find('[data-testid="segments-rule-operator-0"]').element.value).toBe('greater_than');
    expect(wrapper.find('[data-testid="segments-rule-value-0"]').element.value).toBe('5000');

    await wrapper.find('[data-testid="segments-rule-value-0"]').setValue('6000');
    await wrapper.find('[data-testid="segments-dialog-save"]').trigger('click');
    await flushPromises();

    expect(apiPostMock).not.toHaveBeenCalled();
    expect(apiPatchMock).toHaveBeenCalledTimes(1);
    const [url, body] = apiPatchMock.mock.calls[0];
    expect(url).toBe(`/api/segments/${CUSTOM_SEGMENT.id}`);
    expect(body).toEqual({
      name: 'Big spenders',
      rules: {
        match: 'any',
        rules: [{ field: 'lifetime_value', operator: 'greater_than', value: 6000 }],
      },
    });
  });

  it('shows a builtin single-rule segment ("180 days") as its day count and refuses to save it', async () => {
    const wrapper = mountView([BUILTIN_SEGMENT]);
    await flushPromises();

    await wrapper.find('[data-test="segment-row"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-testid="segments-rule-field-0"]').element.value).toBe('last_job_date');
    expect(wrapper.find('[data-testid="segments-rule-operator-0"]').element.value).toBe('older_than');
    expect(wrapper.find('[data-testid="segments-rule-value-0"]').element.value).toBe('180');

    const save = wrapper.find('[data-testid="segments-dialog-save"]');
    expect(save.attributes('disabled')).toBeDefined();
    await save.trigger('click');
    await flushPromises();
    expect(apiPatchMock).not.toHaveBeenCalled();
    expect(apiPostMock).not.toHaveBeenCalled();
  });

  it('refuses to save a rule with no value and never sends criteria/tags', async () => {
    const wrapper = mountView([]);
    await flushPromises();

    await wrapper.find('[data-testid="segments-open-dialog"]').trigger('click');
    await wrapper.find('[data-testid="segments-dialog-name"]').setValue('Empty');
    await wrapper.find('[data-testid="segments-dialog-save"]').trigger('click');
    await flushPromises();
    expect(apiPostMock).not.toHaveBeenCalled();

    await wrapper.find('[data-testid="segments-rule-value-0"]').setValue('30');
    await wrapper.find('[data-testid="segments-dialog-save"]').trigger('click');
    await flushPromises();
    expect(apiPostMock).toHaveBeenCalledTimes(1);
    const body = apiPostMock.mock.calls[0][1];
    expect(Object.keys(body).sort()).toEqual(['name', 'rules']);
  });
});
