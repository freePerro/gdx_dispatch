<template>
    <section class="mobile-customer-detail">
      <header class="mobile-page-head">
        <Button
          v-tooltip="'Back'"
          icon="pi pi-arrow-left"
          aria-label="Back"
          text
          rounded
          @click="goBack"
          data-test="mcd-back"
        />
        <div class="head-title">
          <h1 v-if="customer">{{ customer.name || 'Customer' }}</h1>
          <h1 v-else>Customer</h1>
          <Tag
            v-if="customer?.customer_type"
            :value="customer.customer_type"
            :severity="customer.customer_type === 'Commercial' ? 'warning' : 'info'"
          />
        </div>
        <Button
          v-tooltip="'Edit'"
          icon="pi pi-pencil"
          aria-label="Edit"
          text
          rounded
          @click="openEdit"
          data-test="mcd-edit"
        />
      </header>

      <div v-if="loading && !customer" class="state-msg" data-test="mcd-loading">
        <i class="pi pi-spin pi-spinner" />
        <span>Loading customer…</span>
      </div>

      <div v-else-if="error" class="state-msg" data-test="mcd-error">
        <i class="pi pi-exclamation-triangle empty-icon" />
        <div class="empty-title">{{ error }}</div>
        <Button label="Retry" size="small" @click="fetchCustomer" />
      </div>

      <template v-else-if="customer">
        <!-- Quick-tap action strip: call, text, email, navigate. Each is
             a 44×44 touch target per Apple HIG / Material 48dp. The actions
             that have no contact info render disabled so the strip layout
             stays stable. -->
        <div class="quick-actions" data-test="mcd-quick-actions">
          <a
            class="qa-btn"
            :class="{ disabled: !customer.phone }"
            :href="customer.phone ? `tel:${customer.phone}` : undefined"
            aria-label="Call"
            data-test="mcd-call"
          >
            <i class="pi pi-phone" />
            <span>Call</span>
          </a>
          <a
            class="qa-btn"
            :class="{ disabled: !customer.phone }"
            :href="customer.phone ? `sms:${customer.phone}` : undefined"
            aria-label="Text"
            data-test="mcd-text"
          >
            <i class="pi pi-comment" />
            <span>Text</span>
          </a>
          <a
            class="qa-btn"
            :class="{ disabled: !customer.email }"
            :href="customer.email ? `mailto:${customer.email}` : undefined"
            aria-label="Email"
            data-test="mcd-email"
          >
            <i class="pi pi-envelope" />
            <span>Email</span>
          </a>
          <a
            class="qa-btn"
            :class="{ disabled: !customer.address }"
            :href="customer.address ? `https://maps.google.com/?q=${encodeURIComponent(customer.address)}` : undefined"
            target="_blank"
            rel="noopener"
            aria-label="Navigate"
            data-test="mcd-navigate"
          >
            <i class="pi pi-map-marker" />
            <span>Map</span>
          </a>
        </div>

        <!-- Compact info card: phone/email/address shown inline if present. -->
        <div class="info-card" data-test="mcd-info-card">
          <div v-if="customer.phone" class="info-row">
            <i class="pi pi-phone" /> {{ customer.phone }}
          </div>
          <div v-if="customer.email" class="info-row">
            <i class="pi pi-envelope" /> {{ customer.email }}
          </div>
          <div v-if="customer.address" class="info-row">
            <i class="pi pi-map-marker" /> {{ customer.address }}
          </div>
          <div v-if="!customer.phone && !customer.email && !customer.address" class="muted">
            No contact info on file.
          </div>
        </div>

        <!-- Tab strip — horizontally scrollable for mobile. -->
        <div class="tab-strip" data-test="mcd-tab-strip" role="tablist">
          <button
            v-for="tab in tabs"
            :key="tab"
            type="button"
            role="tab"
            :class="['tab-btn', { active: activeTab === tab }]"
            :aria-selected="activeTab === tab"
            :data-test="`mcd-tab-${tab.toLowerCase().replace(/ /g, '-')}`"
            @click="selectTab(tab)"
          >
            {{ tab }}
          </button>
        </div>

        <div class="tab-content">
          <!-- Jobs -->
          <div v-if="activeTab === 'Jobs'" data-test="mcd-tab-jobs">
            <div v-if="loadingJobs" class="state-msg"><i class="pi pi-spin pi-spinner" /></div>
            <ol v-else-if="jobs.length" class="card-list">
              <li
                v-for="j in jobs"
                :key="j.id"
                class="row-card"
                @click="goJob(j)"
                data-test="mcd-job-row"
              >
                <div class="row-top">
                  <span class="row-title">{{ j.title || j.job_number || 'Job' }}</span>
                  <Tag
                    :value="j.lifecycle_stage || j.status || '—'"
                    :severity="jobStatusSeverity(j.lifecycle_stage || j.status)"
                  />
                </div>
                <div class="row-meta">
                  <span v-if="j.scheduled_at"><i class="pi pi-calendar" /> {{ fmtDate(j.scheduled_at) }}</span>
                  <span v-if="j.priority" class="muted">{{ j.priority }}</span>
                </div>
              </li>
            </ol>
            <div v-else class="state-msg"><span class="muted">No jobs for this customer.</span></div>
          </div>

          <!-- Estimates -->
          <div v-else-if="activeTab === 'Estimates'" data-test="mcd-tab-estimates">
            <div v-if="loadingEstimates" class="state-msg"><i class="pi pi-spin pi-spinner" /></div>
            <ol v-else-if="estimates.length" class="card-list">
              <li
                v-for="e in estimates"
                :key="e.id"
                class="row-card"
                @click="goEstimate(e)"
                data-test="mcd-estimate-row"
              >
                <div class="row-top">
                  <span class="row-title">#{{ e.estimate_number || (e.id || '').slice(0, 8) }}</span>
                  <span class="row-money">${{ fmtMoney(e.total) }}</span>
                </div>
                <div class="row-meta">
                  <Tag :value="e.status || '—'" :severity="estimateStatusSeverity(e.status)" />
                  <span v-if="e.created_at" class="muted">{{ fmtDate(e.created_at) }}</span>
                </div>
              </li>
            </ol>
            <div v-else class="state-msg"><span class="muted">No estimates.</span></div>
          </div>

          <!-- Invoices -->
          <div v-else-if="activeTab === 'Invoices'" data-test="mcd-tab-invoices">
            <div v-if="loadingInvoices" class="state-msg"><i class="pi pi-spin pi-spinner" /></div>
            <ol v-else-if="invoices.length" class="card-list">
              <li
                v-for="inv in invoices"
                :key="inv.id"
                class="row-card"
                @click="goInvoice(inv)"
                data-test="mcd-invoice-row"
              >
                <div class="row-top">
                  <span class="row-title">#{{ inv.number || inv.invoice_number || (inv.id || '').slice(0, 8) }}</span>
                  <span class="row-money">${{ fmtMoney(inv.total) }}</span>
                </div>
                <div class="row-meta">
                  <Tag :value="inv.status || '—'" :severity="invoiceStatusSeverity(inv.status)" />
                  <span v-if="inv.due_date" class="muted">Due {{ fmtDate(inv.due_date) }}</span>
                </div>
              </li>
            </ol>
            <div v-else class="state-msg"><span class="muted">No invoices.</span></div>
          </div>

          <!-- Locations -->
          <div v-else-if="activeTab === 'Locations'" data-test="mcd-tab-locations">
            <div v-if="loadingLocations" class="state-msg"><i class="pi pi-spin pi-spinner" /></div>
            <ol v-else-if="locations.length" class="card-list">
              <li
                v-for="loc in locations"
                :key="loc.id"
                class="row-card"
                data-test="mcd-location-row"
              >
                <div class="row-top">
                  <span class="row-title">{{ loc.label || loc.address || 'Location' }}</span>
                  <Tag v-if="loc.is_primary" value="Primary" severity="success" />
                </div>
                <div v-if="loc.address && loc.address !== loc.label" class="row-meta muted">
                  {{ loc.address }}
                </div>
                <a
                  v-if="loc.address"
                  :href="`https://maps.google.com/?q=${encodeURIComponent(loc.address)}`"
                  target="_blank"
                  rel="noopener"
                  class="row-action"
                >
                  <i class="pi pi-directions" /> Navigate
                </a>
              </li>
            </ol>
            <div v-else class="state-msg"><span class="muted">No locations.</span></div>
          </div>

          <!-- Notes -->
          <div v-else-if="activeTab === 'Notes'" data-test="mcd-tab-notes">
            <div v-if="customer.notes" class="notes-block">{{ customer.notes }}</div>
            <div v-else class="state-msg"><span class="muted">No notes on file.</span></div>
          </div>

          <!-- Equipment -->
          <div v-else-if="activeTab === 'Equipment'" data-test="mcd-tab-equipment">
            <div v-if="loadingEquipment" class="state-msg"><i class="pi pi-spin pi-spinner" /></div>
            <ol v-else-if="equipment.length" class="card-list">
              <li
                v-for="eq in equipment"
                :key="eq.id"
                class="row-card"
                data-test="mcd-equipment-row"
              >
                <div class="row-top">
                  <span class="row-title">{{ eq.label || eq.type || 'Equipment' }}</span>
                  <Tag v-if="eq.condition" :value="eq.condition" />
                </div>
                <div v-if="eq.notes" class="row-meta muted">{{ eq.notes }}</div>
              </li>
            </ol>
            <div v-else class="state-msg"><span class="muted">No equipment on file.</span></div>
          </div>

          <!-- Recurring Jobs -->
          <div v-else-if="activeTab === 'Recurring'" data-test="mcd-tab-recurring">
            <div v-if="loadingRecurring" class="state-msg"><i class="pi pi-spin pi-spinner" /></div>
            <ol v-else-if="recurring.length" class="card-list">
              <li
                v-for="r in recurring"
                :key="r.id"
                class="row-card"
                data-test="mcd-recurring-row"
              >
                <div class="row-top">
                  <span class="row-title">{{ r.title || r.template_name || 'Recurring' }}</span>
                  <span v-if="r.frequency" class="muted">{{ r.frequency }}</span>
                </div>
                <div v-if="r.next_run_at" class="row-meta muted">Next: {{ fmtDate(r.next_run_at) }}</div>
              </li>
            </ol>
            <div v-else class="state-msg"><span class="muted">No recurring jobs.</span></div>
          </div>

          <!-- Communications -->
          <div v-else-if="activeTab === 'Communications'" data-test="mcd-tab-communications">
            <div v-if="loadingComms" class="state-msg"><i class="pi pi-spin pi-spinner" /></div>
            <ol v-else-if="communications.length" class="card-list">
              <li
                v-for="c in communications"
                :key="c.id"
                class="row-card"
                data-test="mcd-comm-row"
              >
                <div class="row-top">
                  <span class="row-title">{{ c.subject || c.kind || 'Note' }}</span>
                  <span v-if="c.created_at" class="muted">{{ fmtDate(c.created_at) }}</span>
                </div>
                <div v-if="c.body || c.message" class="row-meta">
                  {{ (c.body || c.message || '').slice(0, 140) }}
                </div>
              </li>
            </ol>
            <div v-else class="state-msg"><span class="muted">No communications logged.</span></div>
          </div>

          <!-- Portal -->
          <div v-else-if="activeTab === 'Portal'" data-test="mcd-tab-portal">
            <div v-if="loadingPortal" class="state-msg"><i class="pi pi-spin pi-spinner" /></div>
            <div v-else-if="portalStatus" class="info-card">
              <div class="info-row">
                <i class="pi pi-user" />
                <span v-if="portalStatus.exists">Portal account active</span>
                <span v-else class="muted">No portal account.</span>
              </div>
              <div v-if="portalStatus.last_login" class="info-row muted">
                Last login: {{ fmtDate(portalStatus.last_login) }}
              </div>
            </div>
            <div v-else class="state-msg"><span class="muted">Portal status unavailable.</span></div>
          </div>
        </div>
      </template>

      <Dialog
        v-model:visible="editOpen"
        header="Edit Customer"
        modal
        :style="{ width: '95vw', maxWidth: '460px' }"
        :breakpoints="{ '768px': '95vw' }"
      >
        <div v-if="editForm" class="form-stack">
          <div>
            <label for="mcd-edit-name">Name *</label>
            <InputText id="mcd-edit-name" v-model="editForm.name" class="w-full" data-test="mcd-edit-name" />
          </div>
          <div>
            <label for="mcd-edit-phone">Phone</label>
            <InputText id="mcd-edit-phone" v-model="editForm.phone" type="tel" class="w-full" data-test="mcd-edit-phone" />
          </div>
          <div>
            <label for="mcd-edit-email">Email</label>
            <InputText id="mcd-edit-email" v-model="editForm.email" type="email" class="w-full" data-test="mcd-edit-email" />
          </div>
          <div>
            <label for="mcd-edit-address">Address</label>
            <Textarea id="mcd-edit-address" v-model="editForm.address" rows="2" autoResize class="w-full" data-test="mcd-edit-address" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="editOpen = false" />
          <Button label="Save" :loading="saving" :disabled="!editForm?.name?.trim()" @click="submitEdit" data-test="mcd-edit-save" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { useToast } from 'primevue/usetoast'

