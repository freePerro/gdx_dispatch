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
          <!-- Search — shown when the list screen declares `search`. The term
               goes to the SERVER, so it searches the whole table rather than
               whichever page happened to load. -->
          <div v-if="screen.type === 'list' && screen.search" class="plugin-screen__searchbar">
            <InputText
              :model-value="searchTerms[i] || ''"
              :placeholder="screen.search.placeholder || 'Search…'"
              size="small"
              data-testid="plugin-search"
              @update:model-value="(v) => onSearch(i, screen, v)"
              @keyup.enter="searchList(screen, searchTerms[i] || '')"
            />
            <small v-if="!loading">{{ rowsFor(screen).length }} shown</small>
          </div>

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
            <Column v-for="c in screen.columns" :key="c.field" :field="c.field" :header="c.label">
              <template #body="{ data }">
                <!-- Below the mobile breakpoint the header row is hidden and each
                     row becomes a card, so every cell has to carry its own label
                     (the plugin declares it — this renderer can't hardcode
                     nth-child labels the way a fixed table can). Hidden on
                     desktop, where the real <thead> is doing that job. -->
                <span class="plugin-screen__cell-label">{{ c.label }}</span>{{ cellValue(data, c.field) }}
              </template>
            </Column>
            <template v-if="screen.detail_endpoint" #footer>
              <small>{{ isMobileViewport ? 'Tap' : 'Click' }} a row to see everything captured.</small>
            </template>
          </DataTable>

          <!-- Phase 2 (ADR-014): streamed headless browser, gated by the "browser"
               permission + owner consent on the backend. -->
          <BrowserStream
            v-if="screen.type === 'browser' && !isMobileViewport"
            :plugin-key="pluginKey"
            :url="screen.url"
            :capture-endpoint="screen.capture_endpoint || ''"
            :capture-label="screen.capture_label || 'Capture this page'"
            :folders-endpoint="screen.folders_endpoint || ''"
            @captured="onStreamCaptured"
          />
          <!-- Same screen on a phone. The stream is a full-size remote page
               (1280x800) driven by hand — scaled into a phone viewport the text
               is unreadable, and it opens a WebSocket + a server-side browser to
               get there. Say what's happening instead of connecting: the other
               tabs of this plugin still work here. -->
          <div
            v-else-if="screen.type === 'browser'"
            class="plugin-screen__desktop-only"
            data-testid="plugin-screen-desktop-only"
          >
            <i class="pi pi-desktop plugin-screen__desktop-only-icon" aria-hidden="true" />
            <p class="plugin-screen__desktop-only-title">This screen needs a computer</p>
            <p class="plugin-screen__hint">
              {{ screen.title || 'It' }} streams a full-size web page that you drive by hand.
              At phone width it's too small to read or click accurately, so it isn't
              opened here. The other tabs on this plugin work fine on a phone.
            </p>
          </div>

          <!-- Settings screen: per-field toggles, plus an optional ordered field
               list. GET endpoint -> {fields:[{name,on_quote}],
               ordered?:{title,hint,selected:[names],candidates:[names]}};
               Save PUTs {fields:[names that are on], ordered?:[names in order]}. -->
          <div v-if="screen.type === 'settings'" class="plugin-screen__settings">
            <p v-if="screen.description" class="plugin-screen__hint">{{ screen.description }}</p>
            <div v-for="f in settingsFields" :key="f.name" class="plugin-screen__toggle">
              <Checkbox :inputId="`set-${f.name}`" v-model="f.on" :binary="true" />
              <label :for="`set-${f.name}`">{{ f.name }}</label>
            </div>
            <div v-if="orderedGroup" class="plugin-screen__ordered">
              <h4>{{ orderedGroup.title || 'Field order' }}</h4>
              <p v-if="orderedGroup.hint" class="plugin-screen__hint">{{ orderedGroup.hint }}</p>
              <div v-for="(name, idx) in orderedGroup.selected" :key="name" class="plugin-screen__ordered-row">
                <span class="plugin-screen__ordered-name">{{ idx + 1 }}. {{ name }}</span>
                <Button icon="pi pi-arrow-up" text size="small" :disabled="idx === 0"
                  :aria-label="`Move ${name} up`" @click="moveOrdered(idx, -1)" />
                <Button icon="pi pi-arrow-down" text size="small"
                  :disabled="idx === orderedGroup.selected.length - 1"
                  :aria-label="`Move ${name} down`" @click="moveOrdered(idx, 1)" />
                <Button icon="pi pi-times" text size="small" severity="danger"
                  :aria-label="`Remove ${name}`" @click="removeOrdered(idx)" />
              </div>
              <Select v-model="orderedAdd" :options="orderedCandidates" placeholder="Add a field…"
                size="small" class="plugin-screen__ordered-add" @change="addOrdered" />
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
            <!-- optional labeled measurement diagram: renders its fields on the
                 picture; those fields are then skipped in the normal field list. -->
            <MeasurementDiagram
              v-if="screen.create.diagram === 'garage_opening'"
              :state="formState"
            />
            <span v-for="f in screen.create.fields"
                  v-show="isFieldVisible(i, f) && !_inDiagram(screen, f)" :key="f.name" class="p-field">
              <label :for="`pf-${f.name}`">{{ f.label }}</label>
              <!-- select: options from the plugin's (validated) options_endpoint;
                   dependent selects refetch when a field they depend on changes. -->
              <Select
                v-if="f.type === 'select'"
                :inputId="`pf-${f.name}`"
                v-model="formState[f.name]"
                :options="fieldOptions[`${i}:${f.name}`] || f.options || []"
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
    <Dialog
      v-model:visible="detailVisible"
      :header="detailTitle"
      modal
      :style="{ width: '46rem' }"
      :breakpoints="{ '768px': '95vw' }"
    >
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
import MeasurementDiagram from './MeasurementDiagram.vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { cellValue, usePluginScreen } from '../composables/usePluginScreen';
import { useViewMode } from '../composables/useViewMode';

