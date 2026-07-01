<script setup>
import { ref, computed, onMounted } from 'vue'
import Toolbar from 'primevue/toolbar'
import Button from 'primevue/button'
import InputNumber from 'primevue/inputnumber'
import ToggleSwitch from 'primevue/toggleswitch'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import Tabs from 'primevue/tabs'
import TabList from 'primevue/tablist'
import Tab from 'primevue/tab'
import TabPanels from 'primevue/tabpanels'
import TabPanel from 'primevue/tabpanel'
import Message from 'primevue/message'
import InputText from 'primevue/inputtext'
import ColorPicker from 'primevue/colorpicker'
import Dialog from 'primevue/dialog'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../../composables/useApi'

// Sprint tech_mobile S1-Z4 — admin page for per-tenant tech-mobile feature
// settings. The catalog (returned by the GET) drives field rendering; type
// "bool" → ToggleSwitch, "int" → InputNumber, "enum" → Select. Phase field
// drives tab grouping. Save fires a per-key PUT so each change gets its own
// audit row (S1-Z5).

const api = useApi()
const toast = useToast()

const loading = ref(false)
const saving = ref({})
const error = ref(null)

const catalog = ref([])
const overrides = ref({})
const resolved = ref({})

const TAGS_TAB = 'tags'

const PHASE_LABELS = {
  '1.1': "Phase 1.1 — Today's Route",
  '1.2': 'Phase 1.2 — Arrival & On-Site',
  '1.3': 'Phase 1.3 — Parts',
  '1.4': 'Phase 1.4 — Multi-Tech',
  '1.5': 'Phase 1.5 — Push',
  '2.1': 'Sprint 2 — Quoting',
  '3.1': 'Sprint 3 — Offline',
  '5.1': 'Sprint 5 — History',
  '5.2': 'Sprint 5 — Diagnosis',
  '5.3': 'Sprint 5 — GPS',
  '6.1': 'Sprint 6 — Time',
  '6.2': 'Sprint 6 — DOT',
}

const groupedCatalog = computed(() => {
  const groups = new Map()
  for (const entry of catalog.value) {
    const phase = entry.phase || 'other'
    if (!groups.has(phase)) groups.set(phase, [])
    groups.get(phase).push(entry)
  }
  return [...groups.entries()].map(([phase, items]) => ({
    phase,
    label: PHASE_LABELS[phase] || `Phase ${phase}`,
    items,
  }))
})

const activeTab = ref('1.1')

function isOverridden(key) {
  return Object.prototype.hasOwnProperty.call(overrides.value, key)
}

function currentValue(key) {
  return resolved.value[key]
}

async function load() {
  loading.value = true
  error.value = null
  try {
    const data = await api.get('/api/admin/feature-settings/tech-mobile')
    catalog.value = data.catalog || []
    overrides.value = data.overrides || {}
    resolved.value = data.resolved || {}
    if (groupedCatalog.value.length > 0) {
      activeTab.value = groupedCatalog.value[0].phase
    }
  } catch (err) {
    error.value = err?.message || 'Failed to load tech-mobile settings'
  } finally {
    loading.value = false
  }
}

async function saveSetting(entry, value) {
  saving.value = { ...saving.value, [entry.key]: true }
  try {
    const result = await api.put('/api/admin/feature-settings/tech-mobile', {
      key: entry.key,
      value,
    })
    overrides.value = { ...overrides.value, [entry.key]: result.after }
    resolved.value = { ...resolved.value, [entry.key]: result.after }
    toast.add({ severity: 'success', summary: 'Saved', detail: entry.label, life: 2500 })
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Save failed',
      detail: err?.message || 'Unknown error',
      life: 5000,
    })
    // Revert the local view of resolved to the catalog default if the
    // server rejected the write, so the form doesn't show a value the
    // backend won't accept.
    resolved.value = { ...resolved.value, [entry.key]: entry.default }
  } finally {
    saving.value = { ...saving.value, [entry.key]: false }
  }
}

async function resetSetting(entry) {
  saving.value = { ...saving.value, [entry.key]: true }
  try {
    await api.del(
      `/api/admin/feature-settings/tech-mobile/${encodeURIComponent(entry.key)}`,
    )
    const next = { ...overrides.value }
    delete next[entry.key]
    overrides.value = next
    resolved.value = { ...resolved.value, [entry.key]: entry.default }
    toast.add({
      severity: 'info',
      summary: 'Reset to default',
      detail: entry.label,
      life: 2500,
    })
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Reset failed',
      detail: err?.message || 'Unknown error',
      life: 5000,
    })
  } finally {
    saving.value = { ...saving.value, [entry.key]: false }
  }
}

function enumOptions(entry) {
  return (entry.bounds || []).map((v) => ({ label: v, value: v }))
}

