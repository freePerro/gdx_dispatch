<!--
  EmailAttachmentPicker — choose what an outgoing email carries.

  Two ways in, both always visible under the message body:
  - "Attach file": the device's own picker (on a phone: camera, gallery or
    files). Big photos are downscaled on the device before they upload.
  - "From records": the job's photos and files, the customer's documents,
    and the customer's estimates and invoices as PDFs — tick several to send
    a customer every option at once. With no job or customer in context, it
    starts with a customer search.

  v-model is the list of picked items (utils/emailAttachments.js); the host
  sends `toAttachmentRefs(items)` as `attachment_refs`. The server decides
  what the sender may reach and the list here comes from the same rule
  (GET /api/email-attachments/sources), so nothing offered is refused as
  unreachable; a photo can still be refused if it cannot be cleaned of its
  location.
-->
<template>
  <div class="eap" data-test="eap">
    <div class="eap-actions">
      <Button
        type="button"
        label="Attach file"
        icon="pi pi-paperclip"
        severity="secondary"
        outlined
        size="small"
        :loading="reading"
        :disabled="disabled"
        data-test="eap-device"
        @click="fileInput?.click()"
      />
      <Button
        type="button"
        label="From records"
        icon="pi pi-folder-open"
        severity="secondary"
        outlined
        size="small"
        :disabled="disabled"
        data-test="eap-records"
        @click="openSources"
      />
      <input
        ref="fileInput"
        type="file"
        multiple
        class="eap-file-input"
        data-test="eap-file-input"
        @change="onFiles"
      />
    </div>

    <ul v-if="modelValue.length" class="eap-list" data-test="eap-list">
      <li v-for="a in modelValue" :key="attachmentKey(a)" class="eap-chip" data-test="eap-chip">
        <i class="pi" :class="iconFor(a)" aria-hidden="true" />
        <span class="eap-name">{{ a.name }}</span>
        <span v-if="a.internal" class="eap-internal" title="Not marked customer-visible on the job">Internal</span>
        <span v-if="a.size_bytes" class="eap-size">{{ fmtBytes(a.size_bytes) }}</span>
        <span v-else-if="isPdf(a)" class="eap-size">PDF</span>
        <button
          type="button"
          class="eap-remove"
          :aria-label="`Remove ${a.name}`"
          :disabled="disabled"
          data-test="eap-remove"
          @click="remove(a)"
        >
          <i class="pi pi-times" aria-hidden="true" />
        </button>
      </li>
    </ul>
    <div v-if="modelValue.length" class="eap-total" :class="{ 'eap-over': over }" data-test="eap-total">
      <template v-if="over">
        {{ fmtBytes(total) }} — over the {{ fmtBytes(MAX_TOTAL_BYTES) }} an email can carry. Remove a file.
      </template>
      <template v-else>
        {{ fmtBytes(total) }} of {{ fmtBytes(MAX_TOTAL_BYTES) }}<span v-if="pdfCount"> · {{ pdfCount }} PDF{{ pdfCount === 1 ? '' : 's' }} added at send</span>
      </template>
    </div>

    <Dialog
      v-model:visible="sourcesOpen"
      header="Attach from records"
      modal
      :style="{ width: '40rem' }"
      :breakpoints="{ '768px': '100vw' }"
      data-test="eap-dialog"
    >
      <div v-if="!activeCustomerId && !activeJobId" class="eap-search">
        <label for="eap-cust-q">Customer</label>
        <InputText
          id="eap-cust-q"
          v-model="custQuery"
          class="w-full"
          placeholder="Search by name, email or phone"
          autocomplete="off"
          data-test="eap-cust-q"
          @input="searchCustomers"
        />
        <ul v-if="custResults.length" class="eap-cust-results">
          <li v-for="c in custResults" :key="c.id">
            <button type="button" class="eap-cust-row" data-test="eap-cust-row" @click="chooseCustomer(c)">
              <span>{{ c.name }}</span>
              <span class="eap-muted">{{ c.email || c.phone || '' }}</span>
            </button>
          </li>
        </ul>
        <p v-else-if="custQuery.trim().length >= 2 && !custSearching" class="eap-muted">No customers match.</p>
      </div>

      <template v-else>
        <div class="eap-context">
          <span v-if="sources?.customer"><i class="pi pi-user" aria-hidden="true" /> {{ sources.customer.name }}</span>
          <span v-if="sources?.job"><i class="pi pi-briefcase" aria-hidden="true" /> {{ sources.job.label }}</span>
          <Button
            v-if="chosenCustomer"
            type="button"
            label="Change"
            text
            size="small"
            data-test="eap-cust-change"
            @click="clearCustomer"
          />
        </div>
        <p v-if="loading" class="eap-muted"><i class="pi pi-spin pi-spinner" aria-hidden="true" /> Loading…</p>
        <p v-else-if="loadError" class="eap-error">{{ loadError }}</p>
        <p v-else-if="!sections.length" class="eap-muted" data-test="eap-empty">
          Nothing on this job or customer you can attach.
        </p>
        <section v-for="s in sections" :key="s.title" class="eap-section" :data-test="`eap-section-${s.key}`">
          <h4>{{ s.title }}</h4>
          <label v-for="item in s.items" :key="attachmentKey(item)" class="eap-row" data-test="eap-row">
            <input
              type="checkbox"
              :checked="picked.has(attachmentKey(item))"
              @change="toggle(item)"
            />
            <span class="eap-row-main">
              <span class="eap-row-title">{{ item.name }}</span>
              <span v-if="item.detail" class="eap-muted">{{ item.detail }}</span>
            </span>
            <span v-if="item.internal" class="eap-internal" title="Not marked customer-visible on the job">Internal</span>
            <span v-if="item.size_bytes" class="eap-size">{{ fmtBytes(item.size_bytes) }}</span>
          </label>
        </section>
      </template>

      <template #footer>
        <Button type="button" label="Cancel" severity="secondary" text @click="sourcesOpen = false" />
        <Button
          type="button"
          :label="picked.size ? `Attach ${picked.size}` : 'Attach'"
          icon="pi pi-check"
          :disabled="loading"
          data-test="eap-apply"
          @click="apply"
        />
      </template>
    </Dialog>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import {
  MAX_TOTAL_BYTES,
  attachmentKey,
  fmtBytes,
  knownTotalBytes,
  readDeviceFile,
} from '../utils/emailAttachments'

