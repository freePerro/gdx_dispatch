<template>
    <section class="mobile-customers">
      <header class="mobile-page-head">
        <div class="head-row">
          <h1>Customers</h1>
          <button
            type="button"
            class="head-add"
            aria-label="New customer"
            @click="openCreate"
            data-test="mc-head-add"
          >
            <i class="pi pi-plus" /> New
          </button>
        </div>
        <InputText
          v-model="searchQuery"
          placeholder="Search name, phone, email…"
          class="search-input"
          @input="onSearch"
          data-test="mc-search"
        />
      </header>

      <div v-if="loading && !customers.length" class="state-msg">
        <i class="pi pi-spin pi-spinner" />
        <span>Loading customers…</span>
      </div>
      <div v-else-if="!customers.length" class="state-msg">
        <i class="pi pi-users empty-icon" />
        <div class="empty-title">{{ searchQuery ? 'No matches' : 'No customers yet' }}</div>
        <div class="empty-help">{{ searchQuery ? 'Clear the search to see all.' : 'Tap + to add one.' }}</div>
      </div>

      <ol v-else class="card-list">
        <li
          v-for="c in customers"
          :key="c.id"
          class="cust-card"
          @click="openDetail(c)"
          data-test="mc-cust-row"
        >
          <div class="cust-row">
            <div class="cust-name">{{ c.name || c.email || '—' }}</div>
            <i class="pi pi-chevron-right cust-chevron" />
          </div>
          <div class="cust-meta">
            <span v-if="c.phone" class="meta-item">
              <i class="pi pi-phone" /> {{ c.phone }}
            </span>
            <span v-if="c.email" class="meta-item">
              <i class="pi pi-envelope" /> {{ c.email }}
            </span>
            <span v-if="c.address" class="meta-item">
              <i class="pi pi-map-marker" /> {{ c.address }}
            </span>
          </div>
        </li>
      </ol>

      <Dialog
        v-model:visible="createOpen"
        header="New Customer"
        modal
        :style="{ width: '95vw', maxWidth: '460px' }"
        :breakpoints="{ '768px': '95vw' }"
      >
        <div class="form-stack">
          <div>
            <label for="mc-name">Name *</label>
            <InputText id="mc-name" v-model="form.name" class="w-full" data-test="mc-form-name" />
          </div>
          <div>
            <label for="mc-phone">Phone</label>
            <InputText id="mc-phone" v-model="form.phone" type="tel" class="w-full" data-test="mc-form-phone" />
          </div>
          <div>
            <label for="mc-email">Email</label>
            <InputText id="mc-email" v-model="form.email" type="email" class="w-full" data-test="mc-form-email" />
          </div>
          <div>
            <label for="mc-address">Address</label>
            <Textarea id="mc-address" v-model="form.address" rows="2" autoResize class="w-full" data-test="mc-form-address" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="createOpen = false" />
          <Button label="Create" :loading="saving" :disabled="!form.name.trim()" @click="submit" data-test="mc-form-submit" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useApi } from '../composables/useApi'
import { useToast } from 'primevue/usetoast'

import InputText from 'primevue/inputtext'
import Textarea from 'primevue/textarea'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'

const api = useApi()
const toast = useToast()
const router = useRouter()

const customers = ref([])
const loading = ref(false)
const searchQuery = ref('')

const createOpen = ref(false)
const saving = ref(false)
const form = ref(emptyForm())

function emptyForm() {
  return { name: '', phone: '', email: '', address: '' }
}

async function fetchCustomers() {
  loading.value = true
  try {
    const q = searchQuery.value.trim()
    const url = q
      ? `/api/customers?q=${encodeURIComponent(q)}&per_page=200`
      : '/api/customers?per_page=200'
    const r = await api.get(url)
    customers.value = Array.isArray(r) ? r : r?.items || r?.data || []
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Load failed', detail: err?.message, life: 4000 })
  } finally {
    loading.value = false
  }
}

let searchTimer = null
function onSearch() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(fetchCustomers, 300)
}

function openDetail(c) {
  router.push({ path: `/mobile/customers/${c.id}` })
}

function openCreate() {
  form.value = emptyForm()
  createOpen.value = true
}

async function submit() {
  if (!form.value.name.trim()) return
  saving.value = true
  try {
    const created = await api.post('/api/customers', { ...form.value })
    toast.add({ severity: 'success', summary: 'Customer created', life: 2500 })
    createOpen.value = false
    if (created?.id) router.push({ path: `/mobile/customers/${created.id}` })
    else await fetchCustomers()
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Create failed', detail: err?.message, life: 4000 })
  } finally {
    saving.value = false
  }
}

onMounted(fetchCustomers)
</script>

<style scoped>
.mobile-customers {
  padding: 0.75rem 0.75rem calc(5rem + env(safe-area-inset-bottom));
  max-width: 800px;
  margin: 0 auto;
}

.mobile-page-head {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  margin-bottom: 0.75rem;
}

.head-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.mobile-page-head h1 {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 700;
}

.head-add {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  padding: 0.45rem 0.8rem;
  border-radius: 0.5rem;
  background: var(--p-primary-color, #2563eb);
  color: #fff;
  border: 0;
  font-weight: 600;
  font-size: 0.9rem;
  cursor: pointer;
}

.head-add:active { transform: scale(0.97); }

.search-input { width: 100%; }

.card-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.cust-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.55rem;
  padding: 0.75rem 0.85rem;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.cust-card:active { background: var(--p-content-hover-background, #f3f4f6); }

.cust-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.cust-name {
  font-weight: 700;
  font-size: 1rem;
  flex: 1;
}

.cust-chevron {
  color: var(--p-text-muted-color, #9ca3af);
  font-size: 0.85rem;
}

.cust-meta {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.meta-item {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.8rem;
  color: var(--p-text-muted-color, #6b7280);
}

.state-msg {
  text-align: center;
  padding: 2.5rem 1rem;
  color: var(--p-text-muted-color, #6b7280);
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.4rem;
}

.empty-icon { font-size: 2rem; opacity: 0.5; }
.empty-title { font-size: 1.05rem; font-weight: 600; }
.empty-help { font-size: 0.85rem; }

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