import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Tag from 'primevue/tag'

const route = useRoute()
const router = useRouter()
const api = useApi()
const toast = useToast()

const customer = ref(null)
const loading = ref(false)
const error = ref('')

const tabs = ['Jobs', 'Estimates', 'Invoices', 'Locations', 'Notes', 'Equipment', 'Recurring', 'Communications', 'Portal']
const activeTab = ref('Jobs')

const jobs = ref([])
const estimates = ref([])
const invoices = ref([])
const locations = ref([])
const equipment = ref([])
const recurring = ref([])
const communications = ref([])
const portalStatus = ref(null)

const loadingJobs = ref(false)
const loadingEstimates = ref(false)
const loadingInvoices = ref(false)
const loadingLocations = ref(false)
const loadingEquipment = ref(false)
const loadingRecurring = ref(false)
const loadingComms = ref(false)
const loadingPortal = ref(false)

const editOpen = ref(false)
const editForm = ref(null)
const saving = ref(false)

const customerId = computed(() => route.params.id)

function fmtMoney(n) {
  return Number(n || 0).toFixed(2)
}

function fmtDate(d) {
  if (!d) return ''
  try {
    return new Date(d).toLocaleDateString()
  } catch {
    return String(d)
  }
}

function jobStatusSeverity(s) {
  const k = String(s || '').toLowerCase()
  if (['completed', 'complete', 'done', 'paid'].includes(k)) return 'success'
  if (['scheduled', 'in_progress', 'active'].includes(k)) return 'info'
  if (['unscheduled', 'pending'].includes(k)) return 'warning'
  if (['canceled', 'cancelled', 'voided'].includes(k)) return 'danger'
  return 'secondary'
}