const props = defineProps({ pluginKey: { type: String, required: true } });

// Phase 3 (ADR-013): forward a completed CAPTURE with its payload so an
// embedding host (the estimate screen) can auto-insert it. Capture-only by
// the plan's scoping rule — create-form submissions do NOT emit, because a
// configurator plugin's other create forms (e.g. settings rows) are not
// insertable things, and auto-inserting them would be nonsense.
const emit = defineEmits(['captured']);

function onStreamCaptured(payload) {
  load();                       // what @captured="load" always did
  emit('captured', payload);    // NEW: the payload used to be discarded here
}

// Phone viewport — the shared 768px media query from useViewMode (viewport
// only; the stored desktop/mobile preference does NOT feed this flag). Layout
// is CSS below; this drives the two decisions CSS can't make: don't open a
// browser-stream socket that can't be used at this width, and label the row-tap
// affordance correctly.
const { isMobileViewport } = useViewMode();

const api = useApiWithToast();
const { screens, rows, rowsFor, loading, error, load, create, fetchOptions, searchList } = usePluginScreen(props.pluginKey, api);

// Search term per screen index, debounced so typing does not fire a request per
// keystroke against the plugin host.
const searchTerms = reactive({});
const _searchTimers = {};
function onSearch(i, screen, value) {
  searchTerms[i] = value;
  clearTimeout(_searchTimers[i]);
  _searchTimers[i] = setTimeout(() => searchList(screen, value), 250);
}
const formState = reactive({});
// Select/autocomplete options per create field, keyed `${screenIndex}:${fieldName}`.
const fieldOptions = reactive({});
const settingsFields = ref([]);   // [{ name, on }]
const savingSettings = ref(false);
// Optional ordered field list on the settings screen (e.g. "these fields, in
// this order, compose a line description") — present only when the plugin's
// settings GET returns an `ordered` group.
const orderedGroup = ref(null);   // { title, hint, selected: [...], candidates: [...] }
const orderedAdd = ref(null);     // Select model for appending a candidate
const orderedCandidates = computed(() => {
  const g = orderedGroup.value;
  if (!g) return [];
  const sel = new Set(g.selected);
  return (g.candidates || []).filter((n) => !sel.has(n));
});
function moveOrdered(idx, delta) {
  const sel = orderedGroup.value?.selected || [];
  const j = idx + delta;
  if (j < 0 || j >= sel.length) return;
  [sel[idx], sel[j]] = [sel[j], sel[idx]];
}
function removeOrdered(idx) {
  orderedGroup.value?.selected.splice(idx, 1);
}
function addOrdered() {
  if (orderedAdd.value && orderedGroup.value) orderedGroup.value.selected.push(orderedAdd.value);
  orderedAdd.value = null;
}
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

