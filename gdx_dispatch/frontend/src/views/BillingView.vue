<template>
    <section class="billing-view view-card">
      <!-- Summary Cards -->
      <div class="summary-cards">
        <Card data-testid="billing-total-outstanding">
          <template #title>Total Outstanding</template>
          <template #content><p class="stat-value outstanding">{{ currency(totalOutstanding) }}</p></template>
        </Card>
        <Card data-testid="billing-overdue-amount">
          <template #title>Overdue</template>
          <template #content><p class="stat-value overdue">{{ currency(overdueAmount) }}</p></template>
        </Card>
        <Card data-testid="billing-paid-this-month">
          <template #title>Paid This Month</template>
          <template #content><p class="stat-value paid">{{ currency(paidThisMonth) }}</p></template>
        </Card>
        <!-- PR1-billing-capture: drafts are excluded from Outstanding (they
             aren't receivables yet) which made never-sent invoices invisible
             to every KPI. Surface them; click filters the list to Draft. -->
        <Card data-testid="billing-draft-invoices" class="draft-card" @click="activeStatus = 'Draft'">
          <template #title>Unsent Drafts</template>
          <template #content>
            <p class="stat-value drafts">
              {{ draftCount }}
              <span class="draft-total">({{ currency(draftTotal) }})</span>
            </p>
          </template>
        </Card>
      </div>

      <!-- Ready for Billing Queue -->
      <Card v-if="readyJobs.length" class="ready-billing-card" data-testid="ready-for-billing">
        <template #title>
          <div class="flex align-items-center gap-2">
            <i class="pi pi-check-circle" style="color: var(--p-green-500)" />
            Ready for Billing
            <Tag :value="String(readyJobs.length)" severity="warn" rounded />
          </div>
        </template>
        <template #content>
          <DataTable
      responsiveLayout="scroll" :value="readyJobs" :rows="5" size="small" stripedRows>
            <Column field="customer_name" header="Customer" />
            <Column field="title" header="Job" />
            <Column header="Action" style="width: 16rem">
              <template #body="{ data }">
                <!-- Review FIRST — Doug 2026-05-10: completed jobs need a
                     double-check + a chance to add parts before invoicing,
                     not a one-click ship-it. The Review path navigates
                     to job detail where parts/labor/notes can be confirmed. -->
                <Button label="Review" icon="pi pi-search" size="small" severity="secondary"
                  class="mr-2"
                  @click="reviewJob(data)" data-testid="review-job-before-billing" />
                <Button label="Create Invoice" icon="pi pi-dollar" size="small" severity="success"
                  @click="createInvoiceForJob(data)" data-testid="create-invoice-for-job" />
              </template>
            </Column>
          </DataTable>
        </template>
      </Card>

      <!-- PR4-billing-capture: parts a tech recorded as used on a COMPLETED
           job that never reached an invoice — the leak Ready-for-Billing
           misses (the invoice may already be out). Review takes the office
           to the job to bill or dismiss them. -->
      <Card v-if="leakedParts.length" class="ready-billing-card" data-testid="unbilled-parts-review">
        <template #title>
          <div class="flex align-items-center gap-2">
            <i class="pi pi-exclamation-triangle" style="color: var(--p-amber-500)" />
            Parts used, never billed
            <Tag :value="String(leakedParts.length)" severity="danger" rounded />
          </div>
        </template>
        <template #content>
          <DataTable responsiveLayout="scroll" :value="leakedParts" :rows="5" size="small" stripedRows>
            <Column field="customer_name" header="Customer" />
            <Column field="job_title" header="Job" />
            <Column header="Parts">
              <template #body="{ data }">
                {{ data.parts.length }} part(s)
                <small class="muted" v-if="data.suggested_total">
                  · ≈{{ currency(data.suggested_total) }}
                </small>
              </template>
            </Column>
            <Column header="Action" style="width: 16rem">
              <template #body="{ data }">
                <Button label="Review" icon="pi pi-search" size="small" severity="secondary"
                  class="mr-2"
                  @click="reviewJob({ id: data.job_id })" data-testid="review-unbilled-parts" />
                <!-- PR4 audit round 2: without a dismiss verb the card floods
                     with history and becomes wallpaper. wont_bill keeps the
                     audit trail but leaves every billing surface. -->
                <Button label="Won't bill" icon="pi pi-ban" size="small" severity="danger" text
                  @click="dismissLeakedParts(data)" data-testid="dismiss-unbilled-parts" />
              </template>
            </Column>
          </DataTable>
        </template>
      </Card>

      <!-- Toolbar: Search + Date filter + Create -->
      <div class="billing-toolbar">
        <span class="p-input-icon-left search-wrap">
          <InputText
            id="billing-search"
            name="billing-search"
            v-model="searchQuery"
            placeholder="Search invoices..."
            data-testid="billing-search"
          />
        </span>
        <Select
          v-model="datePreset"
          :options="datePresetOptions"
          option-label="label"
          option-value="value"
          placeholder="Date range"
          data-testid="billing-date-preset"
          class="date-preset-select"
        />
        <DatePicker
          v-if="datePreset === 'custom'"
          v-model="customRange"
          selectionMode="range"
          dateFormat="yy-mm-dd"
          placeholder="Pick start & end"
          data-testid="billing-custom-range"
          showIcon
          class="custom-range-picker"
        />
        <span v-if="dateRange[0] || dateRange[1]" class="date-range-summary" data-testid="billing-date-summary">
          {{ formatDate(dateRange[0]) || '…' }} → {{ formatDate(dateRange[1]) || '…' }}
          <Button
            v-tooltip="'Clear date filter'"
            icon="pi pi-times"
            text
            rounded
            size="small"
            aria-label="Clear date filter"
            @click="datePreset = 'all'; customRange = [null, null]"
            data-testid="billing-date-clear"
          />
        </span>
        <Button
          label="Export"
          icon="pi pi-download"
          aria-label="Export CSV"
          text
          data-testid="billing-export-btn"
          @click="exportInvoices"
        />
        <Button
          label="Counter Sale"
          icon="pi pi-shopping-bag"
          severity="secondary"
          data-testid="create-counter-sale-btn"
          @click="$router.push('/billing/new?counter=1')"
        />
        <Button
          label="Create Invoice"
          icon="pi pi-plus"
          data-testid="create-invoice-btn"
          @click="$router.push('/billing/new')"
        />
      </div>

      <!-- Status Filters -->
      <div class="status-tabs">
        <Button
          v-for="tab in statusTabs"
          :key="tab.value"
          :label="`${tab.label} (${tabCount(tab.value)})`"
          :severity="activeStatus === tab.value ? undefined : 'secondary'"
          :data-testid="`billing-status-${tab.value.toLowerCase()}`"
          size="small"
          @click="activeStatus = tab.value"
        />
      </div>

      <!-- Bulk Actions Bar (visible when selection) -->
      <div v-if="selectedInvoices.length > 0" class="bulk-actions-bar" data-testid="bulk-actions-bar">
        <span class="bulk-count">{{ selectedInvoices.length }} selected</span>
        <Button label="Send Selected" icon="pi pi-send" size="small" severity="info" @click="bulkSend" :disabled="bulkProgress.active" :loading="bulkProgress.active && bulkProgress.label === 'Send'" data-testid="bulk-send" />
        <Button label="Mark Paid" icon="pi pi-check" size="small" severity="success" @click="bulkMarkPaid" :disabled="bulkProgress.active" :loading="bulkProgress.active && bulkProgress.label === 'Mark Paid'" data-testid="bulk-mark-paid" />
        <Button label="Export CSV" icon="pi pi-download" size="small" severity="secondary" @click="bulkExport" :disabled="bulkProgress.active" />
        <Button label="Delete" icon="pi pi-trash" aria-label="Delete" size="small" severity="danger" text @click="bulkDelete" :disabled="bulkProgress.active" :loading="bulkProgress.active && bulkProgress.label === 'Delete'" />
        <Button label="Clear" icon="pi pi-times" aria-label="Remove" size="small" text @click="selectedInvoices = []" :disabled="bulkProgress.active" />
      </div>

      <!-- Bulk Progress (visible while a bulk loop is running) -->
      <div v-if="bulkProgress.active" class="bulk-progress-row" data-testid="bulk-progress">
        <ProgressBar
          :value="Math.round((bulkProgress.completed / Math.max(bulkProgress.total, 1)) * 100)"
          style="height: 0.5rem; flex: 1;"
        />
        <span class="bulk-progress-label">{{ bulkProgress.label }}: {{ bulkProgress.completed }} of {{ bulkProgress.total }}</span>
      </div>

      <!-- Invoice Table -->
      <!-- Custom selection column below (not PrimeVue selectionMode="multiple")
           because PrimeVue's built-in header checkbox renders without id/name
           and triggers a Chrome DevTools Issues warning we can't silence. -->
      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        :value="paginatedInvoices"
        :loading="loading"
        data-testid="billing-datatable"
        stripedRows
        @row-click="onRowClick"
        :rowHover="true"
        :sortField="sortField"
        :sortOrder="sortOrder"
        @sort="onSort"
        dataKey="id"
      >
        <template #empty>
          <EmptyState
            icon="pi pi-file"
            :title="searchQuery || activeStatus !== 'All' ? 'No invoices match your filters' : 'No invoices yet'"
            :message="searchQuery || activeStatus !== 'All' ? 'Try clearing your search, status, or date filter.' : 'Click &quot;Create Invoice&quot; to start.'"
          />
        </template>
        <Column headerStyle="width:3rem" bodyStyle="width:3rem">
          <template #header>
            <input
              id="billing-select-all"
              name="billing-select-all"
              type="checkbox"
              :checked="isAllSelected"
              :indeterminate.prop="isSomeSelected"
              aria-label="Select all invoices"
              data-testid="billing-select-all"
              @click.stop
              @change="toggleSelectAll"
            />
          </template>
          <template #body="{ data }">
            <input
              :id="'billing-select-' + data.id"
              :name="'billing-select-' + data.id"
              type="checkbox"
              :checked="isSelected(data)"
              :aria-label="'Select invoice ' + (data.invoice_number || data.id)"
              :data-testid="'billing-select-' + data.id"
              @click.stop
              @change="toggleSelection(data)"
            />
          </template>
        </Column>
        <Column field="invoice_number" header="Invoice #" sortable>
          <template #body="{ data }">
            <!-- Real anchor href so right-click → open in new tab works
                 and the URL is bookmarkable. SPA navigation handled by
                 router-link when present, falls back to native link. -->
            <router-link
              :to="`/billing/${data.id}`"
              class="link-text"
              :data-testid="`invoice-link-${data.id}`"
            >{{ data.invoice_number }}</router-link>
          </template>
        </Column>
        <Column field="customer_name" header="Customer" sortable />
        <Column field="total" header="Amount" sortable>
          <template #body="{ data }">{{ currency(data.total) }}</template>
        </Column>
        <Column field="status" header="Status" sortable>
          <template #body="{ data }">
            <Tag :value="data.status" :severity="statusSeverity(data.status)" data-testid="invoice-status-tag" />
          </template>
        </Column>
        <Column field="due_date" header="Due Date" sortable>
          <template #body="{ data }">{{ formatDate(data.due_date) }}</template>
        </Column>
        <Column header="Actions" style="width: 220px">
          <template #body="{ data }">
            <div class="action-btns">
              <Button v-if="data.status === 'Draft'" icon="pi pi-send" aria-label="Send" severity="info" text size="small"
                v-tooltip="'Send'" :data-testid="`send-invoice-${data.id}`" @click.stop="sendInvoice(data)" />
              <Button icon="pi pi-file-pdf" aria-label="Download PDF" severity="secondary" text size="small"
                v-tooltip="'Download PDF'" :data-testid="`pdf-invoice-${data.id}`" @click.stop="downloadPdf(data)" />
              <Button
                v-if="data.status !== 'Paid'"
                icon="pi pi-link"
                aria-label="Copy pay link"
                severity="primary"
                text
                size="small"
                v-tooltip="'Copy online pay link'"
                :loading="payingInvoiceId === data.id"
                :data-testid="`pay-invoice-${data.id}`"
                @click.stop="copyPayLink(data)"
              />
              <Button v-if="data.status !== 'Paid'" icon="pi pi-dollar" aria-label="Record Payment" severity="success" text size="small"
                v-tooltip="'Record Payment'" :data-testid="`record-payment-${data.id}`" @click.stop="openPaymentDialog(data)" />
              <Button icon="pi pi-pencil" aria-label="Edit" severity="secondary" text size="small"
                v-tooltip="'Edit'" @click.stop="editInvoice(data)" />
              <Button icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small"
                v-tooltip="'Delete'" :data-testid="`delete-invoice-${data.id}`" @click.stop="confirmDelete(data)" />
            </div>
          </template>
        </Column>
      </DataTable>

      <!-- Pagination -->
      <div class="pagination-bar" v-if="totalPages > 1">
        <Button
          v-tooltip="'Previous page'"
          aria-label="Previous page"
          icon="pi pi-angle-left"
          severity="secondary"
          text
          :disabled="currentPage <= 1"
          @click="currentPage--"
        />
        <span class="page-info">Page {{ currentPage }} of {{ totalPages }}</span>
        <Button
          v-tooltip="'Next page'"
          aria-label="Next page"
          icon="pi pi-angle-right"
          severity="secondary"
          text
          :disabled="currentPage >= totalPages"
          @click="currentPage++"
        />
      </div>

      <!-- S122: Create-invoice dialog retired. + New Invoice and per-row
           "Create Invoice for Job" buttons now route to /billing/new. -->

      <!-- Record Payment Dialog -->
      <Dialog
        v-model:visible="showPaymentDialog"
        header="Record Payment"
        modal
        :style="{ width: '480px' }"
        data-testid="record-payment-dialog"
      >
        <div class="form-grid" v-if="paymentTarget">
          <div class="form-field full-width">
            <p><strong>Invoice:</strong> {{ paymentTarget.invoice_number }}</p>
            <p><strong>Balance Due:</strong> {{ currency(paymentTarget.balance_due || paymentTarget.total) }}</p>
          </div>
          <div class="form-field">
            <label for="pay-amount">Amount *</label>
            <InputNumber
              id="pay-amount"
              v-model="newPayment.amount"
              mode="currency"
              currency="USD"
              locale="en-US"
              :min="0.01"
              data-testid="payment-amount"
            />
          </div>
          <div class="form-field">
            <label for="pay-method">Payment Method *</label>
            <Select
              id="pay-method"
              v-model="newPayment.method"
              :options="paymentMethods"
              data-testid="payment-method"
            />
          </div>
          <div class="form-field full-width">
            <label for="pay-reference">Reference #</label>
            <InputText
              id="pay-reference"
              v-model="newPayment.reference"
              placeholder="Check #, confirmation, etc."
              data-testid="payment-reference"
            />
          </div>
        </div>

        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showPaymentDialog = false" />
          <Button
            label="Record Payment"
            data-testid="confirm-record-payment"
            :disabled="!newPayment.amount || !newPayment.method"
            :loading="recordingPayment"
            @click="recordPayment"
          />
        </template>
      </Dialog>

      <Dialog
        v-model:visible="showBulkPaidDialog"
        header="Mark Paid — record payments"
        modal
        :style="{ width: '420px' }"
        data-testid="bulk-mark-paid-dialog"
      >
        <p>
          Records a payment for each selected invoice's remaining balance
          ({{ selectedInvoices.length }} selected; already-paid and void
          invoices are skipped).
        </p>
        <div class="form-field">
          <label for="bulk-paid-method">Payment Method *</label>
          <Select
            id="bulk-paid-method"
            v-model="bulkPaidMethod"
            :options="paymentMethods"
            data-testid="bulk-paid-method"
          />
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showBulkPaidDialog = false" />
          <Button
            label="Record Payments"
            data-testid="bulk-mark-paid-confirm"
            :disabled="!bulkPaidMethod"
            @click="confirmBulkMarkPaid"
          />
        </template>
      </Dialog>

      <!-- ConfirmDialog removed 2026-05-12 — AppLayout.vue:49 mounts one globally. -->
      <Toast data-testid="billing-toast" />
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useRouter, useRoute } from "vue-router";
import { useToast } from "primevue/usetoast";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import { formatDate, formatMoney as currency } from "../composables/useFormatters";
import { openAuthedFile } from "../composables/useAuthedFile";
import { useListPrefs } from "../composables/useListPrefs";
import { useTableExport } from "../composables/useTableExport";
import Button from "primevue/button";
import DatePicker from "primevue/datepicker";
import Card from "primevue/card";
import ProgressBar from "primevue/progressbar";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Select from "primevue/select";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import Tag from "primevue/tag";
import Toast from "primevue/toast";
import EmptyState from "../components/EmptyState.vue";
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync, confirmDestructive } = useDestructiveConfirm();