function estimateStatusSeverity(s) {
  const k = String(s || '').toLowerCase()
  if (['accepted', 'approved'].includes(k)) return 'success'
  if (['sent', 'pending'].includes(k)) return 'info'
  if (['draft'].includes(k)) return 'warning'
  if (['rejected', 'declined'].includes(k)) return 'danger'
  return 'secondary'
}

function invoiceStatusSeverity(s) {
  const k = String(s || '').toLowerCase()
  if (['paid'].includes(k)) return 'success'
  if (['sent'].includes(k)) return 'info'
  if (['draft', 'pending'].includes(k)) return 'warning'
  if (['void', 'canceled', 'overdue'].includes(k)) return 'danger'
  return 'secondary'
}

async function fetchCustomer() {
  loading.value = true
  error.value = ''
  try {
    customer.value = await api.get(`/api/customers/${customerId.value}`)
  } catch (err) {
    error.value = err?.message || 'Failed to load customer'
  } finally {
    loading.value = false
  }
}

async function fetchJobs() {
  if (!customerId.value) return
  loadingJobs.value = true
  try {
    const r = await api.get(`/api/jobs?customer_id=${customerId.value}&per_page=100`)
    jobs.value = Array.isArray(r) ? r : r?.items || r?.data || []
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Jobs failed to load', detail: err.message, life: 3500 })
  } finally {
    loadingJobs.value = false
  }
}