// Progressive disclosure — an "order it step by step" workflow. A dependent
// select is revealed only once it actually has options to offer (its plugin
// endpoint returns [] until its prerequisites are met, e.g. a window design has
// no options until a panel design is chosen; a size has none for a part). This
// makes each choice unlock the next relevant one and hides fields that don't
// apply to the current path. Base fields (no depends_on) are always shown.
// Measurement fields rendered inside a diagram are skipped in the normal list.
const _DIAGRAM_FIELDS = { garage_opening: ['opening_w', 'opening_h', 'ceiling'] };
function _inDiagram(screen, f) {
  const set = _DIAGRAM_FIELDS[screen?.create?.diagram];
  return !!set && set.includes(f.name);
}

function isFieldVisible(si, f) {
  const deps = Array.isArray(f.depends_on) ? f.depends_on : [];
  if (!deps.length) return true;
  if (f.type === 'select' && f.options_endpoint) {
    const opts = fieldOptions[`${si}:${f.name}`];
    return Array.isArray(opts) && opts.length > 0;
  }
  // dependent non-selects (e.g. an add-on qty) show once their deps are filled
  return deps.every((d) => formState[d] !== undefined && formState[d] !== null && formState[d] !== '');
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
  const g = data?.ordered;
  orderedGroup.value = g && Array.isArray(g.candidates)
    ? { title: g.title || '', hint: g.hint || '', selected: [...(g.selected || [])], candidates: [...g.candidates] }
    : null;
}