const router = useRouter();
const route = useRoute();
const api = useApi();
const toast = useToast();

// --- State ---
const loading = ref(false);
const readyJobs = ref([]);
// PR4 — /api/parts-needed/unbilled-consumed: parts used on completed jobs
// that never reached an invoice, grouped per job.
const leakedParts = ref([]);
const creating = ref(false);
const recordingPayment = ref(false);
const invoices = ref([]);
const customers = ref([]);
const jobs = ref([]);
const searchQuery = ref("");
const activeStatus = ref("All");

// Date filter — preset + optional custom range
const datePreset = ref("all");
const customRange = ref([null, null]);
const datePresetOptions = [
  { label: "All time", value: "all" },
  { label: "Today", value: "today" },
  { label: "Yesterday", value: "yesterday" },
  { label: "Last 7 days", value: "7d" },
  { label: "Last 30 days", value: "30d" },
  { label: "Last 90 days", value: "90d" },
  { label: "This month", value: "this_month" },
  { label: "Last month", value: "last_month" },
  { label: "This quarter", value: "this_quarter" },
  { label: "This year", value: "this_year" },
  { label: "Last year", value: "last_year" },
  { label: "Custom range", value: "custom" },
];

function _startOfDay(d) { const x = new Date(d); x.setHours(0, 0, 0, 0); return x; }
function _endOfDay(d) { const x = new Date(d); x.setHours(23, 59, 59, 999); return x; }

