<template>
  <section class="door-listings-view view-card">
    <Toolbar>
      <template #start>
        <h1 class="view-heading">Doors for Sale</h1>
        <!-- The queue rots if approvals are slow, so the backlog is the first
             thing on the page, not a tab you have to remember to open. -->
        <Tag
          v-if="pendingCount > 0"
          :value="`${pendingCount} awaiting review`"
          severity="warn"
          class="pending-chip"
          data-testid="pending-count"
        />
      </template>
      <template #end>
        <Button
          label="Refresh"
          icon="pi pi-refresh"
          severity="secondary"
          :disabled="loading"
          @click="fetchItems"
        />
        <Button
          label="Add a door"
          icon="pi pi-plus"
          data-testid="new-listing"
          @click="openCreate"
        />
      </template>
    </Toolbar>

    <div v-if="error" class="error-banner">{{ error }}</div>

    <div class="filter-row">
      <Button
        v-for="f in FILTERS"
        :key="f.value"
        :label="f.label"
        :severity="statusFilter === f.value ? 'primary' : 'secondary'"
        :outlined="statusFilter !== f.value"
        size="small"
        @click="statusFilter = f.value; fetchItems()"
      />
    </div>

    <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

    <DataTable v-else :value="listings" dataKey="id" responsiveLayout="scroll">
      <Column header="Door">
        <template #body="{ data }">
          <div class="door-cell">
            <img
              v-if="data.photos?.length"
              :src="photoUrl(data.photos[0].id)"
              :alt="data.title"
              class="thumb"
              loading="lazy"
            />
            <div v-else class="thumb thumb--empty"><i class="pi pi-image" /></div>
            <div>
              <div class="door-title">{{ data.title }}</div>
              <div class="door-sub">{{ sizeLabel(data) }}</div>
            </div>
          </div>
        </template>
      </Column>

      <Column header="Type">
        <template #body="{ data }">
          <Tag :value="TYPE_LABEL[data.listing_type] || data.listing_type" severity="secondary" />
        </template>
      </Column>

      <Column header="Source">
        <template #body="{ data }">
          <!-- Consignment is visibly different: it ranks below GDX stock on the
               website and the office needs to see that at a glance. -->
          <Tag
            :value="data.gdx_owned ? 'GDX' : 'Customer'"
            :severity="data.gdx_owned ? 'success' : 'info'"
          />
        </template>
      </Column>

      <Column header="Price">
        <template #body="{ data }">
          <span v-if="data.price_display === 'call_for_price'" class="muted">Call for price</span>
          <span v-else>{{ formatMoney(data.price) }}</span>
        </template>
      </Column>

      <Column header="Status">
        <template #body="{ data }">
          <Tag :value="STATUS_LABEL[data.status] || data.status" :severity="statusSeverity(data.status)" />
          <div v-if="data.status === 'rejected' && data.rejection_reason" class="reject-reason">
            {{ data.rejection_reason }}
          </div>
        </template>
      </Column>

      <Column header="Actions">
        <template #body="{ data }">
          <div class="actions">
            <!-- Approve/reject inline — opening a drawer to approve is the
                 friction that makes a review queue go stale. -->
            <Button
              v-if="data.status !== 'published' && data.status !== 'sold'"
              label="Publish"
              icon="pi pi-check"
              size="small"
              :disabled="busyId === data.id"
              :data-testid="`publish-${data.id}`"
              @click="publish(data)"
            />
            <Button
              v-if="data.status === 'pending_review'"
              label="Reject"
              icon="pi pi-times"
              severity="danger"
              outlined
              size="small"
              :disabled="busyId === data.id"
              @click="openReject(data)"
            />
            <Button
              v-if="data.status === 'published'"
              label="Mark sold"
              icon="pi pi-flag"
              severity="secondary"
              size="small"
              :disabled="busyId === data.id"
              :data-testid="`sold-${data.id}`"
              @click="markSold(data)"
            />
            <Button
              icon="pi pi-pencil"
              severity="secondary"
              outlined
              size="small"
              :data-testid="`edit-${data.id}`"
              v-tooltip.bottom="'Edit'"
              @click="openEdit(data)"
            />
            <Button
              icon="pi pi-images"
              severity="secondary"
              outlined
              size="small"
              v-tooltip.bottom="'Photos'"
              @click="openPhotos(data)"
            />
          </div>
        </template>
      </Column>

      <template #empty>
        <p class="empty">No doors yet. Add one, or wait for a tech to send one in from the field.</p>
      </template>
    </DataTable>

    <!-- Create / edit -->
    <Dialog v-model:visible="showEditor" :header="editing.id ? 'Edit door' : 'Add a door'" modal :style="{ width: '32rem' }">
      <div class="form-grid">
        <label>Title<InputText v-model="editing.title" data-testid="listing-title" /></label>
        <label>Type
          <Select v-model="editing.listing_type" :options="TYPE_OPTIONS" optionLabel="label" optionValue="value" />
        </label>
        <label>Condition
          <Select v-model="editing.condition" :options="CONDITION_OPTIONS" optionLabel="label" optionValue="value" showClear />
        </label>
        <div class="two-up">
          <label>Width (in)<InputNumber v-model="editing.width_in" :min="0" /></label>
          <label>Height (in)<InputNumber v-model="editing.height_in" :min="0" /></label>
        </div>
        <label>Color<InputText v-model="editing.color" /></label>
        <label>Price
          <InputNumber v-model="editing.price" mode="currency" currency="USD" :min="0" />
        </label>
        <label class="inline">
          <Checkbox v-model="editing.callForPrice" :binary="true" />
          <span>Show "Call for price" instead of the number</span>
        </label>
        <label>Description<Textarea v-model="editing.description" rows="3" autoResize /></label>
      </div>
      <template #footer>
        <Button label="Cancel" severity="secondary" text @click="showEditor = false" />
        <Button label="Save" :loading="saving" data-testid="save-listing" @click="save" />
      </template>
    </Dialog>

    <!-- Photos -->
    <Dialog v-model:visible="showPhotos" header="Photos" modal :style="{ width: '30rem' }">
      <p class="muted small">
        A door needs at least one photo before it can go on the website.
      </p>
      <div class="photo-grid">
        <div v-for="p in (photoTarget.photos || [])" :key="p.id" class="photo-cell">
          <img :src="photoUrl(p.id)" :alt="photoTarget.title" loading="lazy" />
          <Button icon="pi pi-trash" severity="danger" text size="small" @click="removePhoto(p)" />
        </div>
      </div>
      <FileUpload
        mode="basic"
        name="file"
        accept="image/jpeg,image/png,image/webp"
        :auto="true"
        :customUpload="true"
        chooseLabel="Add photo"
        chooseIcon="pi pi-upload"
        data-testid="photo-upload"
        @uploader="uploadPhoto"
      />
    </Dialog>

    <!-- Reject -->
    <Dialog v-model:visible="showReject" header="Reject this door" modal :style="{ width: '26rem' }">
      <p class="muted small">
        The reason goes back to whoever sent it in. A rejection with no reason is
        how people learn to stop submitting.
      </p>
      <Textarea v-model="rejectReason" rows="3" autoResize class="full" data-testid="reject-reason" />
      <template #footer>
        <Button label="Cancel" severity="secondary" text @click="showReject = false" />
        <Button
          label="Reject"
          severity="danger"
          :disabled="!rejectReason.trim()"
          data-testid="confirm-reject"
          @click="reject"
        />
      </template>
    </Dialog>
  </section>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useApi } from '../composables/useApi'