// ── Tags tab (S1-A8) ──────────────────────────────────────────────────
const tags = ref([])
const tagsLoading = ref(false)
const tagDialogVisible = ref(false)
const tagDialogMode = ref('create') // 'create' | 'edit'
const tagDraft = ref({ id: null, name: '', color: '6366f1', description: '' })

async function loadTags() {
  tagsLoading.value = true
  try {
    const data = await api.get('/api/admin/customer-tags')
    tags.value = data.tags || []
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Failed to load tags',
      detail: err?.message || 'Unknown error',
      life: 4000,
    })
  } finally {
    tagsLoading.value = false
  }
}

function openCreateTag() {
  tagDialogMode.value = 'create'
  tagDraft.value = { id: null, name: '', color: '6366f1', description: '' }
  tagDialogVisible.value = true
}

function openEditTag(tag) {
  tagDialogMode.value = 'edit'
  tagDraft.value = {
    id: tag.id,
    name: tag.name,
    color: (tag.color || '#6366f1').replace(/^#/, ''),
    description: tag.description || '',
  }
  tagDialogVisible.value = true
}

async function saveTag() {
  const payload = {
    name: tagDraft.value.name.trim(),
    color: '#' + (tagDraft.value.color || '6366f1').replace(/^#/, ''),
    description: tagDraft.value.description || null,
  }
  try {
    if (tagDialogMode.value === 'create') {
      await api.post('/api/admin/customer-tags', payload)
      toast.add({ severity: 'success', summary: 'Tag created', detail: payload.name, life: 2500 })
    } else {
      await api.put(`/api/admin/customer-tags/${tagDraft.value.id}`, payload)
      toast.add({ severity: 'success', summary: 'Tag updated', detail: payload.name, life: 2500 })
    }
    tagDialogVisible.value = false
    await loadTags()
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Save failed',
      detail: err?.message || 'Unknown error',
      life: 4000,
    })
  }
}

async function deleteTag(tag) {
  if (
    !(await confirmAsync({ header: 'Confirm', message: `Delete "${tag.name}"? This removes the tag from every customer it\'s assigned to. (Customers and their notes are unaffected.)` }))
  )
    return
  try {
    const result = await api.del(`/api/admin/customer-tags/${tag.id}`)
    toast.add({
      severity: 'info',
      summary: 'Tag deleted',
      detail:
        result?.assignments_removed > 0
          ? `Removed from ${result.assignments_removed} customer${result.assignments_removed === 1 ? '' : 's'}`
          : 'Tag deleted',
      life: 3500,
    })
    await loadTags()
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Delete failed',
      detail: err?.message || 'Unknown error',
      life: 4000,
    })
  }
}

onMounted(async () => {
  await load()
  await loadTags()
})
</script>