const props = defineProps({
  modelValue: { type: Array, default: () => [] },
  jobId: { type: [String, Number], default: null },
  customerId: { type: [String, Number], default: null },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

const api = useApi()
const toast = useToast()

const fileInput = ref(null)
const reading = ref(false)
const sourcesOpen = ref(false)
const sources = ref(null)
const loading = ref(false)
const loadError = ref('')
const picked = ref(new Set())
const chosenCustomer = ref(null)
const custQuery = ref('')
const custResults = ref([])
const custSearching = ref(false)

const activeJobId = computed(() => props.jobId || null)
const activeCustomerId = computed(() => props.customerId || chosenCustomer.value?.id || null)

const total = computed(() => knownTotalBytes(props.modelValue))
const over = computed(() => total.value > MAX_TOTAL_BYTES)
const pdfCount = computed(() => props.modelValue.filter((a) => isPdf(a) && !a.size_bytes).length)

// A different job or customer is a different set of records.
watch(() => [props.jobId, props.customerId], () => {
  sources.value = null
  chosenCustomer.value = null
})

function isPdf(a) {
  return a.kind === 'estimate_pdf' || a.kind === 'invoice_pdf'
}

function iconFor(a) {
  if (a.kind === 'job_photo' || (a.content_type || '').startsWith('image/')) return 'pi-image'
  if (isPdf(a)) return 'pi-file-pdf'
  return 'pi-file'
}

function money(v) {
  return v == null ? '' : Number(v).toLocaleString(undefined, { style: 'currency', currency: 'USD' })
}

function shortDate(iso) {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString()
}

const sections = computed(() => {
  const s = sources.value
  if (!s) return []
  const out = [
    {
      key: 'estimates',
      title: 'Estimates',
      items: (s.estimates || []).map((e) => ({
        kind: 'estimate_pdf',
        id: e.id,
        name: `Estimate ${e.number || ''}`.trim(),
        detail: [e.label, e.status, money(e.total), shortDate(e.created_at)].filter(Boolean).join(' · '),
      })),
    },
    {
      key: 'invoices',
      title: 'Invoices',
      items: (s.invoices || []).map((i) => ({
        kind: 'invoice_pdf',
        id: i.id,
        name: `Invoice ${i.number || ''}`.trim(),
        detail: [i.status, money(i.total), shortDate(i.created_at)].filter(Boolean).join(' · '),
      })),
    },
    {
      key: 'photos',
      title: 'Job photos',
      items: (s.job_photos || []).map((p) => ({
        kind: 'job_photo',
        id: p.id,
        name: p.name,
        size_bytes: p.size_bytes || null,
        content_type: 'image/jpeg',
        internal: !p.customer_visible,
        detail: [p.kind, shortDate(p.uploaded_at)].filter(Boolean).join(' · '),
      })),
    },
    {
      key: 'job-files',
      title: 'Job files',
      items: (s.job_documents || []).map((d) => docItem(d)),
    },
    {
      key: 'customer-files',
      title: 'Customer files',
      items: (s.customer_documents || []).map((d) => docItem(d)),
    },
  ]
  return out.filter((sec) => sec.items.length)
})

function docItem(d) {
  return {
    kind: 'document',
    id: d.id,
    name: d.file_name || d.name,
    size_bytes: d.size_bytes || null,
    content_type: d.content_type,
    detail: d.name !== d.file_name ? d.name : shortDate(d.uploaded_at),
  }
}

async function loadSources() {
  const params = new URLSearchParams()
  if (activeJobId.value) params.set('job_id', String(activeJobId.value))
  if (activeCustomerId.value) params.set('customer_id', String(activeCustomerId.value))
  if (![...params.keys()].length) return
  loading.value = true
  loadError.value = ''
  try {
    sources.value = await api.get(`/api/email-attachments/sources?${params}`, { suppressErrorToast: true })
  } catch (err) {
    loadError.value = err?.message || 'Could not load the records.'
  } finally {
    loading.value = false
  }
}

function openSources() {
  picked.value = new Set(props.modelValue.filter((a) => a.kind !== 'upload').map(attachmentKey))
  sourcesOpen.value = true
  if (!sources.value && (activeJobId.value || activeCustomerId.value)) loadSources()
}

let searchTimer = null
function searchCustomers() {
  clearTimeout(searchTimer)
  const q = custQuery.value.trim()
  if (q.length < 2) {
    custResults.value = []
    return
  }
  searchTimer = setTimeout(async () => {
    custSearching.value = true
    try {
      const rows = await api.get(`/api/customers/search?q=${encodeURIComponent(q)}`, { suppressErrorToast: true })
      if (custQuery.value.trim() === q) custResults.value = (Array.isArray(rows) ? rows : []).slice(0, 8)
    } catch {
      custResults.value = []
    } finally {
      custSearching.value = false
    }
  }, 250)
}

function chooseCustomer(c) {
  chosenCustomer.value = { id: c.id, name: c.name }
  custResults.value = []
  loadSources()
}

function clearCustomer() {
  chosenCustomer.value = null
  sources.value = null
  custQuery.value = ''
}

function toggle(item) {
  const key = attachmentKey(item)
  const next = new Set(picked.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  picked.value = next
}

function apply() {
  const offered = new Map(sections.value.flatMap((s) => s.items).map((i) => [attachmentKey(i), i]))
  // Keep device files and anything picked earlier from another customer;
  // records offered here follow the ticks.
  const kept = props.modelValue.filter((a) => a.kind === 'upload' || !offered.has(attachmentKey(a)))
  const added = [...offered.entries()].filter(([k]) => picked.value.has(k)).map(([, i]) => i)
  emit('update:modelValue', [...kept, ...added])
  sourcesOpen.value = false
}

function remove(a) {
  const key = attachmentKey(a)
  emit('update:modelValue', props.modelValue.filter((x) => attachmentKey(x) !== key))
}

async function onFiles(event) {
  const files = [...(event.target.files || [])]
  event.target.value = ''
  if (!files.length) return
  reading.value = true
  const added = []
  try {
    for (const f of files) {
      try {
        added.push(await readDeviceFile(f))
      } catch (err) {
        toast.add({ severity: 'warn', summary: 'File not attached', detail: err.message, life: 5000 })
      }
    }
  } finally {
    reading.value = false
  }
  const have = new Set(props.modelValue.map(attachmentKey))
  emit('update:modelValue', [...props.modelValue, ...added.filter((a) => !have.has(attachmentKey(a)))])
}
</script>

<style scoped>
.eap {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.eap-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.eap-file-input {
  display: none;
}
.eap-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}
.eap-chip {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  border: 1px solid var(--p-content-border-color, #cbd5e1);
  background: var(--p-content-background, #fff);
  color: var(--p-text-color, #1e293b);
  border-radius: 999px;
  padding: 0.2rem 0.3rem 0.2rem 0.7rem;
  font-size: 0.8rem;
  max-width: 100%;
}
.eap-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 14rem;
}
.eap-size,
.eap-muted {
  color: var(--p-text-muted-color, #64748b);
  font-size: 0.75rem;
}
.eap-internal {
  font-size: 0.68rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.03em;
  color: var(--p-orange-700, #c2410c);
  background: color-mix(in srgb, var(--p-orange-500, #f97316) 14%, transparent);
  border-radius: 4px;
  padding: 0 0.3rem;
}
.eap-remove {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.6rem;
  height: 1.6rem;
  border: 0;
  border-radius: 999px;
  background: transparent;
  color: var(--p-text-muted-color, #64748b);
  cursor: pointer;
}
.eap-remove:hover:not(:disabled) {
  background: var(--p-content-hover-background, #f1f5f9);
  color: var(--p-text-color, #1e293b);
}
.eap-total {
  font-size: 0.75rem;
  color: var(--p-text-muted-color, #64748b);
}
.eap-over,
.eap-error {
  color: var(--p-red-600, #dc2626);
  font-weight: 600;
}
.eap-search {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.eap-cust-results {
  list-style: none;
  margin: 0;
  padding: 0;
}
.eap-cust-row {
  display: flex;
  justify-content: space-between;
  gap: 0.5rem;
  width: 100%;
  padding: 0.6rem 0.5rem;
  border: 0;
  border-bottom: 1px solid var(--p-content-border-color, #e2e8f0);
  background: transparent;
  color: var(--p-text-color, #1e293b);
  text-align: left;
  cursor: pointer;
}
.eap-cust-row:hover {
  background: var(--p-content-hover-background, #f1f5f9);
}
.eap-context {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 0.5rem;
  font-weight: 600;
}
.eap-section h4 {
  margin: 0.75rem 0 0.25rem;
  font-size: 0.85rem;
  color: var(--p-text-muted-color, #64748b);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.eap-row {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.45rem 0.25rem;
  border-bottom: 1px solid var(--p-content-border-color, #e2e8f0);
  cursor: pointer;
}
.eap-row input {
  width: 1.15rem;
  height: 1.15rem;
  flex: none;
}
.eap-row-main {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}
.eap-row-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Touch: a tech in a garage taps these with a thumb. */
@media (pointer: coarse) {
  .eap-remove {
    width: 44px;
    height: 44px;
  }
  .eap-row,
  .eap-cust-row {
    min-height: 48px;
  }
}
</style>