import { useToast } from 'primevue/usetoast'
import { formatMoney } from '../composables/useFormatters'

import Toolbar from 'primevue/toolbar'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
import Textarea from 'primevue/textarea'
import Select from 'primevue/select'
import Checkbox from 'primevue/checkbox'
import FileUpload from 'primevue/fileupload'
import ProgressSpinner from 'primevue/progressspinner'

const api = useApi()
const toast = useToast()

const listings = ref([])
const pendingCount = ref(0)
const loading = ref(false)
const saving = ref(false)
const error = ref(null)
const busyId = ref(null)
const statusFilter = ref('')

const showEditor = ref(false)
const showPhotos = ref(false)
const showReject = ref(false)
const editing = ref({})
const photoTarget = ref({})
const rejectReason = ref('')

const FILTERS = [
  { label: 'All', value: '' },
  { label: 'Needs review', value: 'pending_review' },
  { label: 'Live', value: 'published' },
  { label: 'Drafts', value: 'draft' },
  { label: 'Sold', value: 'sold' },
]
const TYPE_OPTIONS = [
  { label: 'Used', value: 'used' },
  { label: 'In stock', value: 'in_stock' },
  { label: 'Quick ship', value: 'quick_ship' },
]
const CONDITION_OPTIONS = [
  { label: 'New', value: 'new' },
  { label: 'Like new', value: 'like_new' },
  { label: 'Good', value: 'good' },
  { label: 'Fair', value: 'fair' },
]
const TYPE_LABEL = { used: 'Used', in_stock: 'In stock', quick_ship: 'Quick ship' }
const STATUS_LABEL = {
  draft: 'Draft',
  pending_review: 'Needs review',
  published: 'Live on site',
  rejected: 'Rejected',
  sold: 'Sold',
  archived: 'Archived',
}

