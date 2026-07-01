<template>
  <div class="plugin-screen" data-testid="plugin-screen">
    <p v-if="error" class="plugin-screen__error">{{ error }}</p>

    <!-- One tab per manifest screen (Workspace / Captured / Settings / …). Panels
         stay mounted (v-show, not v-if) so the browser-stream WebSocket survives a
         switch to Settings and back — the operator doesn't lose their login. -->
    <Tabs v-if="screens.length" v-model:value="activeTab">
      <TabList>
        <Tab v-for="(screen, i) in screens" :key="i" :value="String(i)">{{ screen.title }}</Tab>
      </TabList>
      <TabPanels>
        <TabPanel v-for="(screen, i) in screens" :key="i" :value="String(i)" class="plugin-screen__screen">
          <!-- Folder filter — shown when the list carries a folder column. -->
          <div v-if="screen.type === 'list' && _hasFolders(screen)" class="plugin-screen__folderbar">
            <label>Folder</label>
            <Select v-model="listFolder" :options="folderChoices" showClear
              placeholder="All folders" size="small" />
          </div>
          <DataTable
            v-if="screen.type === 'list'"
            :value="_filteredRows(screen)"
            :loading="loading"
            dataKey="id"
            :rowHover="!!screen.detail_endpoint"
            @row-click="screen.detail_endpoint && onRowClick(screen, $event)"
          >
            <Column v-for="c in screen.columns" :key="c.field" :field="c.field" :header="c.label" />
            <template v-if="screen.detail_endpoint" #footer>
              <small>Click a row to see everything captured.</small>
            </template>
          </DataTable>

          <!-- Phase 2 (ADR-014): streamed headless browser, gated by the "browser"
               permission + owner consent on the backend. -->
          <BrowserStream
            v-if="screen.type === 'browser'"
            :plugin-key="pluginKey"
            :url="screen.url"
            :capture-endpoint="screen.capture_endpoint || ''"
            :capture-label="screen.capture_label || 'Capture this page'"
            :folders-endpoint="screen.folders_endpoint || ''"
            @captured="load"
          />

          <!-- Settings screen: per-field toggles. GET endpoint -> {fields:[{name,on_quote}]};
               Save PUTs {fields:[names that are on]}. -->
          <div v-if="screen.type === 'settings'" class="plugin-screen__settings">
            <p v-if="screen.description" class="plugin-screen__hint">{{ screen.description }}</p>
            <div v-for="f in settingsFields" :key="f.name" class="plugin-screen__toggle">
              <Checkbox :inputId="`set-${f.name}`" v-model="f.on" :binary="true" />
              <label :for="`set-${f.name}`">{{ f.name }}</label>
            </div>
            <Button label="Save" size="small" :loading="savingSettings" @click="saveSettings(screen)" />
          </div>

          <!-- Help screen: documentation as sections of headings + text/bullets.
               Plain text only (no raw HTML) — safe + theme-aware. -->
          <div v-if="screen.type === 'help'" class="plugin-screen__help">
            <section v-for="(sec, i) in screen.sections" :key="i" class="plugin-screen__help-sec">
              <h4>{{ sec.heading }}</h4>
              <p v-for="(b, j) in sec.body" :key="j"
                :class="b.startsWith('- ') ? 'plugin-screen__help-bullet' : 'plugin-screen__help-p'">
                {{ b.startsWith('- ') ? b.slice(2) : b }}
              </p>
            </section>
          </div>

          <form
            v-if="screen.create"
            class="plugin-screen__create"
            @submit.prevent="onCreate(screen)"
          >
            <span v-for="f in screen.create.fields" :key="f.name" class="p-field">
              <label :for="`pf-${f.name}`">{{ f.label }}</label>
              <!-- select: options from the plugin's (validated) options_endpoint;
                   dependent selects refetch when a field they depend on changes. -->
              <Select
                v-if="f.type === 'select'"
                :inputId="`pf-${f.name}`"
                v-model="formState[f.name]"
                :options="fieldOptions[`${i}:${f.name}`] || []"
                optionLabel="label"
                optionValue="value"
                :filter="!!f.filter"
                showClear
                :placeholder="f.label"
                size="small"
                @change="onFieldChange(i, f)"
              />
              <InputNumber
                v-else-if="f.type === 'number'"
                :inputId="`pf-${f.name}`"
                v-model="formState[f.name]"
                :min="f.min ?? 0"
                showButtons
                size="small"
              />
              <InputText v-else :id="`pf-${f.name}`" v-model="formState[f.name]" :required="f.required" />
            </span>
            <Button type="submit" label="Add" size="small" />
          </form>
        </TabPanel>
      </TabPanels>
    </Tabs>

    <!-- Row detail: everything the capture saved, grouped into the sections the
         plugin's detail endpoint returns (e.g. Quote / Installer / Receiving). -->
    <Dialog v-model:visible="detailVisible" :header="detailTitle" modal :style="{ width: '46rem' }">
      <p v-if="detailLoading">Loading…</p>
      <div v-for="sec in detailSections" :key="sec.title" class="plugin-screen__detail-sec">
        <h4>{{ sec.title }}</h4>
        <img v-if="sec.image" :src="sec.image" alt="Door" class="plugin-screen__photo" />
        <pre v-else-if="sec.text" class="plugin-screen__raw">{{ sec.text }}</pre>
        <table v-else-if="sec.rows.length" class="plugin-screen__kv">
          <tbody>
            <tr v-for="r in sec.rows" :key="r.k">
              <th>{{ r.k }}</th><td>{{ r.v }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="plugin-screen__hint">—</p>
      </div>
    </Dialog>
  </div>
</template>

<script setup>
// Generic host renderer for a plugin's declarative UI manifest (ADR-013 step 4).
// All logic lives in usePluginScreen so it's unit-tested without the DOM; this
// component is the PrimeVue template over it. No plugin-supplied JavaScript ever
// runs in the browser — only data the host renders.
import { computed, onMounted, reactive, ref } from 'vue';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import InputText from 'primevue/inputtext';
import Checkbox from 'primevue/checkbox';
import Button from 'primevue/button';
import Tabs from 'primevue/tabs';
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import TabPanels from 'primevue/tabpanels';
import TabPanel from 'primevue/tabpanel';
import Dialog from 'primevue/dialog';
import Select from 'primevue/select';
import InputNumber from 'primevue/inputnumber';
import BrowserStream from './BrowserStream.vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { usePluginScreen } from '../composables/usePluginScreen';

const props = defineProps({ pluginKey: { type: String, required: true } });

const api = useApiWithToast();
const { screens, rows, rowsFor, loading, error, load, create, fetchOptions } = usePluginScreen(props.pluginKey, api);
const formState = reactive({});
// Select/autocomplete options per create field, keyed `${screenIndex}:${fieldName}`.
const fieldOptions = reactive({});
const settingsFields = ref([]);   // [{ name, on }]
const savingSettings = ref(false);
const activeTab = ref('0');       // index of the open tab (string, PrimeVue Tabs)

// Folder filter for a list screen that carries a folder column (e.g. captures).
const listFolder = ref(null);
const folderChoices = computed(
  () => [...new Set((rows.value || []).map((r) => r.folder).filter(Boolean))].sort(),
);
function _hasFolders(screen) {
  return Array.isArray(screen.columns) && screen.columns.some((c) => c.field === 'folder');
}
function _filteredRows(screen) {
  const list = rowsFor(screen);
  if (_hasFolders(screen) && listFolder.value) {
    return list.filter((r) => r.folder === listFolder.value);
  }
  return list;
}

// Row detail dialog (captured-quote "show everything").
const detailVisible = ref(false);
const detailLoading = ref(false);
const detailTitle = ref('');
const detailSections = ref([]);   // [{ title, rows: [{k,v}], text }]

// Turn a detail object {Section: {k:v} | "text" | [...]} into renderable sections.
// Scalars/strings render as a text block; objects render as key/value rows with
// nested values stringified so nothing the plugin saved is hidden.
function buildSections(obj) {
  if (!obj || typeof obj !== 'object') return [];
  return Object.entries(obj).map(([title, val]) => {
    if (typeof val === 'string' && val.startsWith('data:image')) {
      return { title, rows: [], text: '', image: val };
    }
    if (val && typeof val === 'object' && !Array.isArray(val)) {
      const rows = Object.entries(val).map(([k, v]) => ({
        k, v: (v !== null && typeof v === 'object') ? JSON.stringify(v) : String(v),
      }));
      return { title, rows, text: '', image: '' };
    }
    return { title, rows: [], image: '', text: Array.isArray(val) ? JSON.stringify(val, null, 2) : String(val) };
  });
}

async function onRowClick(screen, e) {
  const id = e?.data?.id;
  if (id == null) return;
  detailTitle.value = e.data.qcd || `Capture #${id}`;
  detailSections.value = [];
  detailVisible.value = true;
  detailLoading.value = true;
  try {
    const data = await api.get(screen.detail_endpoint.replace('{id}', id));
    detailSections.value = buildSections(data);
  } finally {
    detailLoading.value = false;
  }
}

async function onCreate(screen) {
  // Send only this form's declared fields, then clear them (each create form
  // has its own field set; forms must not leak values into one another).
  const names = (screen?.create?.fields || []).map((f) => f.name);
  const values = Object.fromEntries(names.map((n) => [n, formState[n]]));
  await create(values, screen);
  for (const n of names) delete formState[n];
  applyFieldDefaults(screen);
}

// ── create-form field types (select / number) ──────────────────────────────
function applyFieldDefaults(screen) {
  for (const f of screen?.create?.fields || []) {
    if (f.default !== undefined && formState[f.name] === undefined) formState[f.name] = f.default;
  }
}

async function loadFieldOptions(si, field) {
  const opts = await fetchOptions(field, formState);
  if (opts !== null) fieldOptions[`${si}:${field.name}`] = opts;  // null = superseded
}

// A select changed: clear + refetch any field in the same form that depends on it.
function onFieldChange(si, field, screen) {
  const scr = screen || screens.value[si];
  for (const dep of scr?.create?.fields || []) {
    if (Array.isArray(dep.depends_on) && dep.depends_on.includes(field.name)) {
      formState[dep.name] = undefined;
      loadFieldOptions(si, dep);
    }
  }
}

// Initial option load for create fields with a static/independent source.
function initCreateForms() {
  screens.value.forEach((screen, si) => {
    if (!screen?.create?.fields) return;
    applyFieldDefaults(screen);
    for (const f of screen.create.fields) {
      if ((f.type === 'select' || f.type === 'autocomplete')
          && f.options_endpoint && !f.depends_on) {
        loadFieldOptions(si, f);
      }
    }
  });
}

async function loadSettings(screen) {
  const data = await api.get(screen.endpoint);
  settingsFields.value = (data?.fields || []).map((f) => ({ name: f.name, on: !!f.on_quote }));
}

async function saveSettings(screen) {
  savingSettings.value = true;
  try {
    const fields = settingsFields.value.filter((f) => f.on).map((f) => f.name);
    await api.put(screen.endpoint, { fields }, { successMessage: 'Saved' });
  } finally {
    savingSettings.value = false;
  }
}

onMounted(async () => {
  await load();
  initCreateForms();
  const s = screens.value.find((x) => x.type === 'settings');
  if (s?.endpoint) await loadSettings(s);
});
</script>

<style scoped>
.plugin-screen__detail-sec { margin-bottom: 1rem; }
.plugin-screen__detail-sec h4 { margin: 0 0 0.25rem; color: var(--p-text-color, #1f2937); }
.plugin-screen__hint { color: var(--p-text-color-secondary, #6b7280); font-size: 0.85rem; }
.plugin-screen__kv { width: 100%; border-collapse: collapse; }
.plugin-screen__kv th { text-align: left; vertical-align: top; padding: 2px 12px 2px 0; white-space: nowrap; color: var(--p-text-color-secondary, #6b7280); font-weight: 600; }
.plugin-screen__kv td { padding: 2px 0; color: var(--p-text-color, #1f2937); }
.plugin-screen__raw { white-space: pre-wrap; word-break: break-word; max-height: 16rem; overflow: auto; color: var(--p-text-color, #1f2937); background: rgba(128, 128, 128, 0.12); border: 1px solid rgba(128, 128, 128, 0.25); padding: 0.5rem; border-radius: 4px; font-size: 0.8rem; }
.plugin-screen__photo { max-width: 100%; max-height: 22rem; border: 1px solid var(--surface-border, #ccc); border-radius: 4px; }
.plugin-screen__folderbar { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }
.plugin-screen__help { max-width: 60rem; color: var(--p-text-color, #1f2937); }
.plugin-screen__help-sec { margin-bottom: 1.25rem; }
.plugin-screen__help-sec h4 { margin: 0 0 0.4rem; }
.plugin-screen__help-p { margin: 0 0 0.4rem; line-height: 1.5; }
.plugin-screen__help-bullet { margin: 0 0 0.25rem; padding-left: 1.1rem; position: relative; line-height: 1.5; }
.plugin-screen__help-bullet::before { content: "•"; position: absolute; left: 0.2rem; color: var(--p-text-color-secondary, #6b7280); }
</style>
