<template>
    <section class="delivery-loadsheet view-card">
      <div class="loadsheet-header">
        <div>
          <h2 class="page-title">Delivery Load Sheet</h2>
          <p class="subtitle">{{ supplierName }} — {{ formattedDate }}</p>
        </div>
        <div class="header-right">
          <DatePicker v-model="selectedDate" dateFormat="yy-mm-dd" :showIcon="true"
            class="date-picker" @date-select="loadData" />
          <Tag :value="`${checkedCount} / ${totalItems} loaded`"
            :severity="checkedCount === totalItems && totalItems > 0 ? 'success' : 'info'" class="progress-tag" />
        </div>
      </div>

      <div v-if="loading" class="loading-state"><ProgressSpinner /></div>

      <div v-else-if="!stops.length" class="empty-state">
        <i class="pi pi-truck" style="font-size:3rem;color:var(--p-text-muted-color)"></i>
        <p>No deliveries scheduled.</p>
      </div>

      <template v-else>
        <!-- Summary bar -->
        <div class="summary-bar">
          <div class="summary-item"><strong>{{ stops.length }}</strong> stops</div>
          <div class="summary-item"><strong>{{ totalItems }}</strong> line items</div>
          <div class="summary-item"><strong>{{ totalQty }}</strong> total units</div>
        </div>

        <!-- Each stop -->
        <div v-for="(stop, idx) in stops" :key="stop.order_id" class="stop-card">
          <div class="stop-header">
            <div class="stop-number">Stop {{ idx + 1 }}</div>
            <div class="stop-info">
              <strong>{{ stop.dealer }}</strong>
              <span v-if="stop.address" class="stop-address">{{ stop.address }}</span>
              <Tag :value="stop.status" :severity="stop.status === 'confirmed' ? 'success' : 'warning'" size="small" />
            </div>
            <div class="stop-total">{{ formatMoney(stop.total_amount) }}</div>
          </div>

          <div v-if="stop.notes" class="stop-notes">
            <i class="pi pi-info-circle"></i> {{ stop.notes }}
          </div>

          <div class="stop-items">
            <div v-for="item in stop.items" :key="item.sku + item.name" class="load-item"
              :class="{ checked: item.checked }" @click="item.checked = !item.checked">
              <Checkbox v-model="item.checked" :binary="true" />
              <div class="item-info">
                <span class="item-name">{{ item.name }}</span>
                <span v-if="item.sku" class="item-sku">SKU: {{ item.sku }}</span>
              </div>
              <span class="item-qty">×{{ item.quantity }}</span>
            </div>
          </div>
        </div>
      </template>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApi } from "../composables/useApi";
import { formatDate, formatMoney } from "../composables/useFormatters";
import Checkbox from "primevue/checkbox";
import DatePicker from "primevue/datepicker";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";

const api = useApi();
const loading = ref(true);
const supplierName = ref("");
const selectedDate = ref(new Date());
const stops = ref([]);

const formattedDate = computed(() => {
  const d = selectedDate.value instanceof Date ? selectedDate.value : new Date(selectedDate.value);
  return formatDate(d, { options: { weekday: "long", month: "long", day: "numeric", year: "numeric" } });
});

const totalItems = computed(() => stops.value.reduce((s, st) => s + st.items.length, 0));
const totalQty = computed(() => stops.value.reduce((s, st) => s + st.items.reduce((q, i) => q + i.quantity, 0), 0));
const checkedCount = computed(() => stops.value.reduce((s, st) => s + st.items.filter((i) => i.checked).length, 0));

function dateStr(d) {
  const dt = d instanceof Date ? d : new Date(d);
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
}

async function loadData() {
  loading.value = true;
  try {
    // supplier_id would come from supplier auth session — for now use query param or stored value
    const sid = sessionStorage.getItem("supplier_id") || "";
    if (!sid) { stops.value = []; return; }
    const data = await api.get(`/api/supplier/portal/delivery-loadsheet?supplier_id=${sid}&date=${dateStr(selectedDate.value)}`);
    supplierName.value = data.supplier_name || "Supplier";
    stops.value = (data.stops || []).map((s) => ({
      ...s,
      items: s.items.map((i) => ({ ...i, checked: false })),
    }));
  } catch {
    stops.value = [];
  } finally {
    loading.value = false;
  }
}