function statusSeverity(status) {
  if (status === 'published') return 'success'
  if (status === 'pending_review') return 'warn'
  if (status === 'rejected') return 'danger'
  if (status === 'sold') return 'info'
  return 'secondary'
}

// Photo bytes come from the unauthenticated public route, not the API — a page
// of thumbnails through the keyed endpoint would trip its 60 req/min cap.
function photoUrl(photoId) {
  return `/public/door-listings/${photoId}.jpg`
}

function sizeLabel(d) {
  if (!d.width_in && !d.height_in) return d.color || ''
  const w = d.width_in ? `${d.width_in}"` : '?'
  const h = d.height_in ? `${d.height_in}"` : '?'
  return [`${w} × ${h}`, d.color].filter(Boolean).join(' · ')
}

async function fetchItems() {
  loading.value = true
  error.value = null
  try {
    const qs = statusFilter.value ? `?status=${statusFilter.value}` : ''
    const data = await api.get(`/api/door-listings${qs}`)
    listings.value = data.listings || []
    pendingCount.value = data.pending_count || 0
  } catch (e) {
    error.value = e.message || 'Could not load listings'
  } finally {
    loading.value = false
  }
}

// Portal submissions arrive priced "call for price" on purpose — pricing a
// customer's door is the office's call at approval, so this is the screen where
// that actually happens. Without it the customer-submission flow dead-ends.
function openEdit(row) {
  editing.value = {
    id: row.id,
    title: row.title,
    listing_type: row.listing_type,
    condition: row.condition,
    width_in: row.width_in,
    height_in: row.height_in,
    color: row.color,
    price: row.price,
    description: row.description,
    callForPrice: row.price_display === 'call_for_price',
  }
  showEditor.value = true
}

function openCreate() {
  editing.value = {
    title: '', listing_type: 'used', condition: null, width_in: null,
    height_in: null, color: '', price: null, description: '', callForPrice: false,
  }
  showEditor.value = true
}

async function save() {
  saving.value = true
  try {
    const body = {
      title: editing.value.title,
      listing_type: editing.value.listing_type,
      condition: editing.value.condition || null,
      width_in: editing.value.width_in,
      height_in: editing.value.height_in,
      color: editing.value.color || null,
      price: editing.value.price,
      description: editing.value.description || null,
      price_display: editing.value.callForPrice ? 'call_for_price' : 'fixed',
    }
    if (editing.value.id) {
      await api.patch(`/api/door-listings/${editing.value.id}`, body)
    } else {
      await api.post('/api/door-listings', body)
    }
    showEditor.value = false
    await fetchItems()
  } catch (e) {
    error.value = e.message || 'Could not save'
  } finally {
    saving.value = false
  }
}