const dateRange = computed(() => {
  const now = new Date();
  const today = _startOfDay(now);
  switch (datePreset.value) {
    case "all": return [null, null];
    case "today": return [today, _endOfDay(now)];
    case "yesterday": {
      const y = new Date(today); y.setDate(y.getDate() - 1);
      return [y, _endOfDay(y)];
    }
    case "7d": {
      const s = new Date(today); s.setDate(s.getDate() - 6);
      return [s, _endOfDay(now)];
    }
    case "30d": {
      const s = new Date(today); s.setDate(s.getDate() - 29);
      return [s, _endOfDay(now)];
    }
    case "90d": {
      const s = new Date(today); s.setDate(s.getDate() - 89);
      return [s, _endOfDay(now)];
    }
    case "this_month": {
      const s = new Date(now.getFullYear(), now.getMonth(), 1);
      return [s, _endOfDay(now)];
    }
    case "last_month": {
      const s = new Date(now.getFullYear(), now.getMonth() - 1, 1);
      const e = _endOfDay(new Date(now.getFullYear(), now.getMonth(), 0));
      return [s, e];
    }
    case "this_quarter": {
      const q = Math.floor(now.getMonth() / 3);
      const s = new Date(now.getFullYear(), q * 3, 1);
      return [s, _endOfDay(now)];
    }
    case "this_year": return [new Date(now.getFullYear(), 0, 1), _endOfDay(now)];
    case "last_year": {
      const s = new Date(now.getFullYear() - 1, 0, 1);
      const e = _endOfDay(new Date(now.getFullYear() - 1, 11, 31));
      return [s, e];
    }
    case "custom": {
      const [a, b] = customRange.value || [];
      return [a ? _startOfDay(a) : null, b ? _endOfDay(b) : null];
    }
    default: return [null, null];
  }
});
const currentPage = ref(1);
const perPage = 20;
const selectedInvoices = ref([]);