async function fetchEstimates() {
  loadingEstimates.value = true
  try {
    const r = await api.get(`/api/estimates?customer_id=${customerId.value}`)
    estimates.value = Array.isArray(r) ? r : r?.items || r?.data || []
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Estimates failed to load', detail: err.message, life: 3500 })
  } finally {
    loadingEstimates.value = false
  }
}

async function fetchInvoices() {
  loadingInvoices.value = true
  try {
    const r = await api.get(`/api/invoices?customer_id=${customerId.value}`)
    invoices.value = Array.isArray(r) ? r : r?.items || r?.data || []
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Invoices failed to load', detail: err.message, life: 3500 })
  } finally {
    loadingInvoices.value = false
  }
}

async function fetchLocations() {
  loadingLocations.value = true
  try {
    const r = await api.get(`/api/customers/${customerId.value}/locations`)
    locations.value = Array.isArray(r) ? r : r?.items || r?.data || []
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Locations failed to load', detail: err.message, life: 3500 })
  } finally {
    loadingLocations.value = false
  }
}

async function fetchEquipment() {
  loadingEquipment.value = true
  try {
    const r = await api.get(`/api/customers/${customerId.value}/equipment`)
    equipment.value = Array.isArray(r) ? r : r?.items || r?.data || []
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Equipment failed to load', detail: err.message, life: 3500 })
  } finally {
    loadingEquipment.value = false
  }
}