async function publish(row) {
  busyId.value = row.id
  try {
    await api.post(`/api/door-listings/${row.id}/publish`, {})
    toast.add({ severity: 'success', summary: 'Live on garagedoorxperts.com', life: 3000 })
    await fetchItems()
  } catch (e) {
    // The most common cause is "no photo yet" — surface it rather than a toastless failure.
    toast.add({ severity: 'warn', summary: 'Not published', detail: e.message, life: 6000 })
  } finally {
    busyId.value = null
  }
}

async function markSold(row) {
  busyId.value = row.id
  try {
    await api.post(`/api/door-listings/${row.id}/sold`, {})
    await fetchItems()
  } finally {
    busyId.value = null
  }
}

function openReject(row) {
  photoTarget.value = row
  rejectReason.value = ''
  showReject.value = true
}

async function reject() {
  try {
    await api.post(`/api/door-listings/${photoTarget.value.id}/reject`, {
      reason: rejectReason.value.trim(),
    })
    showReject.value = false
    await fetchItems()
  } catch (e) {
    error.value = e.message || 'Could not reject'
  }
}

function openPhotos(row) {
  photoTarget.value = row
  showPhotos.value = true
}

async function uploadPhoto(event) {
  const file = event.files?.[0]
  if (!file) return
  const form = new FormData()
  form.append('file', file)
  try {
    await api.post(`/api/door-listings/${photoTarget.value.id}/photos`, form)
    await fetchItems()
    photoTarget.value = listings.value.find((l) => l.id === photoTarget.value.id) || photoTarget.value
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Upload failed', detail: e.message, life: 6000 })
  }
}

async function removePhoto(photo) {
  await api.del(`/api/door-listings/${photoTarget.value.id}/photos/${photo.id}`)
  await fetchItems()
  photoTarget.value = listings.value.find((l) => l.id === photoTarget.value.id) || photoTarget.value
}

onMounted(fetchItems)
</script>

<style scoped>
.pending-chip { margin-left: 0.75rem; }
.filter-row { display: flex; gap: 0.5rem; flex-wrap: wrap; margin: 0.75rem 0; }
.door-cell { display: flex; align-items: center; gap: 0.75rem; }
.thumb {
  width: 64px; height: 48px; object-fit: cover; border-radius: 6px;
  border: 1px solid var(--p-content-border-color);
}
.thumb--empty {
  display: flex; align-items: center; justify-content: center;
  color: var(--p-text-muted-color); background: var(--p-content-hover-background);
}
.door-title { font-weight: 600; }
.door-sub, .muted { color: var(--p-text-muted-color); }
.small { font-size: 0.85rem; }
.reject-reason { font-size: 0.8rem; color: var(--p-text-muted-color); margin-top: 0.25rem; }
.actions { display: flex; gap: 0.4rem; flex-wrap: wrap; }
.form-grid { display: flex; flex-direction: column; gap: 0.85rem; }
.form-grid label { display: flex; flex-direction: column; gap: 0.3rem; font-weight: 500; }
.form-grid label.inline { flex-direction: row; align-items: center; gap: 0.5rem; font-weight: 400; }
.two-up { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }
.photo-grid { display: flex; flex-wrap: wrap; gap: 0.5rem; margin: 0.75rem 0; }
.photo-cell { position: relative; }
.photo-cell img {
  width: 96px; height: 72px; object-fit: cover; border-radius: 6px;
  border: 1px solid var(--p-content-border-color);
}
.full { width: 100%; }
.spinner-wrap { display: flex; justify-content: center; padding: 2rem; }
.empty { color: var(--p-text-muted-color); text-align: center; padding: 1.5rem; }
.error-banner {
  background: var(--p-red-50); color: var(--p-red-700);
  border: 1px solid var(--p-red-200); border-radius: 6px; padding: 0.6rem 0.9rem; margin: 0.5rem 0;
}
@media (prefers-color-scheme: dark) {
  .error-banner { background: color-mix(in srgb, var(--p-red-500) 15%, transparent); color: var(--p-red-300); }
}
:root[data-theme='dark'] .error-banner {
  background: color-mix(in srgb, var(--p-red-500) 15%, transparent);
  color: var(--p-red-300);
  border-color: color-mix(in srgb, var(--p-red-500) 35%, transparent);
}
</style>