// Sort state, owned externally so sort applies to the FULL filtered set
// before pagination slices it. Without this, PrimeVue's `sortable` only
// sorts the visible page (the 20 rows in `paginatedInvoices`), making
// "sort by Status" appear broken when there are 305 invoices and the
// Paid ones live mostly on pages 2+. Fix: DataTable runs in controlled
// sort mode via :sortField + :sortOrder; @sort updates the refs; the
// `sortedInvoices` computed applies the sort to `filteredInvoices`;
// `paginatedInvoices` slices the sorted set.
const sortField = ref(null);
const sortOrder = ref(null);
function onSort(event) {
  sortField.value = event.sortField || null;
  sortOrder.value = event.sortOrder || null;
  currentPage.value = 1;
}
const payingInvoiceId = ref(null);

// Phase 12 — bulk-action progress state. Driven by bulkSend / bulkMarkPaid /
// bulkDelete; rendered as a ProgressBar above the invoice table.
const bulkProgress = ref({ active: false, label: '', completed: 0, total: 0 });

// TD-015: bulk financial ops — track per-invoice success/failure so we
// don't claim "all done" when some 500'd silently. The previous code
// swallowed every failure and toasted blanket success regardless.
async function bulkSend() {
  const total = selectedInvoices.value.length;
  if (!(await confirmAsync({ header: 'Confirm', message: `Send ${total} invoice(s) to customers?` }))) return;
  let ok = 0;
  const failed = [];
  bulkProgress.value = { active: true, label: 'Send', completed: 0, total };
  for (const inv of selectedInvoices.value) {
    try {
      // 2026-07-20 (audit catch): the old catch-block "fallback" PATCHed the
      // status to Sent and counted it a success even when NO email went out —
      // and a 200 with email_sent=false was also counted as delivered. Now
      // only an acknowledged delivery counts; everything else is reported.
      const res = await api.post(`/api/invoices/${inv.id}/send`, {});
      const data = res?.data || res;
      if (data && data.email_sent === false) {
        failed.push({ id: inv.id, number: inv.invoice_number, err: data.email_skip_reason || "email not delivered" });
      } else {
        ok += 1;
      }
    } catch (e) {
      failed.push({ id: inv.id, number: inv.invoice_number, err: String(e?.message || e) });
    }
    bulkProgress.value.completed += 1;
  }
  bulkProgress.value = { active: false, label: '', completed: 0, total: 0 };
  if (failed.length === 0) {
    toast.add({ severity: "success", summary: "Bulk Send", detail: `${ok} invoice(s) sent`, life: 3000 });
  } else {
    toast.add({
      severity: ok > 0 ? "warn" : "error",
      summary: ok > 0 ? "Bulk Send — partial failure" : "Bulk Send failed",
      detail: `${ok}/${total} emailed. Not delivered: ${failed.map((f) => f.number || f.id).join(", ")} — open each and use Re-send.`,
      life: 8000,
    });
  }
  selectedInvoices.value = [];
  await loadData();
}

// "Mark Paid" records a real payment for each remaining balance — PATCHing
// {status: 'Paid'} always 422'd (InvoicePatchIn forbids status) and would
// desync status from balance_due even if it worked. The dialog asks for the
// payment method because that's what feeds the GL cash account.
const showBulkPaidDialog = ref(false);
const bulkPaidMethod = ref('Check');

async function bulkMarkPaid() {
  bulkPaidMethod.value = 'Check';
  showBulkPaidDialog.value = true;
}

