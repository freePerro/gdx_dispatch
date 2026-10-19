/**
 * SurveysView speaks routers/surveys.py (GDXA-1693, sibling of ReferralsView).
 *
 * The template form sent {template_type, send_channel, message}; the router's
 * SurveyTemplateIn requires `question` and calls the type `kind`, so create was
 * a 422 every time and an edit kept only the name. The metrics card read
 * `csat_score` (the API says `csat_avg`) and printed the 0..1 response rate as
 * a percent. These tests read the router source, so either side drifting fails.
 */
import { flushPromises, mount } from '@vue/test-utils';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mockGet = vi.fn();
const mockPost = vi.fn();
const mockPatch = vi.fn();

vi.mock('../../composables/useApiWithToast', () => ({
  useApiWithToast: () => ({ get: mockGet, post: mockPost, patch: mockPatch, del: vi.fn() }),
}));

import SurveysView from '../SurveysView.vue';

const PY = readFileSync(
  join(__dirname, '..', '..', '..', '..', 'routers', 'surveys.py'),
  'utf8',
);

/** Field lines of a pydantic model: { name, required }. */
function modelFields(name) {
  const at = PY.indexOf(`class ${name}(`);
  expect(at, `${name} not found`).toBeGreaterThan(-1);
  const body = PY.slice(at).split('\n').slice(1);
  const fields = [];
  for (const line of body) {
    if (line.trim() && !line.startsWith(' ')) break;
    const m = line.match(/^ {4}(\w+): ([^=]+?)(?: = (.*))?$/);
    if (!m || m[1] === 'model_config') continue;
    const dflt = m[3] || '';
    fields.push({ name: m[1], required: !dflt || /^Field\((?!.*default=)/.test(dflt) });
  }
  return fields;
}

function pyFunc(name) {
  const at = PY.indexOf(`def ${name}(`);
  expect(at, `${name} not found`).toBeGreaterThan(-1);
  return PY.slice(at, PY.indexOf('\ndef ', at + 1));
}

const stub = { template: '<div><slot /><slot name="footer" /></div>' };

function mountView() {
  return mount(SurveysView, {
    global: {
      stubs: {
        Toolbar: stub, Dialog: stub, Card: stub,
        Tabs: true, TabList: true, Tab: true,
        DataTable: true, Column: true, ProgressSpinner: true, EmptyState: true,
        Button: true, InputText: true, Select: true, Textarea: true, ToggleSwitch: true,
      },
      directives: { tooltip: {} },
    },
  });
}

const TEMPLATE = { id: 't1', name: 'Post job', kind: 'csat', question: 'How did we do?', follow_up_question: null, active: true };

function routeGet(metrics = {}) {
  mockGet.mockImplementation((url) => {
    if (url === '/api/surveys/templates') return Promise.resolve([TEMPLATE]);
    if (url.startsWith('/api/surveys/responses')) return Promise.resolve([]);
    if (url.startsWith('/api/surveys/metrics')) return Promise.resolve(metrics);
    return Promise.reject(new Error(`unexpected ${url}`));
  });
}

describe('SurveysView template contract', () => {
  beforeEach(() => {
    mockGet.mockReset();
    mockPost.mockReset();
    mockPatch.mockReset();
  });

  it('creates with exactly SurveyTemplateIn fields, every required one set', async () => {
    routeGet();
    mockPost.mockResolvedValue({});
    const wrapper = mountView();
    await flushPromises();

    const vm = wrapper.vm;
    vm.openTemplateDialog();
    vm.templateForm.name = ' NPS ';
    vm.templateForm.question = ' Would you recommend us? ';
    await vm.saveTemplate();
    await flushPromises();

    const [url, body] = mockPost.mock.calls[0];
    expect(url).toBe('/api/surveys/templates');
    const fields = modelFields('SurveyTemplateIn');
    const declared = fields.map((f) => f.name).sort();
    expect(Object.keys(body).sort()).toEqual(declared);
    for (const f of fields.filter((x) => x.required)) expect(body[f.name], f.name).toBeTruthy();
    expect(body).toMatchObject({ name: 'NPS', kind: 'nps', question: 'Would you recommend us?', follow_up_question: null, active: true });
  });

  it('will not post a template without a question', async () => {
    routeGet();
    const wrapper = mountView();
    await flushPromises();

    wrapper.vm.openTemplateDialog();
    wrapper.vm.templateForm.name = 'NPS';
    await wrapper.vm.saveTemplate();
    expect(mockPost).not.toHaveBeenCalled();
  });

  it('edits through SurveyTemplatePatch fields, round-tripping what the list returned', async () => {
    routeGet();
    mockPatch.mockResolvedValue({});
    const wrapper = mountView();
    await flushPromises();

    wrapper.vm.openEditTemplate(TEMPLATE);
    await wrapper.vm.saveTemplate();
    await flushPromises();

    const [url, body] = mockPatch.mock.calls[0];
    expect(url).toBe('/api/surveys/templates/t1');
    const patchFields = modelFields('SurveyTemplatePatch').map((f) => f.name).sort();
    expect(Object.keys(body).sort()).toEqual(patchFields);
    expect(body).toMatchObject({ kind: 'csat', question: 'How did we do?' });
    for (const key of patchFields) expect(pyFunc('_serialize_template')).toContain(`"${key}":`);
  });

  it('offers exactly the kinds the router pattern allows', () => {
    routeGet();
    const wrapper = mountView();
    const m = PY.match(/kind: str = Field\(default="nps", pattern=r"\^\(([^)]*)\)\$"\)/);
    expect(m, 'kind pattern not found').toBeTruthy();
    expect(wrapper.vm.templateKindOptions.map((o) => o.value).sort()).toEqual(m[1].split('|').sort());
  });

  it('templates table reads only keys _serialize_template returns', () => {
    const SFC = readFileSync(join(__dirname, '..', 'SurveysView.vue'), 'utf8');
    const table = SFC.slice(SFC.indexOf(':value="templates"'));
    const region = table.slice(0, table.indexOf('</DataTable>'));
    const keys = new Set([
      ...[...region.matchAll(/field="(\w+)"/g)].map((m) => m[1]),
      ...[...region.matchAll(/slotProps\.data\.(\w+)/g)].map((m) => m[1]),
    ]);
    expect(keys.size).toBeGreaterThan(0);
    const serializer = pyFunc('_serialize_template');
    for (const key of keys) expect(serializer, key).toContain(`"${key}":`);
  });

  it('labels the template kind and falls back to the raw value', () => {
    routeGet();
    const wrapper = mountView();
    expect(wrapper.vm.kindLabel('csat')).toBe('CSAT (1–5)');
    expect(wrapper.vm.kindLabel('legacy')).toBe('legacy');
    expect(wrapper.vm.kindLabel(null)).toBe('—');
  });

  it('reads metric keys the metrics endpoint returns', async () => {
    routeGet({ total_sent: 4, total_responded: 1, response_rate: 0.25, nps_score: 50, csat_avg: 4.5 });
    const wrapper = mountView();
    await flushPromises();
    wrapper.vm.activeTab = 'metrics';
    await flushPromises();

    const fn = pyFunc('survey_metrics');
    for (const key of ['total_sent', 'total_responded', 'response_rate', 'nps_score', 'csat_avg']) {
      expect(fn, key).toContain(`"${key}":`);
    }
    const text = wrapper.text();
    expect(text).toContain('4.5');
    expect(text).toContain('25%');
  });
});
