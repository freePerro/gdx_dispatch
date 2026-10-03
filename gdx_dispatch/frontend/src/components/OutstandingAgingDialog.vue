<!--
  Outstanding by age — opened from the Billing page's Total Outstanding card.

  Re-cuts the same receivables the card sums into day ranges the office picks
  ("30, 60, 90" or "15, 45" or anything). Computed client-side over the
  invoices the card itself covers (so a date-filtered card gets a
  date-filtered breakdown); read-only, nothing is written. Every receivable
  lands in exactly one row, and a card total the rows don't reach is called
  out rather than hidden.
-->
<template>
  <Dialog
    :visible="visible"
    modal
    header="Outstanding by age"
    :style="{ width: 'min(760px, 96vw)' }"
    :breakpoints="{ '640px': '100vw' }"
    data-testid="aging-dialog"
    @update:visible="$emit('update:visible', $event)"
  >
    <p v-if="scopeNote" class="muted scope-note" data-testid="aging-scope">{{ scopeNote }}</p>

    <div class="aging-controls">
      <label class="field">
        <span>Age from</span>
        <SelectButton
          v-model="basis"
          :options="basisOptions"
          option-label="label"
          option-value="value"
          :allow-empty="false"
          data-testid="aging-basis"
        />
      </label>
      <label class="field">
        <span>Day ranges (split at)</span>
        <InputText
          v-model="breakpointText"
          placeholder="30, 60, 90"
          data-testid="aging-breakpoints"
        />
      </label>
      <div class="presets">
        <Button
          v-for="p in presets"
          :key="p"
          :label="p"
          size="small"
          severity="secondary"
          text
          :data-testid="`aging-preset-${p.replace(/\D+/g, '-')}`"
          @click="breakpointText = p"
        />
      </div>
    </div>

    <DataTable
      :value="result.rows"
      size="small"
      data-key="key"
      selection-mode="single"
      v-model:selection="selectedRow"
      class="aging-table"
      data-testid="aging-table"
    >
      <Column field="label" header="Age" body-class="nowrap" />
      <Column header="Count" style="text-align: right">
        <template #body="{ data }">{{ data.count }}</template>
      </Column>
      <Column header="Amount" style="text-align: right">
        <template #body="{ data }"><span :data-testid="`aging-row-${data.key}`">{{ currency(data.total) }}</span></template>
      </Column>
      <Column header="Running total">
        <template #body="{ data }">
          <span v-if="data.cumulative !== undefined" class="muted running">
            <small>{{ data.cumulativeLabel }}</small>
            <span>{{ currency(data.cumulative) }}</span>
          </span>
        </template>
      </Column>
      <template #footer>
        <div class="aging-footer" data-testid="aging-total">
          <span>Total — {{ result.count }} invoices</span>
          <strong>{{ currency(result.total) }}</strong>
        </div>
        <small v-if="mismatch" class="muted" data-testid="aging-mismatch">
          The card shows {{ currency(cardTotal) }}, but these rows add up to
          {{ currency(result.total) }}. Refresh the page to reload the invoice list.
        </small>
      </template>
    </DataTable>

    <div v-if="selectedRow" class="aging-detail" data-testid="aging-detail">
      <h4>{{ selectedRow.label }} — {{ selectedRow.count }} invoices</h4>
      <DataTable
        :value="selectedRow.invoices"
        size="small"
        scrollable
        scroll-height="260px"
        data-key="id"
      >
        <Column header="Invoice">
          <template #body="{ data }">
            <a href="#" @click.prevent="openInvoice(data)">{{ data.invoice_number }}</a>
          </template>
        </Column>
        <Column field="customer_name" header="Customer" />
        <Column :header="basis === 'due' ? 'Due' : 'Issued'">
          <template #body="{ data }">{{ formatDate(basis === 'due' ? data.due_date : (data.invoice_date || data.created_at)) }}</template>
        </Column>
        <Column header="Days">
          <template #body="{ data }">{{ data.age_days ?? '—' }}</template>
        </Column>
        <Column header="Balance" style="text-align: right">
          <template #body="{ data }">{{ currency(data.balance_due) }}</template>
        </Column>
      </DataTable>
    </div>
    <small v-else class="muted">Click a row to see its invoices.</small>
  </Dialog>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import { useRouter } from "vue-router";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import SelectButton from "primevue/selectbutton";
import { formatDate, formatMoney as currency } from "../composables/useFormatters";
import { bucketOutstanding, parseBreakpoints, DEFAULT_BREAKPOINTS } from "../utils/arAging";

const props = defineProps({
  visible: { type: Boolean, default: false },
  invoices: { type: Array, default: () => [] },
  // The number on the Total Outstanding card — the rows must reach it.
  cardTotal: { type: Number, default: null },
  // Shop's 'YYYY-MM-DD' (tenant timezone), so ages match the server's Overdue.
  today: { type: String, default: null },
  // e.g. "Invoices issued This year" when the page's date filter scopes the card.
  scopeNote: { type: String, default: "" },
});
defineEmits(["update:visible"]);

const router = useRouter();

const basisOptions = [
  { label: "Invoice date", value: "invoice" },
  { label: "Due date", value: "due" },
];
const presets = ["30, 60, 90", "15, 30, 45, 60", "60, 120"];

// The office's last choice sticks between visits (this browser only).
const PREFS_KEY = "gdx_billing_aging";
function loadPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch { return {}; }
}
const saved = loadPrefs();
const basis = ref(saved.basis === "due" ? "due" : "invoice");
const breakpointText = ref(typeof saved.ranges === "string" ? saved.ranges : DEFAULT_BREAKPOINTS.join(", "));
watch([basis, breakpointText], ([b, r]) => {
  try { localStorage.setItem(PREFS_KEY, JSON.stringify({ basis: b, ranges: r })); } catch { /* private mode */ }
});
const selectedRow = ref(null);

const result = computed(() => bucketOutstanding(props.invoices, {
  breakpoints: parseBreakpoints(breakpointText.value),
  basis: basis.value,
  today: props.today || new Date(),
}));

// Re-bucketing rebuilds the rows; keep the drill-down on the same row if it
// still exists, otherwise close it rather than show a stale invoice list.
watch(result, (r) => {
  if (!selectedRow.value) return;
  selectedRow.value = r.rows.find((row) => row.key === selectedRow.value.key) || null;
});

const mismatch = computed(() =>
  typeof props.cardTotal === "number"
  && Math.abs(props.cardTotal - result.value.total) >= 0.005
);

function openInvoice(inv) {
  if (inv?.id) router.push(`/billing/${inv.id}`);
}
</script>

<style scoped>
.aging-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 0.75rem 1rem;
  margin-bottom: 1rem;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.85rem;
}
.scope-note { margin: 0 0 0.75rem; }
.presets { display: flex; flex-wrap: wrap; gap: 0.25rem; }
.aging-footer {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
}
.aging-detail { margin-top: 1rem; }
.aging-detail h4 { margin: 0 0 0.5rem; }
.muted { color: var(--p-text-muted-color, #6b7280); }
.aging-table :deep(tr) { cursor: pointer; }
.aging-table :deep(.nowrap) { white-space: nowrap; }
.running { display: flex; flex-direction: column; white-space: nowrap; }
@media (max-width: 640px) {
  .aging-table :deep(td), .aging-table :deep(th) { padding: 0.4rem 0.35rem; }
  .running { font-size: 0.85rem; }
}
</style>
