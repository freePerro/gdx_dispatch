<template>
    <section class="payroll-view view-card">
      <Toolbar data-testid="payroll-toolbar">
        <template #start>
          <!-- "Pay Runs", matching its tab. Inside the Payroll hub a heading
               that just said "Payroll" named the whole section rather than
               this screen, which is the one part of it that does not work. -->
          <h2 class="page-title">Pay Runs</h2>
        </template>
        <!-- The "Run payroll for current period" button is GONE, not
             disabled. POST /api/payroll/run-current-period is a ui_compat 501
             stub, so the button could only ever fail. Disabling it was tried
             first and read worse: the theme renders a disabled primary button
             almost identically to an enabled one, so it invited a click that
             silently did nothing — less honest than the error it replaced.
             The notice below carries the message instead. -->
      </Toolbar>

      <div class="payroll-notice" data-testid="payroll-not-built-notice">
        <i class="pi pi-info-circle" aria-hidden="true" />
        <div>
          <p class="notice-lede"><strong>Pay runs are not built.</strong></p>
          <p class="notice-body">
            This screen cannot calculate a pay run or produce pay stubs — the
            endpoints behind its tables return “not implemented”, and nothing
            is stored here.
          </p>
          <!-- Half of what this notice used to claim stopped being true in
               v1.105.0: pay periods DO exist now, and the office can export
               and send a period's hours. Leaving the old wording would have
               left this page contradicting the tab next to it. -->
          <p class="notice-body">
            <strong>Pay periods and hours are a different screen, and they do
            work.</strong>
            <router-link to="/timesheets" data-testid="payroll-timesheets-link">
              Timesheets
            </router-link>
            groups the crew's hours into the pay period set in Settings,
            exports them as a PDF or spreadsheet, and sends them to payroll.
          </p>
        </div>
      </div>

      <Tabs v-model:value="activeTab" class="payroll-tabview">
        <TabList>
          <Tab value="periods" data-testid="periods-tab">Pay Periods</Tab>
          <Tab value="stubs" data-testid="stubs-tab">Pay Stubs</Tab>
        </TabList>
        <TabPanels>
        <TabPanel value="periods">
          <div v-if="loadingPeriods" class="spinner-wrap"><ProgressSpinner /></div>
          <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
            v-else
            :value="payPeriods"
            paginator
            :rows="10"
            striped-rows
            
            data-testid="pay-periods-table"
            @row-click="($event) => openDetail($event.data, 'period')"
          >
            <template #empty>
              <EmptyState
                icon="pi pi-calendar"
                title="Pay periods are not built"
                message="Nothing creates them yet. Hours live in the timeclock."
              />
            </template>
            <Column field="start" header="Start">
              <template #body="{ data }">{{ formatDate(data.start) }}</template>
            </Column>
            <Column field="end" header="End">
              <template #body="{ data }">{{ formatDate(data.end) }}</template>
            </Column>
            <Column field="status" header="Status" style="width:140px">
              <template #body="{ data }">
                <Tag :value="statusLabel(data.status)" :severity="statusSeverity(data.status)" />
              </template>
            </Column>
            <Column field="total_hours" header="Total Hours">
              <template #body="{ data }">{{ data.total_hours ?? '—' }}</template>
            </Column>
            <Column field="total_gross" header="Total Gross">
              <template #body="{ data }">{{ formatCurrency(data.total_gross || 0) }}</template>
            </Column>
          </DataTable>
        </TabPanel>
        <TabPanel value="stubs">
          <div v-if="loadingStubs" class="spinner-wrap"><ProgressSpinner /></div>
          <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
            v-else
            :value="payStubs"
            paginator
            :rows="12"
            striped-rows
            
            data-testid="pay-stubs-table"
            @row-click="($event) => openDetail($event.data, 'stub')"
          >
            <template #empty>
              <EmptyState
                icon="pi pi-file"
                title="No pay stubs yet"
                message="Pay stubs appear here after payroll runs."
              />
            </template>
            <Column field="employee" header="Employee" />
            <Column field="hours" header="Hours">
              <template #body="{ data }">{{ data.hours ?? '—' }}</template>
            </Column>
            <Column field="gross" header="Gross">
              <template #body="{ data }">{{ formatCurrency(data.gross || 0) }}</template>
            </Column>
            <Column field="net" header="Net">
              <template #body="{ data }">{{ formatCurrency(data.net || 0) }}</template>
            </Column>
            <Column field="period" header="Period" />
            <Column field="status" header="Status" style="width:120px">
              <template #body="{ data }">
                <Tag :value="statusLabel(data.status)" :severity="statusSeverity(data.status)" />
              </template>
            </Column>
          </DataTable>
        </TabPanel>
        </TabPanels>
      </Tabs>

      <Dialog v-model:visible="detailModal" modal header="Details" :style="{ width: '520px' }" data-testid="payroll-detail-dialog">
        <div v-if="detailRecord">
          <p><strong>Type:</strong> {{ detailType === 'period' ? 'Pay Period' : 'Pay Stub' }}</p>
          <div v-for="(value, key) in detailRecord" :key="key" class="detail-row">
            <label>{{ key.replace('_', ' ') }}</label>
            <p>{{ formatDetail(value) }}</p>
          </div>
        </div>
        <template #footer>
          <Button label="Close" severity="secondary" @click="detailModal = false" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { payrollRunSeverity } from '../utils/statusSeverity';
import { onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatMoney as formatCurrency } from '../composables/useFormatters';
import EmptyState from '../components/EmptyState.vue';
import Toolbar from 'primevue/toolbar';
import Button from 'primevue/button';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import Dialog from 'primevue/dialog';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';
import ProgressSpinner from 'primevue/progressspinner';
import Tag from 'primevue/tag';

const api = useApiWithToast();

const payPeriods = ref([]);
const payStubs = ref([]);
const loadingPeriods = ref(false);
const loadingStubs = ref(false);
const activeTab = ref('periods');
const detailModal = ref(false);
const detailRecord = ref(null);
const detailType = ref('');

function statusSeverity(status) {
  return payrollRunSeverity(status);
}

function statusLabel(status) {
  return status ? status.replace('_', ' ') : 'Unknown';
}

function formatDate(value) {
  if (!value) return '—';
  try {
    return value.split('T')[0];
  } catch {
    return value;
  }
}

function formatDetail(value) {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') return JSON.stringify(value, null, 2);
  return value;
}

// `/api/payroll/pay-periods`, `/pay-stubs` and `/run-current-period` are all
// ui_compat 501 stubs — none of them is implemented. NOTE (2026-08-27): the
// tenant's real pay-period calendar is NOT this dead endpoint — it lives on
// AppSettings and is served by GET /api/timeclock/pay-periods. Do not wire
// this screen to the stub on the strength of its name. Calling them produced a
// failed request and an error toast on every visit, while the empty states
// told the operator to press the button that caused it. The screen now says
// so instead of asking the server three times to confirm it.
//
// Deliberately NOT deleted: the tables and the detail modal are the shape this
// screen will take when payroll runs exist, and the decision list
// (`unimplemented-endpoints-decision-list`) has not called build-or-remove on
// them yet.

function openDetail(record, type) {
  detailRecord.value = record;
  detailType.value = type;
  detailModal.value = true;
}

</script>

<style scoped>
/* Theme tokens, not a fixed palette — this panel has to read in dark mode,
   where a hardcoded pale background would put near-white text on it. */
.payroll-notice {
  display: flex;
  gap: 0.75rem;
  align-items: flex-start;
  margin: 0.75rem 0 1rem;
  padding: 0.85rem 1rem;
  border: 1px solid var(--p-content-border-color);
  border-left: 4px solid var(--p-primary-color);
  border-radius: var(--p-content-border-radius, 6px);
  background: var(--p-content-background);
  color: var(--p-text-color);
}
.payroll-notice .pi {
  margin-top: 0.15rem;
  color: var(--p-primary-color);
}
.notice-lede { margin: 0 0 0.2rem; }
.notice-body { margin: 0; color: var(--p-text-muted-color); font-size: 0.9rem; }
</style>