async function saveSettings(screen) {
  savingSettings.value = true;
  try {
    const body = { fields: settingsFields.value.filter((f) => f.on).map((f) => f.name) };
    if (orderedGroup.value) body.ordered = [...orderedGroup.value.selected];
    await api.put(screen.endpoint, body, { successMessage: 'Saved' });
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
.plugin-screen__ordered { margin: 1rem 0 0.75rem; }
.plugin-screen__ordered h4 { margin: 0 0 0.25rem; color: var(--p-text-color, #1f2937); }
.plugin-screen__ordered-row { display: flex; align-items: center; gap: 0.25rem; }
.plugin-screen__ordered-name { min-width: 12rem; color: var(--p-text-color, #1f2937); }
.plugin-screen__ordered-add { margin-top: 0.35rem; min-width: 12rem; }
.plugin-screen__help { max-width: 60rem; color: var(--p-text-color, #1f2937); }
.plugin-screen__help-sec { margin-bottom: 1.25rem; }
.plugin-screen__help-sec h4 { margin: 0 0 0.4rem; }
.plugin-screen__help-p { margin: 0 0 0.4rem; line-height: 1.5; }
.plugin-screen__help-bullet { margin: 0 0 0.25rem; padding-left: 1.1rem; position: relative; line-height: 1.5; }
.plugin-screen__help-bullet::before { content: "•"; position: absolute; left: 0.2rem; color: var(--p-text-color-secondary, #6b7280); }

/* The per-cell label only exists for the mobile card layout; the real <thead>
   labels the columns everywhere else. */
.plugin-screen__cell-label { display: none; }

.plugin-screen__desktop-only {
  display: flex; flex-direction: column; align-items: center; text-align: center;
  gap: 0.4rem; padding: 2rem 1rem; max-width: 34rem; margin: 0 auto;
  border: 1px dashed var(--p-content-border-color, #e5e7eb); border-radius: 0.6rem;
}
.plugin-screen__desktop-only-icon { font-size: 1.6rem; color: var(--p-text-color-secondary, #6b7280); }
.plugin-screen__desktop-only-title { margin: 0; font-weight: 600; color: var(--p-text-color, #1f2937); }
.plugin-screen__desktop-only .plugin-screen__hint { margin: 0; }

/* ── phone layout ────────────────────────────────────────────────────────────
   A plugin declares its columns/fields; this renderer can't know how many or
   how wide, so on a phone the generic table + fixed-width form rows overflow
   (audit P1 #3, the systemic wide-table finding). Same treatment the hand-built
   mobile companions use — hide the header, stack each row into a card, and let
   every fixed width go fluid. Scoped to the breakpoint so the desktop layout
   these plugins are used on every day is untouched. */
@media (max-width: 768px) {
  /* Tab strip: 4 tabs (Workspace / Captured / Settings / Help) don't fit at
     390px — scroll them instead of squeezing or clipping. */
  .plugin-screen :deep(.p-tablist-tab-list) { overflow-x: auto; }
  .plugin-screen :deep(.p-tab) { white-space: nowrap; }
  .plugin-screen__screen { padding-inline: 0; }

  /* List → card stack. */
  .plugin-screen :deep(.p-datatable-thead) { display: none; }
  .plugin-screen :deep(.p-datatable-tbody > tr) {
    display: flex; flex-direction: column; gap: 0.2rem;
    border: 1px solid var(--p-content-border-color, #e5e7eb);
    border-radius: 0.55rem; margin-bottom: 0.45rem; padding: 0.6rem 0.75rem;
  }
  .plugin-screen :deep(.p-datatable-tbody > tr > td) {
    border: 0; padding: 0.1rem 0; width: 100% !important; text-align: left;
  }
  .plugin-screen__cell-label {
    display: inline; margin-right: 0.4rem; font-weight: 600;
    color: var(--p-text-color-secondary, #6b7280);
  }

  /* Folder filter + create form: one full-width control per row. */
  .plugin-screen__folderbar { flex-wrap: wrap; }
  .plugin-screen__folderbar :deep(.p-select) { flex: 1 1 100%; }
  .plugin-screen__create { display: flex; flex-direction: column; gap: 0.75rem; }
  .plugin-screen__create .p-field { display: flex; flex-direction: column; gap: 0.25rem; }
  .plugin-screen__create :deep(.p-select),
  .plugin-screen__create :deep(.p-inputnumber),
  .plugin-screen__create :deep(input) { width: 100%; }

  /* Ordered-field rows: a 12rem name + three buttons is ~18rem of fixed width. */
  .plugin-screen__ordered-row { flex-wrap: wrap; }
  .plugin-screen__ordered-name { min-width: 0; flex: 1 1 100%; }
  .plugin-screen__ordered-add { width: 100%; }

  /* Detail dialog contents (the Dialog itself goes to 95vw via :breakpoints). */
  .plugin-screen__kv th, .plugin-screen__kv td { display: block; width: 100%; }
  .plugin-screen__kv th { white-space: normal; padding: 0.35rem 0 0; }
}

/* Search bar for list screens that declare it. */
.plugin-screen__searchbar {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.75rem;
}
.plugin-screen__searchbar :deep(input) { min-width: min(340px, 100%); }
.plugin-screen__searchbar small { color: var(--p-text-muted-color, #6b7280); white-space: nowrap; }
</style>