async function confirmBulkMarkPaid() {
  showBulkPaidDialog.value = false;
  const targets = selectedInvoices.value.filter(
    (inv) => inv.status !== 'Paid' && inv.status !== 'Void',
  );
  const total = targets.length;
  let ok = 0;
  let skipped = selectedInvoices.value.length - total;
  const failed = [];
  const today = new Date().toISOString().slice(0, 10);
  bulkProgress.value = { active: true, label: 'Mark Paid', completed: 0, total };
  for (const inv of targets) {
    const balance = toNum(inv.balance_due ?? inv.total);
    if (balance <= 0) {
      skipped += 1;
      bulkProgress.value.completed += 1;
      continue;
    }
    try {
      await api.post(`/api/invoices/${inv.id}/payments`, {
        amount: balance,
        method: bulkPaidMethod.value,
        date: today,
        reference: 'bulk mark-paid',
      });
      ok += 1;
    } catch (e) {
      failed.push({ id: inv.id, number: inv.invoice_number, err: String(e?.message || e) });
    }
    bulkProgress.value.completed += 1;
  }
  bulkProgress.value = { active: false, label: '', completed: 0, total: 0 };
  const skippedNote = skipped > 0 ? ` (${skipped} already paid/void skipped)` : '';
  if (failed.length === 0) {
    toast.add({ severity: "success", summary: "Marked Paid", detail: `${ok} payment(s) recorded${skippedNote}`, life: 4000 });
  } else {
    toast.add({
      severity: ok > 0 ? "warn" : "error",
      summary: ok > 0 ? "Mark Paid — partial failure" : "Mark Paid failed",
      detail: `${ok}/${total} recorded${skippedNote}. Failed: ${failed.map((f) => f.number || f.id).join(", ")}`,
      life: 6000,
    });
  }
  selectedInvoices.value = [];
  await loadData();
}

function bulkExport() {
  const headers = ["Invoice #", "Customer", "Amount", "Status", "Due Date"];
  const rows = selectedInvoices.value.map((i) => [
    i.invoice_number || "", i.customer_name || "", i.total || 0, i.status || "", i.due_date || "",
  ]);
  const csv = [headers, ...rows].map((row) => row.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(",")).join("\n");
  const blob = new Blob([csv], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `invoices-${new Date().toISOString().split("T")[0]}.csv`;
  a.click();
  URL.revokeObjectURL(url);
  toast.add({ severity: "success", summary: "Exported", detail: `${selectedInvoices.value.length} invoices exported to CSV`, life: 3000 });
}

async function bulkDelete() {
  const total = selectedInvoices.value.length;
  if (!(await confirmAsync({ header: 'Confirm', message: `Delete ${total} invoice(s)? This cannot be undone.` }))) return;
  let ok = 0;
  const failed = [];
  bulkProgress.value = { active: true, label: 'Delete', completed: 0, total };
  for (const inv of selectedInvoices.value) {
    try {
      await api.del(`/api/invoices/${inv.id}`);
      ok += 1;
    } catch (e) {
      failed.push({ id: inv.id, number: inv.invoice_number, err: String(e?.message || e) });
    }
    bulkProgress.value.completed += 1;
  }
  bulkProgress.value = { active: false, label: '', completed: 0, total: 0 };
  if (failed.length === 0) {
    toast.add({ severity: "success", summary: "Deleted", detail: `${ok} invoice(s) deleted`, life: 3000 });
  } else {
    toast.add({
      severity: ok > 0 ? "warn" : "error",
      summary: ok > 0 ? "Delete — partial failure" : "Delete failed",
      detail: `${ok}/${total} deleted. Failed: ${failed.map((f) => f.number || f.id).join(", ")}`,
      life: 6000,
    });
  }
  selectedInvoices.value = [];
  await loadData();
}

async function copyPayLink(invoice) {
  // Replaces payInvoiceOnline, which POSTed a payments-intent stub that
  // never returned a checkout URL, so the old "Pay $X" button was a dead
  // end. The office's real move is handing the customer a link.
  if (!invoice?.id || payingInvoiceId.value) return;
  payingInvoiceId.value = invoice.id;
  try {
    const result = await api.post(`/api/invoices/${invoice.id}/pay-link`, {});
    if (result?.url) {
      let copied = false;
      try {
        await navigator.clipboard.writeText(result.url);
        copied = true;
      } catch (_) { /* clipboard denied — still show the link */ }
      toast.add({
        severity: 'success',
        summary: copied ? 'Pay link copied' : 'Pay link ready',
        detail: copied ? 'Paste it into a text or email to the customer.' : result.url,
        life: 6000,
      });
    } else {
      toast.add({
        severity: 'warn',
        summary: 'Online payments not configured',
        detail: result?.stripe_configured
          ? 'GDX_PUBLIC_BASE_URL is not set on the server.'
          : 'Add Stripe API keys to enable customer pay links.',
        life: 6000,
      });
    }
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Pay link failed',
      detail: err?.message || 'Could not create the pay link',
      life: 4000,
    });
  } finally {
    payingInvoiceId.value = null;
  }
}

const showPaymentDialog = ref(false);
const paymentTarget = ref(null);

const statusTabs = [
  { label: "All", value: "All" },
  { label: "Draft", value: "Draft" },
  { label: "Sent", value: "Sent" },
  { label: "Paid", value: "Paid" },
  { label: "Overdue", value: "Overdue" },
];

// Persist status tab + search + date preset across reloads. Validators guard
// against stale storage: a status no longer in statusTabs (future rename) or a
// removed date preset falls back to its default instead of silently filtering
// the list to empty. "custom" is intentionally excluded from the valid preset
// set — customRange holds Date objects we don't persist, so a restored
// "custom" would show the picker with an empty range (no filter); fall back to
// "all" instead. Placed after statusTabs so the validator can reference it.
const BILLING_STATUS_KEYS = statusTabs.map((t) => t.value);
const BILLING_DATE_PRESET_KEYS = datePresetOptions
  .map((o) => o.value)
  .filter((v) => v !== "custom");
useListPrefs(
  "billing",
  { activeStatus, searchQuery, datePreset },
  {
    activeStatus: { default: "All", valid: (v) => BILLING_STATUS_KEYS.includes(v) },
    searchQuery: { default: "", valid: (v) => typeof v === "string" },
    datePreset: { default: "all", valid: (v) => BILLING_DATE_PRESET_KEYS.includes(v) },
  },
);

const paymentMethods = ["Cash", "Check", "Card", "Zelle", "Venmo", "ACH", "Other"];

const newPayment = ref({ amount: 0, method: "Cash", reference: "" });

