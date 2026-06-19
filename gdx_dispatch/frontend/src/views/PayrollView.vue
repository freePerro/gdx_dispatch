<template>
    <section class="payroll-view view-card">
      <Toolbar data-testid="payroll-toolbar">
        <template #start>
          <h2 class="page-title">Payroll</h2>
        </template>
        <template #end>
          <Button
            label="Run payroll for current period"
            icon="pi pi-play"
            :loading="runningPayroll"
            data-testid="run-payroll-btn"
            @click="runPayroll"
          />
        </template>
      </Toolbar>

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
              <template #body="{ data }">${{ Number(data.total_gross || 0).toFixed(2) }}</template>
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
            <Column field="employee" header="Employee" />
            <Column field="hours" header="Hours">
              <template #body="{ data }">{{ data.hours ?? '—' }}</template>
            </Column>
            <Column field="gross" header="Gross">
              <template #body="{ data }">${{ Number(data.gross || 0).toFixed(2) }}</template>
            </Column>
            <Column field="net" header="Net">
              <template #body="{ data }">${{ Number(data.net || 0).toFixed(2) }}</template>
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
import { onMounted, ref } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
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
const runningPayroll = ref(false);
const activeTab = ref('periods');
const detailModal = ref(false);
const detailRecord = ref(null);
const detailType = ref('');

function statusSeverity(status) {
  if (!status) return 'info';
  const normalized = status.toLowerCase();
  if (['paid', 'finalized', 'completed'].includes(normalized)) return 'success';
  if (['failed', 'rejected', 'denied'].includes(normalized)) return 'danger';
  if (['pending', 'processing', 'running'].includes(normalized)) return 'warning';
  return 'info';
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

async function loadPayPeriods() {
  loadingPeriods.value = true;
  try {
    const data = await api.get('/api/payroll/pay-periods');
    payPeriods.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loadingPeriods.value = false;
  }
}

async function loadPayStubs() {
  loadingStubs.value = true;
  try {
    const data = await api.get('/api/payroll/pay-stubs');
    payStubs.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loadingStubs.value = false;
  }
}

async function loadPayrollData() {
  await Promise.all([loadPayPeriods(), loadPayStubs()]);
}

async function runPayroll() {
  runningPayroll.value = true;
  try {
    await api.post('/api/payroll/run-current-period', null, { successMessage: 'Payroll queued' });
    await loadPayrollData();
  } finally {
    runningPayroll.value = false;
  }
}

function openDetail(record, type) {
  detailRecord.value = record;
  detailType.value = type;
  detailModal.value = true;
}

onMounted(loadPayrollData);
</script>