onMounted(loadData);
</script>

<style scoped>
.delivery-loadsheet { max-width: 700px; margin: 0 auto; }

.loadsheet-header { display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 1rem; margin-bottom: 1rem; }
.page-title { margin: 0; font-size: 1.4rem; font-weight: 700; }
.subtitle { color: var(--p-text-muted-color); font-size: 0.9rem; margin-top: 0.2rem; }
.header-right { display: flex; align-items: center; gap: 0.75rem; }
.date-picker { max-width: 160px; }
.progress-tag { font-size: 0.85rem; padding: 0.3rem 0.7rem; }

.summary-bar { display: flex; gap: 1.5rem; padding: 0.75rem 1rem; background: var(--p-content-hover-background, #1e293b); border-radius: 8px; margin-bottom: 1rem; }
.summary-item { font-size: 0.9rem; color: var(--p-text-muted-color); }
.summary-item strong { color: var(--p-text-color); font-size: 1.1rem; }

.stop-card { background: var(--p-content-hover-background, #1e293b); border: 1px solid var(--p-content-border-color, #334155); border-radius: 10px; margin-bottom: 1rem; overflow: hidden; }
.stop-header { display: flex; align-items: center; gap: 0.75rem; padding: 0.75rem 1rem; border-bottom: 1px solid var(--p-content-border-color, #334155); }
.stop-number { background: var(--p-primary-color, #3b82f6); color: white; font-weight: 800; font-size: 0.8rem; padding: 0.25rem 0.6rem; border-radius: 4px; flex-shrink: 0; }
.stop-info { flex: 1; }
.stop-info strong { display: block; font-size: 1rem; }
.stop-address { display: block; font-size: 0.78rem; color: var(--p-text-muted-color); }
.stop-total { font-weight: 700; font-size: 1.1rem; color: var(--p-primary-color); }

.stop-notes { padding: 0.5rem 1rem; font-size: 0.82rem; color: var(--p-text-muted-color); background: rgba(255, 255, 255, 0.03); border-bottom: 1px solid var(--p-content-border-color, #334155); }

.stop-items { padding: 0.25rem 0; }
.load-item { display: flex; align-items: center; gap: 0.75rem; padding: 0.6rem 1rem; cursor: pointer; transition: opacity 0.15s; }
.load-item:hover { background: rgba(255, 255, 255, 0.03); }
.load-item.checked { opacity: 0.45; }
.item-info { flex: 1; }
.item-name { font-weight: 600; font-size: 0.95rem; }
.load-item.checked .item-name { text-decoration: line-through; }
.item-sku { display: block; font-size: 0.72rem; color: var(--p-text-muted-color); font-family: monospace; }
.item-qty { font-family: monospace; font-weight: 700; font-size: 1.05rem; background: var(--p-content-background, #0f172a); padding: 0.15rem 0.5rem; border-radius: 4px; }

.loading-state { display: flex; justify-content: center; padding: 3rem; }
.empty-state { text-align: center; padding: 3rem; color: var(--p-text-muted-color); display: flex; flex-direction: column; align-items: center; gap: 0.5rem; }

/* Mobile: stack the loadsheet header so the date picker + progress tag
   wrap below the title rather than crunching it. Pad bottom for the
   bottom-nav. */
@media (max-width: 768px) {
  .delivery-loadsheet { padding: 0.75rem 0.75rem calc(5rem + env(safe-area-inset-bottom)); }
  .loadsheet-header { flex-direction: column; align-items: stretch; gap: 0.5rem; }
  .header-right { flex-wrap: wrap; }
  .date-picker { max-width: none; flex: 1; }
  .summary-bar { gap: 1rem; padding: 0.6rem 0.75rem; flex-wrap: wrap; }
  .stop-header { flex-wrap: wrap; gap: 0.4rem; padding: 0.6rem 0.75rem; }
  .stop-info { width: 100%; order: 2; }
  .stop-number { order: 0; }
  .stop-total { order: 1; margin-left: auto; }
  .load-item { padding: 0.7rem 0.85rem; }
  .item-name { font-size: 0.95rem; }
  .item-qty { font-size: 1rem; }
}
</style>