async function fetchRecurring() {
  loadingRecurring.value = true
  try {
    const r = await api.get(`/api/customers/${customerId.value}/recurring-jobs`)
    recurring.value = Array.isArray(r) ? r : r?.items || r?.data || []
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Recurring failed to load', detail: err.message, life: 3500 })
  } finally {
    loadingRecurring.value = false
  }
}

async function fetchCommunications() {
  loadingComms.value = true
  try {
    const r = await api.get(`/api/customers/${customerId.value}/communications`)
    communications.value = Array.isArray(r) ? r : r?.items || r?.data || []
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Communications failed to load', detail: err.message, life: 3500 })
  } finally {
    loadingComms.value = false
  }
}

async function fetchPortal() {
  loadingPortal.value = true
  try {
    portalStatus.value = await api.get(`/api/customers/${customerId.value}/portal-account`)
  } catch (_) {
    portalStatus.value = null
  } finally {
    loadingPortal.value = false
  }
}

const tabFetchers = {
  Jobs: fetchJobs,
  Estimates: fetchEstimates,
  Invoices: fetchInvoices,
  Locations: fetchLocations,
  Equipment: fetchEquipment,
  Recurring: fetchRecurring,
  Communications: fetchCommunications,
  Portal: fetchPortal,
}

const tabLoaded = new Set()
function selectTab(tab) {
  activeTab.value = tab
  if (tabLoaded.has(tab)) return
  const fn = tabFetchers[tab]
  if (fn) {
    tabLoaded.add(tab)
    fn()
  }
}

function goBack() {
  if (window.history.length > 1) {
    router.back()
  } else {
    router.push('/mobile/customers')
  }
}

function goJob(j) {
  router.push({ path: `/jobs/${j.id}` })
}

function goEstimate(e) {
  router.push({ path: `/estimates/${e.id}` })
}

function goInvoice(inv) {
  router.push({ path: `/billing/${inv.id}` })
}

