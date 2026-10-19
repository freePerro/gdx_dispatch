/**
 * SurveysView — the template form posts the schema's keys (GDXA-1676).
 *
 * The form used to send template_type/send_channel/message, which
 * SurveyTemplateIn (routers/surveys.py) does not have: create was a 422 every
 * time, and edit answered 200 while kind/question never changed. The schema
 * now forbids unknown keys; this pins the frontend side of the same contract.
 * test_surveys.py::test_ui_create_payload_persists_kind_and_question posts
 * this payload shape to the real router.
 */
import { mount, flushPromises } from '@vue/test-utils';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SurveysView from '../SurveysView.vue';

const SCHEMA_FIELDS = ['name', 'kind', 'question', 'follow_up_question', 'active'];

const apiGet = vi.fn();
const apiPost = vi.fn();
const apiPatch = vi.fn();
vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: apiGet, post: apiPost, patch: apiPatch, del: vi.fn() }),
}));

const vModelInput = (tag) => ({
  props: ['modelValue'],
  emits: ['update:modelValue'],
  template: `<${tag} v-bind="$attrs" :value="modelValue"
      @input="$emit('update:modelValue', $event.target.value)" />`,
});

const stubs = {
  Toolbar: { template: '<div><slot name="start" /></div>' },
  Tabs: { template: '<div><slot /></div>' },
  TabList: { template: '<div><slot /></div>' },
  Tab: { template: '<div><slot /></div>' },
  ProgressSpinner: { template: '<div />' },
  Card: { template: '<div><slot /></div>' },
  // Each row provides itself so the Column stub can render its body slot
  // (the Edit button lives there).
  DataTable: {
    props: ['value'],
    components: {
      RowScope: {
        props: ['row'],
        provide() { return { dtRow: this.row }; },
        template: '<div class="dt-row"><slot /></div>',
      },
    },
    template: '<div class="dt"><RowScope v-for="row in value" :key="row.id" :row="row"><slot /></RowScope></div>',
  },
  Column: {
    inject: { dtRow: { default: null } },
    template: '<div><slot v-if="dtRow" name="body" :data="dtRow" /></div>',
  },
  EmptyState: { template: '<div />' },
  Select: {
    props: ['modelValue', 'options', 'optionValue'],
    emits: ['update:modelValue'],
    template: `<select v-bind="$attrs" :value="modelValue"
        @change="$emit('update:modelValue', $event.target.value)">
        <option v-for="o in options" :key="o.value ?? o" :value="o.value ?? o">{{ o.label ?? o }}</option>
      </select>`,
  },
  InputText: vModelInput('input'),
  Textarea: vModelInput('textarea'),
  ToggleSwitch: {
    props: ['modelValue'],
    emits: ['update:modelValue'],
    template: `<input type="checkbox" v-bind="$attrs" :checked="modelValue"
        @change="$emit('update:modelValue', $event.target.checked)" />`,
  },
  Dialog: {
    props: ['visible'],
    template: '<div v-if="visible" class="dlg"><slot /><slot name="footer" /></div>',
  },
  Button: {
    inheritAttrs: false,
    props: ['label'],
    emits: ['click'],
    template: `<button v-bind="$attrs" @click="$emit('click', $event)">{{ label }}</button>`,
  },
};

const existing = {
  id: 'tpl-1',
  name: 'Post-Job NPS',
  kind: 'nps',
  question: 'How likely are you to recommend us?',
  follow_up_question: 'Why?',
  active: true,
};

async function mountView(templates = []) {
  apiGet.mockImplementation((url) => {
    if (url.startsWith('/api/surveys/templates')) return Promise.resolve(templates);
    if (url.startsWith('/api/surveys/responses')) return Promise.resolve([]);
    return Promise.resolve({});
  });
  const wrapper = mount(SurveysView, {
    global: { stubs, directives: { tooltip: {} } },
  });
  await flushPromises();
  return wrapper;
}

const clickButton = async (wrapper, label) => {
  await wrapper.findAll('button').find((b) => b.text() === label).trigger('click');
  await flushPromises();
};

describe('SurveysView template form', () => {
  beforeEach(() => {
    apiGet.mockReset();
    apiPost.mockReset().mockResolvedValue({});
    apiPatch.mockReset().mockResolvedValue({});
  });

  it('creates with only the SurveyTemplateIn keys', async () => {
    const wrapper = await mountView();
    await clickButton(wrapper, '+ Template');

    await wrapper.find('.dlg input').setValue('Post-Job CSAT');
    await wrapper.find('[data-testid="survey-template-kind"]').setValue('csat');
    await wrapper.find('[data-testid="survey-template-question"]').setValue('  How satisfied were you?  ');
    await wrapper.find('[data-testid="survey-template-save"]').trigger('click');
    await flushPromises();

    expect(apiPost).toHaveBeenCalledTimes(1);
    const [url, payload] = apiPost.mock.calls[0];
    expect(url).toBe('/api/surveys/templates');
    expect(Object.keys(payload).every((k) => SCHEMA_FIELDS.includes(k))).toBe(true);
    expect(payload).toEqual({
      name: 'Post-Job CSAT',
      kind: 'csat',
      question: 'How satisfied were you?',
      follow_up_question: null,
      active: true,
    });
  });

  it('refuses a template with no question, visibly', async () => {
    const wrapper = await mountView();
    await clickButton(wrapper, '+ Template');
    await wrapper.find('.dlg input').setValue('No question');
    await wrapper.find('[data-testid="survey-template-save"]').trigger('click');
    await flushPromises();
    expect(apiPost).not.toHaveBeenCalled();
    // Refused out loud, not a click that does nothing.
    expect(wrapper.find('[data-testid="survey-template-error"]').text())
      .toBe('A template needs a name and a question.');
  });

  it('prefills edit from kind/question and patches the schema keys', async () => {
    const wrapper = await mountView([existing]);
    await wrapper.find('button[aria-label="Edit"]').trigger('click');
    await flushPromises();

    expect(wrapper.find('[data-testid="survey-template-question"]').element.value)
      .toBe('How likely are you to recommend us?');
    expect(wrapper.find('[data-testid="survey-template-follow-up"]').element.value).toBe('Why?');

    await wrapper.find('[data-testid="survey-template-kind"]').setValue('custom');
    await wrapper.find('[data-testid="survey-template-active"]').setValue(false);
    await wrapper.find('[data-testid="survey-template-save"]').trigger('click');
    await flushPromises();

    const [url, payload] = apiPatch.mock.calls[0];
    expect(url).toBe('/api/surveys/templates/tpl-1');
    expect(Object.keys(payload).sort()).toEqual([...SCHEMA_FIELDS].sort());
    expect(payload.kind).toBe('custom');
    expect(payload.question).toBe('How likely are you to recommend us?');
    expect(payload.active).toBe(false);
  });
});