<template>
    <section class="tech-mobile-settings view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">Tech Mobile — Feature Settings</h1>
        </template>
        <template #end>
          <Button
            label="Reload"
            icon="pi pi-refresh"
            severity="secondary"
            outlined
            @click="load"
            :loading="loading"
          />
        </template>
      </Toolbar>

      <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>

      <p class="muted">
        These settings control how the tech-mobile experience behaves for this tenant.
        Defaults ship with the platform; any value you change here applies only to your
        tenant. Use <em>Reset</em> to revert to the platform default.
      </p>

      <Tabs v-model:value="activeTab">
        <TabList>
          <Tab v-for="g in groupedCatalog" :key="g.phase" :value="g.phase">
            {{ g.label }}
          </Tab>
          <Tab :value="TAGS_TAB">Customer Alert Tags</Tab>
        </TabList>
        <TabPanels>
          <TabPanel v-for="g in groupedCatalog" :key="g.phase" :value="g.phase">
            <div class="settings-grid">
              <div v-for="entry in g.items" :key="entry.key" class="setting-row">
                <div class="setting-meta">
                  <div class="setting-label">
                    {{ entry.label }}
                    <Tag
                      v-if="isOverridden(entry.key)"
                      severity="info"
                      value="overridden"
                    />
                  </div>
                  <div v-if="entry.help" class="setting-help">{{ entry.help }}</div>
                  <div class="setting-key">{{ entry.key }}</div>
                </div>

                <div class="setting-control">
                  <ToggleSwitch
                    v-if="entry.type === 'bool'"
                    :modelValue="currentValue(entry.key)"
                    :disabled="saving[entry.key]"
                    @update:modelValue="(v) => saveSetting(entry, v)"
                  />

                  <InputNumber
                    v-else-if="entry.type === 'int'"
                    :modelValue="currentValue(entry.key)"
                    :min="entry.bounds ? entry.bounds[0] : undefined"
                    :max="entry.bounds ? entry.bounds[1] : undefined"
                    :disabled="saving[entry.key]"
                    showButtons
                    @update:modelValue="(v) => saveSetting(entry, v)"
                  />

                  <Select
                    v-else-if="entry.type === 'enum'"
                    :modelValue="currentValue(entry.key)"
                    :options="enumOptions(entry)"
                    optionLabel="label"
                    optionValue="value"
                    :disabled="saving[entry.key]"
                    @update:modelValue="(v) => saveSetting(entry, v)"
                  />

                  <span v-else class="muted">unsupported type: {{ entry.type }}</span>

                  <Button
                    v-if="isOverridden(entry.key)"
                    label="Reset"
                    icon="pi pi-undo"
                    severity="secondary"
                    text
                    size="small"
                    :disabled="saving[entry.key]"
                    @click="resetSetting(entry)"
                  />
                </div>
              </div>
            </div>
          </TabPanel>

          <TabPanel :value="TAGS_TAB">
            <div class="tags-toolbar">
              <p class="muted tags-help">
                Customer alert tags surface on each tech's job card (dog warning, gate code, COD-only,
                etc.). Add or rename tags to match how your team thinks. Renames are non-destructive —
                every customer carrying the tag gets the new name instantly. Deleting a tag removes it
                from every customer it's currently on; the customer record itself is unchanged.
              </p>
              <Button
                label="Add tag"
                icon="pi pi-plus"
                size="small"
                @click="openCreateTag"
              />
            </div>

            <div v-if="tagsLoading" class="muted">Loading tags…</div>

            <div v-else-if="tags.length === 0" class="muted">
              No tags yet. Click "Add tag" to create one.
            </div>

            <ul v-else class="tag-list">
              <li v-for="tag in tags" :key="tag.id" class="tag-row">
                <span
                  class="tag-swatch"
                  :style="{ backgroundColor: tag.color }"
                  :title="tag.color"
                />
                <div class="tag-meta">
                  <div class="tag-name">{{ tag.name }}</div>
                  <div v-if="tag.description" class="tag-desc">{{ tag.description }}</div>
                </div>
                <Button
                  v-tooltip="'Edit'"
                  icon="pi pi-pencil"
                  text
                  rounded
                  aria-label="Edit"
                  @click="openEditTag(tag)"
                />
                <Button
                  v-tooltip="'Delete'"
                  icon="pi pi-trash"
                  text
                  rounded
                  severity="danger"
                  aria-label="Delete"
                  @click="deleteTag(tag)"
                />
              </li>
            </ul>
          </TabPanel>
        </TabPanels>
      </Tabs>

      <Dialog
        v-model:visible="tagDialogVisible"
        :header="tagDialogMode === 'create' ? 'New customer tag' : 'Edit customer tag'"
        modal
        :style="{ width: '420px' }"
      >
        <div class="tag-form">
          <label class="tag-form-label">
            <span>Name</span>
            <InputText
              v-model="tagDraft.name"
              placeholder="dog_warning"
              :disabled="tagDialogMode === 'edit' && tagDraft.id !== null && false"
            />
            <small class="muted">Lowercase letters, digits, underscores only.</small>
          </label>
          <label class="tag-form-label">
            <span>Color</span>
            <ColorPicker v-model="tagDraft.color" format="hex" />
          </label>
          <label class="tag-form-label">
            <span>Description</span>
            <InputText
              v-model="tagDraft.description"
              placeholder="Optional context for techs"
            />
          </label>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="tagDialogVisible = false" />
          <Button label="Save" @click="saveTag" />
        </template>
      </Dialog>
    </section>
</template>

<style scoped>
.tech-mobile-settings {
  padding: 1rem 1.25rem;
}
.view-heading {
  font-size: 1.25rem;
  font-weight: 600;
  margin: 0;
}
.muted {
  color: var(--p-text-muted-color, #6b7280);
  font-size: 0.9rem;
}
.settings-grid {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  padding: 0.5rem 0;
}
.setting-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 1rem;
  padding: 0.75rem 0;
  border-bottom: 1px solid var(--p-content-border-color, #e5e7eb);
  align-items: start;
}
.setting-meta {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.setting-label {
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.setting-help {
  color: var(--p-text-muted-color, #6b7280);
  font-size: 0.85rem;
}
.setting-key {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.75rem;
  color: var(--p-text-muted-color, #6b7280);
}
.setting-control {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}
.tags-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
  margin: 0.5rem 0 1rem;
}
.tags-help {
  margin: 0;
  max-width: 60ch;
}
.tag-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.tag-row {
  display: grid;
  grid-template-columns: auto 1fr auto auto;
  align-items: center;
  gap: 0.75rem;
  padding: 0.5rem 0.6rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.4rem;
}
.tag-swatch {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  border: 1px solid rgba(0, 0, 0, 0.08);
}
.tag-name {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-weight: 600;
}
.tag-desc {
  font-size: 0.85rem;
  color: var(--p-text-muted-color, #6b7280);
}
.tag-form {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}
.tag-form-label {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-weight: 500;
}
</style>