function openEdit() {
  if (!customer.value) return
  editForm.value = {
    name: customer.value.name || '',
    phone: customer.value.phone || '',
    email: customer.value.email || '',
    address: customer.value.address || '',
  }
  editOpen.value = true
}

async function submitEdit() {
  if (!editForm.value?.name?.trim()) return
  saving.value = true
  try {
    customer.value = await api.patch(`/api/customers/${customerId.value}`, { ...editForm.value })
    toast.add({ severity: 'success', summary: 'Saved', life: 2000 })
    editOpen.value = false
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Save failed', detail: err?.message, life: 4000 })
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  await fetchCustomer()
  // Eager-load the default tab so the user sees content immediately.
  selectTab('Jobs')
})
</script>

<style scoped>
.mobile-customer-detail {
  padding: 0.5rem 0.75rem calc(5rem + env(safe-area-inset-bottom));
  max-width: 800px;
  margin: 0 auto;
}

.mobile-page-head {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
}

.mobile-page-head :deep(.p-button) {
  min-width: 44px;
  min-height: 44px;
}

.head-title {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  min-width: 0;
}
.head-title h1 {
  margin: 0;
  font-size: 1.15rem;
  font-weight: 700;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.quick-actions {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 0.4rem;
  margin-bottom: 0.75rem;
}
.qa-btn {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.2rem;
  min-height: 64px;
  border-radius: 0.55rem;
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  color: var(--p-text-color, #111);
  font-size: 0.78rem;
  font-weight: 600;
  text-decoration: none;
  cursor: pointer;
}
.qa-btn:active { background: var(--p-content-hover-background, #f3f4f6); }
.qa-btn.disabled {
  opacity: 0.4;
  pointer-events: none;
}
.qa-btn i { font-size: 1.1rem; }

.info-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.55rem;
  padding: 0.7rem 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  margin-bottom: 0.75rem;
}
.info-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.9rem;
}

.tab-strip {
  display: flex;
  gap: 0.4rem;
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
  padding-bottom: 0.4rem;
  margin-bottom: 0.5rem;
}
.tab-strip::-webkit-scrollbar { display: none; }
.tab-btn {
  flex: 0 0 auto;
  min-height: 44px;
  padding: 0 0.9rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  background: var(--p-content-background, #fff);
  border-radius: 999px;
  font-size: 0.85rem;
  font-weight: 600;
  cursor: pointer;
  white-space: nowrap;
  color: var(--p-text-color, #111);
}
.tab-btn.active {
  background: var(--p-primary-color, #2563eb);
  color: #fff;
  border-color: var(--p-primary-color, #2563eb);
}

.tab-content {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.card-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.row-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.55rem;
  padding: 0.7rem 0.85rem;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.row-card:active { background: var(--p-content-hover-background, #f3f4f6); }
.row-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}
.row-title {
  font-weight: 700;
  font-size: 0.95rem;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row-money {
  font-weight: 700;
  font-size: 0.95rem;
  color: var(--p-primary-color, #2563eb);
}
.row-meta {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.8rem;
  color: var(--p-text-muted-color, #6b7280);
  flex-wrap: wrap;
}

.row-action {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  margin-top: 0.3rem;
  font-size: 0.85rem;
  color: var(--p-primary-color, #2563eb);
  text-decoration: none;
  min-height: 44px;
}

.notes-block {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.55rem;
  padding: 0.85rem;
  font-size: 0.9rem;
  white-space: pre-wrap;
}

.state-msg {
  text-align: center;
  padding: 2rem 1rem;
  color: var(--p-text-muted-color, #6b7280);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.4rem;
}
.empty-icon { font-size: 2rem; opacity: 0.5; }
.empty-title { font-size: 1.05rem; font-weight: 600; }

.muted { color: var(--p-text-muted-color, #6b7280); }

.form-stack {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}
.form-stack label {
  display: block;
  font-size: 0.85rem;
  font-weight: 500;
  margin-bottom: 0.2rem;
}
.w-full { width: 100%; }
</style>
