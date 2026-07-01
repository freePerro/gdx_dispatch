<template>
    <section class="mobile-inventory">
      <header class="mobile-page-head">
        <div class="head-row">
          <h1>Inventory</h1>
          <Button v-tooltip="'Refresh'" icon="pi pi-refresh" aria-label="Refresh" text size="small" :loading="loading" @click="fetchParts" data-test="mi-refresh" />
        </div>
        <InputText v-model="searchQuery" placeholder="Search SKU or name…" class="search-input" @input="onSearch" data-test="mi-search" />
      </header>

      <div v-if="loading && !parts.length" class="state-msg">
        <i class="pi pi-spin pi-spinner" />
        <span>Loading inventory…</span>
      </div>
      <div v-else-if="!filtered.length" class="state-msg">
        <i class="pi pi-box empty-icon" />
        <div class="empty-title">{{ searchQuery ? 'No matches' : 'No parts' }}</div>
      </div>

      <ol v-else class="card-list">
        <li
          v-for="p in filtered"
          :key="p.id"
          class="part-card"
          :class="{ low: isLowStock(p) }"
          data-test="mi-part-row"
        >
          <div class="part-row">
            <span class="part-name">{{ p.name || '—' }}</span>
            <span class="part-qty">{{ p.quantity_on_hand ?? p.quantity ?? 0 }}</span>
          </div>
          <div class="part-meta">
            <span v-if="p.sku" class="meta-item"><i class="pi pi-tag" /> {{ p.sku }}</span>
            <span v-if="p.location" class="meta-item"><i class="pi pi-map-marker" /> {{ p.location }}</span>
            <span v-if="p.unit_cost != null" class="meta-item"><i class="pi pi-dollar" /> {{ Number(p.unit_cost).toFixed(2) }}</span>
            <span v-if="isLowStock(p)" v-tooltip="'Low stock — at or below reorder point'" class="pill pill-warn">LOW</span>
          </div>
        </li>
      </ol>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useApi } from '../composables/useApi'
import { useToast } from 'primevue/usetoast'

import Button from 'primevue/button'
import InputText from 'primevue/inputtext'

const api = useApi()
const toast = useToast()

const parts = ref([])
const loading = ref(false)
const searchQuery = ref('')

const filtered = computed(() => {
  const q = searchQuery.value.trim().toLowerCase()
  if (!q) return parts.value
  return parts.value.filter((p) => {
    const hay = `${p.name || ''} ${p.sku || ''} ${p.location || ''}`.toLowerCase()
    return hay.includes(q)
  })
})

function isLowStock(p) {
  const qty = Number(p.quantity_on_hand ?? p.quantity ?? 0)
  const reorder = Number(p.reorder_point ?? p.min_stock ?? 0)
  if (!reorder) return qty <= 0
  return qty <= reorder
}

async function fetchParts() {
  loading.value = true
  try {
    const r = await api.get('/api/inventory/parts')
    parts.value = Array.isArray(r) ? r : r?.items || r?.data || []
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Load failed', detail: err.message, life: 4000 })
  } finally {
    loading.value = false
  }
}

let searchTimer = null
function onSearch() {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {}, 100)
}

onMounted(fetchParts)
</script>

<style scoped>
.mobile-inventory {
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

.search-input { width: 100%; }

.card-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.part-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.55rem;
  padding: 0.75rem 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.part-card.low {
  border-left: 3px solid #f59e0b;
}

.part-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
}

.part-name {
  font-weight: 700;
  font-size: 1rem;
  flex: 1;
}

.part-qty {
  font-family: monospace;
  font-weight: 700;
  font-size: 1.05rem;
  background: var(--p-content-hover-background, #f3f4f6);
  padding: 0.1rem 0.55rem;
  border-radius: 0.4rem;
}

.part-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  align-items: center;
}

.meta-item {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  font-size: 0.78rem;
  color: var(--p-text-muted-color, #6b7280);
}

.pill {
  display: inline-flex;
  align-items: center;
  padding: 0.1rem 0.4rem;
  border-radius: 999px;
  font-size: 0.7rem;
  font-weight: 700;
}

.pill-warn {
  background: #f59e0b;
  color: #1f2937;
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

.empty-icon {
  font-size: 2rem;
  opacity: 0.5;
}

.empty-title {
  font-size: 1.05rem;
  font-weight: 600;
}
</style>