const filteredInvoices = computed(() => {
  let list = invoices.value;
  if (activeStatus.value !== "All") {
    list = list.filter((inv) => inv.status === activeStatus.value);
  }
  if (searchQuery.value) {
    const q = searchQuery.value.toLowerCase();
    list = list.filter((inv) => {
      const hay = [inv.invoice_number, inv.customer_name, inv.notes].join(" ").toLowerCase();
      return hay.includes(q);
    });
  }
  const [start, end] = dateRange.value;
  if (start || end) {
    list = list.filter((inv) => {
      // Prefer invoice_date (issue date), fall back to created_at
      const raw = inv.invoice_date || inv.created_at;
      if (!raw) return false;
      const t = new Date(raw).getTime();
      if (start && t < start.getTime()) return false;
      if (end && t > end.getTime()) return false;
      return true;
    });
  }
  return list;
});

const totalPages = computed(() => Math.max(1, Math.ceil(filteredInvoices.value.length / perPage)));

const sortedInvoices = computed(() => {
  if (!sortField.value) return filteredInvoices.value;
  const field = sortField.value;
  const dir = sortOrder.value || 1;
  // Copy before sorting — never mutate the source array (would
  // invalidate the upstream computed and cause loop).
  return [...filteredInvoices.value].sort((a, b) => {
    const av = a?.[field];
    const bv = b?.[field];
    // Null/undefined values sort to the end regardless of direction
    // (matches PrimeVue's default `nullSortOrder: 1` behavior).
    const an = av == null || av === "";
    const bn = bv == null || bv === "";
    if (an && bn) return 0;
    if (an) return 1;
    if (bn) return -1;
    // Numeric compare when both sides are numbers; otherwise locale string
    if (typeof av === "number" && typeof bv === "number") {
      return (av - bv) * dir;
    }
    return String(av).localeCompare(String(bv), undefined, { numeric: true, sensitivity: "base" }) * dir;
  });
});

const paginatedInvoices = computed(() => {
  const start = (currentPage.value - 1) * perPage;
  return sortedInvoices.value.slice(start, start + perPage);
});

// CSV export — dumps the CURRENTLY FILTERED rows (status tab + search +
// date range applied, in the table's sort order, all pages), matching the
// visible columns. Sibling to bulkExport, which exports only the SELECTED
// rows from the bulk-actions bar.
const { exportCsv } = useTableExport();
function exportInvoices() {
  exportCsv(
    sortedInvoices.value,
    [
      { field: "invoice_number", header: "Invoice #" },
      { field: "customer_name", header: "Customer" },
      { field: "total", header: "Amount" },
      { field: "status", header: "Status" },
      { field: "due_date", header: "Due Date" },
    ],
    "invoices",
  );
}

// Selection helpers for the custom select column (replaces PrimeVue
// selectionMode="multiple" which renders a header checkbox without id/name).
const isSelected = (row) => selectedInvoices.value.some((r) => r.id === row.id);
const toggleSelection = (row) => {
  if (isSelected(row)) {
    selectedInvoices.value = selectedInvoices.value.filter((r) => r.id !== row.id);
  } else {
    selectedInvoices.value = [...selectedInvoices.value, row];
  }
};
const isAllSelected = computed(() =>
  paginatedInvoices.value.length > 0 &&
  paginatedInvoices.value.every((r) => isSelected(r))
);
const isSomeSelected = computed(() => {
  const some = paginatedInvoices.value.some((r) => isSelected(r));
  return some && !isAllSelected.value;
});
const toggleSelectAll = () => {
  if (isAllSelected.value) {
    // Deselect everything currently visible on this page
    const visibleIds = new Set(paginatedInvoices.value.map((r) => r.id));
    selectedInvoices.value = selectedInvoices.value.filter((r) => !visibleIds.has(r.id));
  } else {
    // Add any currently-visible rows that aren't already selected
    const existingIds = new Set(selectedInvoices.value.map((r) => r.id));
    const toAdd = paginatedInvoices.value.filter((r) => !existingIds.has(r.id));
    selectedInvoices.value = [...selectedInvoices.value, ...toAdd];
  }
};

// Server-side KPIs (S113 — D-S111-billing-summary-404). Falls back to
// client-side computation if /api/invoices/summary is unavailable so the
// view still renders something during transient backend errors.
const billingSummary = ref(null);

async function loadBillingSummary() {
  try {
    billingSummary.value = await api.get('/api/invoices/summary');
  } catch (_) {
    billingSummary.value = null;
  }
}

// Outstanding = receivables only. Prefer server-side aggregator (full-table
// SUM, no pagination cap) and fall back to client-side over the loaded
// list. Drafts excluded — they aren't yet receivables.
const totalOutstanding = computed(() => {
  if (billingSummary.value && typeof billingSummary.value.total_outstanding === 'number') {
    return billingSummary.value.total_outstanding;
  }
  return invoices.value
    .filter((inv) => inv.status !== "Paid" && inv.status !== "Draft")
    .reduce((sum, inv) => sum + toNum(inv.balance_due ?? inv.total), 0);
});

const overdueAmount = computed(() => {
  if (billingSummary.value && typeof billingSummary.value.overdue === 'number') {
    return billingSummary.value.overdue;
  }
  return invoices.value
    .filter((inv) => inv.status === "Overdue")
    .reduce((sum, inv) => sum + toNum(inv.balance_due ?? inv.total), 0);
});

const paidThisMonth = computed(() => {
  if (billingSummary.value && typeof billingSummary.value.paid_this_month === 'number') {
    return billingSummary.value.paid_this_month;
  }
  const now = new Date();
  const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).toISOString().slice(0, 10);
  return invoices.value
    .filter((inv) => inv.status === "Paid" && (inv.paid_at || inv.updated_at || "") >= monthStart)
    .reduce((sum, inv) => sum + toNum(inv.total), 0);
});

// Unsent drafts (PR1-billing-capture). Server pair preferred; client-side
// fallback mirrors the other KPI computeds.
const draftCount = computed(() => {
  if (billingSummary.value && typeof billingSummary.value.draft_count === 'number') {
    return billingSummary.value.draft_count;
  }
  return invoices.value.filter((inv) => inv.status === "Draft").length;
});

const draftTotal = computed(() => {
  if (billingSummary.value && typeof billingSummary.value.draft_total === 'number') {
    return billingSummary.value.draft_total;
  }
  return invoices.value
    .filter((inv) => inv.status === "Draft")
    .reduce((sum, inv) => sum + toNum(inv.total), 0);
});

// --- Helpers ---
function capitalize(s) {
  if (!s) return "";
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}

function toNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function statusSeverity(status) {
  const map = { Draft: "secondary", Sent: "info", Paid: "success", Overdue: "danger", Partial: "warn" };
  return map[status] || "secondary";
}

function tabCount(status) {
  if (status === "All") return invoices.value.length;
  return invoices.value.filter((inv) => inv.status === status).length;
}

function normalizeInvoice(raw, customerMap = {}) {
  return {
    id: raw.id,
    invoice_number: raw.invoice_number || raw.invoiceNumber || `INV-${String(raw.id).substring(0, 8)}`,
    customer_name: raw.customer_name || raw.customer || customerMap[String(raw.customer_id)] || "Unknown",
    customer_id: raw.customer_id,
    job_id: raw.job_id,
    total: toNum(raw.total || raw.amount || raw.total_amount || 0),
    balance_due: toNum(raw.balance_due ?? raw.total ?? raw.amount ?? 0),
    status: capitalize(raw.effective_status || raw.status) || "Draft",
    due_date: raw.due_date || raw.dueDate || "",
    paid_at: raw.paid_at || "",
    updated_at: raw.updated_at || "",
    notes: raw.notes || "",
  };
}

// --- Actions ---
async function loadData() {
  loading.value = true;
  try {
    const [invRes, custRes, jobRes] = await Promise.allSettled([
      api.get("/api/invoices", { suppressErrorToast: true }),
      api.get("/api/customers?per_page=500", { suppressErrorToast: true }),
      api.get("/api/jobs", { suppressErrorToast: true }),
    ]);

    const customerMap = {};
    if (custRes.status === "fulfilled") {
      const raw = custRes.value;
      const list = Array.isArray(raw) ? raw : raw?.items || raw?.data || [];
      customers.value = list;
      for (const c of list) customerMap[String(c.id)] = c.name;
    }

    if (jobRes.status === "fulfilled") {
      const raw = jobRes.value;
      jobs.value = Array.isArray(raw) ? raw : raw?.items || raw?.data || [];
    }

    if (invRes.status === "fulfilled") {
      const raw = invRes.value;
      const list = Array.isArray(raw) ? raw : raw?.items || raw?.data || [];
      invoices.value = list.map((inv) => normalizeInvoice(inv, customerMap));
    }
    // If the invoice fetch itself failed, surface it instead of silently
    // showing an empty list (otherwise the page renders "No invoices yet"
    // even when there are 305 invoices in the DB).
    if (invRes.status === "rejected") {
      const status = invRes.reason?.status || invRes.reason?.response?.status;
      const msg = status === 401 || status === 403
        ? "Your session may have expired — refresh the page to retry."
        : "Failed to load invoices. Refresh to retry.";
      toast.add({ severity: "error", summary: "Couldn't load invoices", detail: msg, life: 4000 });
    }
    // Load ready-for-billing jobs
    try {
      const rfb = await api.get("/api/jobs/ready-for-billing");
      readyJobs.value = Array.isArray(rfb) ? rfb : [];
    } catch (e) {
      console.warn("ready_for_billing_failed", e);
      readyJobs.value = [];
    }
    // PR4 — parts consumed on completed jobs that never reached an invoice.
    try {
      const leaked = await api.get("/api/parts-needed/unbilled-consumed");
      leakedParts.value = Array.isArray(leaked) ? leaked : [];
    } catch (e) {
      console.warn("unbilled_consumed_parts_failed", e);
      leakedParts.value = [];
    }
  } catch (e) {
    console.error("billing_loadData_failed", e);
    toast.add({ severity: "error", summary: "Error", detail: "Failed to load billing data", life: 3000 });
  } finally {
    loading.value = false;
  }
}

async function dismissLeakedParts(entry) {
  // PR4 — office decision: these consumed parts will never be billed
  // (warranty/goodwill/flat-priced). Marks each part wont_bill; the rows
  // keep their audit trail but leave the billing surfaces.
  try {
    for (const part of entry.parts || []) {
      await api.patch(`/api/parts-needed/${part.id}/status`, { status: 'wont_bill' });
    }
    leakedParts.value = leakedParts.value.filter((e) => e.job_id !== entry.job_id);
    toast.add({ severity: 'info', summary: "Won't bill", detail: `${(entry.parts || []).length} part(s) dismissed`, life: 3000 });
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Error', detail: e.message || 'Failed to dismiss parts', life: 4000 });
  }
}

function createInvoiceForJob(job) {
  // S122 — route to the dedicated /billing/new page. InvoiceCreateView
  // hydrates customer + job + estimate lines + parts-from-job picker on its
  // own. Replaces the in-place dialog that lived here pre-S122.
  if (!job?.id) return;
  router.push({
    path: '/billing/new',
    query: { job_id: job.id, customer_id: job.customer_id || '' },
  });
}

function reviewJob(job) {
  // Review path — open the job-detail page so the office can verify parts,
  // labor, and notes before invoicing. Doug 2026-05-10: completed jobs
  // shouldn't be one-click invoiced; they need a double-check + a chance
  // to add parts that the tech may have forgotten.
  if (job?.id) router.push(`/jobs/${job.id}`);
}

function openPaymentDialog(inv) {
  paymentTarget.value = inv;
  newPayment.value = { amount: 0, method: "Cash", reference: "" };
  showPaymentDialog.value = true;
}

async function recordPayment() {
  if (!paymentTarget.value) return;
  recordingPayment.value = true;
  try {
    await api.post(`/api/invoices/${paymentTarget.value.id}/payments`, {
      amount: newPayment.value.amount,
      method: newPayment.value.method,
      reference: newPayment.value.reference,
      // The dialog has no date picker; the API requires `date` (422
      // without it). Same today-default as InvoiceDetail/MobileInvoice.
      date: new Date().toISOString().slice(0, 10),
    });
    showPaymentDialog.value = false;
    toast.add({ severity: "success", summary: "Recorded", detail: "Payment recorded", life: 3000 });
    await loadData();
  } catch (err) {
    toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to record payment", life: 3000 });
  } finally {
    recordingPayment.value = false;
  }
}

function sendInvoice(inv) {
  // 2026-07-20 — no more fire-and-forget POST /send from the list row. Sending
  // now goes through the invoice detail composer (?compose=1 auto-opens it):
  // the operator sees the recipient, the message, and a preview of the actual
  // PDF before anything leaves the building. Status flips via mark-sent on a
  // real send, not optimistically here.
  if (inv?.id) router.push(`/billing/${inv.id}?compose=1`);
}

async function downloadPdf(inv) {
  try {
    await openAuthedFile(`/api/invoices/${inv.id}/pdf`);
  } catch (e) {
    console.error("invoice_pdf_failed", inv?.id, e);
    toast.add({
      severity: "error",
      summary: "PDF failed",
      detail: e?.message || "Could not open invoice PDF",
      life: 5000,
    });
  }
}

function editInvoice(inv) {
  // S122 — edits happen on the invoice detail page (line-level add/remove,
  // tax, due-date PATCH). The list-page edit dialog is gone.
  if (inv?.id) router.push(`/billing/${inv.id}`);
}

function confirmDelete(inv) {
  confirmDestructive({
    message: `Delete invoice ${inv.invoice_number}? This cannot be undone.`,
    header: "Confirm Delete",
    acceptLabel: "Delete",
    accept: () => deleteInvoice(inv),
  });
}

async function deleteInvoice(inv) {
  try {
    await api.del(`/api/invoices/${inv.id}`);
    invoices.value = invoices.value.filter((i) => i.id !== inv.id);
    toast.add({ severity: "success", summary: "Deleted", detail: "Invoice deleted", life: 3000 });
  } catch (err) {
    toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to delete", life: 3000 });
  }
}

function onRowClick(event) {
  const id = event?.data?.id;
  if (id) router.push(`/billing/${id}`);
}

// Expose for InvoiceDetail to use
defineExpose({ openPaymentDialog, sendInvoice });

onMounted(async () => {
  loadBillingSummary();
  await loadData();
  // ?status=Overdue|Paid|Sent|Draft on mount sets the active tab — used by
  // the Dashboard "X overdue invoices need collection" alert so the user
  // lands directly on the filtered list instead of the All view, where
  // overdue rows scatter among 300 invoices sorted by created_at desc.
  const qs = String(route.query.status || "");
  if (qs && statusTabs.some((t) => t.value === qs)) {
    activeStatus.value = qs;
  }
  // S122 — legacy ?action=create&customer_id=…&job_id=… deep links now route
  // to the dedicated /billing/new page; preserves the Job-Detail "Create
  // Invoice" link contract.
  if (route.query.action === "create" && route.query.customer_id) {
    router.replace({
      path: '/billing/new',
      query: {
        customer_id: route.query.customer_id,
        job_id: route.query.job_id || undefined,
      },
    });
  }
});
</script>

<style scoped>
.summary-cards {
  display: grid;
  grid-template-columns: repeat(3, minmax(180px, 1fr));
  gap: 0.75rem;
  margin-bottom: 1.25rem;
}

.stat-value {
  font-size: 1.5rem;
  font-weight: 700;
  margin: 0;
}
.stat-value.outstanding { color: var(--p-blue-500, #3b82f6); }
.stat-value.overdue { color: var(--p-red-500, #ef4444); }
.stat-value.paid { color: var(--p-green-500, #22c55e); }
.stat-value.drafts { color: var(--p-amber-500, #f59e0b); }
.draft-card { cursor: pointer; }
.draft-total {
  font-size: 0.9rem;
  color: var(--p-text-muted-color, #6b7280);
  font-weight: 400;
}

.billing-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}

.search-wrap {
  flex: 1;
  max-width: 360px;
}
.search-wrap .p-inputtext {
  width: 100%;
}

.date-preset-select {
  min-width: 160px;
}
.custom-range-picker {
  min-width: 220px;
}
.date-range-summary {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
  background: var(--p-content-hover-background);
  padding: 0.25rem 0.5rem;
  border-radius: 4px;
}

.status-tabs {
  display: flex;
  gap: 0.5rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}

.clickable-table :deep(tr) {
  cursor: pointer;
}

.bulk-actions-bar {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.75rem 1rem;
  background: var(--p-primary-100, rgba(14, 165, 233, 0.1));
  border: 1px solid var(--p-primary-color);
  border-radius: 8px;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}
.bulk-count {
  font-weight: 600;
  color: var(--p-primary-color);
  margin-right: 0.5rem;
}

.bulk-progress-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.5rem 1rem;
  background: var(--p-surface-100, #f1f5f9);
  border-radius: 8px;
  margin-bottom: 1rem;
}
.bulk-progress-label {
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
  white-space: nowrap;
}

.link-text {
  color: var(--p-primary-color, #3b82f6);
  font-weight: 600;
}

.pagination-bar {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.75rem;
  margin-top: 1rem;
}

.page-info {
  font-size: 0.875rem;
  color: var(--p-text-muted-color, #6b7280);
}

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.form-field.full-width {
  grid-column: 1 / -1;
}
.form-field label {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--p-text-muted-color, #6b7280);
  text-transform: uppercase;
}
.form-field :deep(.p-dropdown),
.form-field :deep(.p-calendar),
.form-field :deep(.p-inputtext),
.form-field :deep(.p-inputnumber),
.form-field :deep(.p-textarea) {
  width: 100%;
}

.line-items-editor {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.line-item-header,
.line-item-row {
  display: grid;
  grid-template-columns: 2fr 0.7fr 1fr 1fr 2rem;
  gap: 0.5rem;
  align-items: center;
}
.line-item-header {
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--p-text-muted-color);
  padding: 0 0.25rem;
}
.col-desc, .col-qty, .col-price, .col-total, .col-action {
  width: 100%;
}
.line-total-display {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.line-item-buttons {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.25rem;
}
.line-items-subtotal {
  margin-top: 0.5rem;
  text-align: right;
  font-size: 0.95rem;
  color: var(--p-text-color);
}

@media (max-width: 900px) {
  .summary-cards {
    grid-template-columns: 1fr;
  }
  .form-grid {
    grid-template-columns: 1fr;
  }
  .billing-toolbar {
    flex-direction: column;
    align-items: stretch;
  }
  .search-wrap {
    max-width: 100%;
  }
}
</style>
