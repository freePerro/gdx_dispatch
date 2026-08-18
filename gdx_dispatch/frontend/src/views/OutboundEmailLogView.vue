<template>
  <section class="outbound-email-log">
    <div class="page-header">
      <div>
        <h1>Outbound Email Log</h1>
        <p class="muted">
          Every email the system attempted to send — who triggered it, who it
          went to, what happened. Click a row to see the exact message.
        </p>
      </div>
    </div>

    <div class="filters">
      <Select v-model="filters.status" :options="statusOptions" option-label="label"
        option-value="value" placeholder="Any outcome" show-clear class="filter"
        data-testid="oel-filter-status" @change="reload" />
      <Select v-model="filters.kind" :options="kindOptions" option-label="label"
        option-value="value" placeholder="Any type" show-clear class="filter"
        data-testid="oel-filter-kind" @change="reload" />
      <Select v-model="filters.initiator_kind" :options="initiatorOptions" option-label="label"
        option-value="value" placeholder="Any source" show-clear class="filter"
        data-testid="oel-filter-initiator" @change="reload" />
      <InputText v-model="filters.to_email" placeholder="Recipient contains…"
        class="filter" data-testid="oel-filter-to" @keyup.enter="reload" />
      <Button label="Search" icon="pi pi-search" outlined @click="reload" />
    </div>

    <DataTable :value="items" :loading="loading" data-key="id" striped-rows
      selection-mode="single" data-testid="oel-table" @row-click="openDetail">
      <Column field="created_at" header="When">
        <template #body="{ data }">{{ formatDateTime(data.created_at) }}</template>
      </Column>
      <Column field="to_email" header="To">
        <template #body="{ data }">
          <span>{{ data.to_name || data.to_email }}</span>
          <small v-if="data.to_name" class="muted block">{{ data.to_email }}</small>
        </template>
      </Column>
      <Column field="subject" header="Subject" />
      <Column field="kind" header="Type">
        <template #body="{ data }"><Tag :value="data.kind || '—'" severity="secondary" /></template>
      </Column>
      <Column field="initiator_kind" header="Sent by">
        <template #body="{ data }">
          <span>{{ initiatorLabel(data) }}</span>
        </template>
      </Column>
      <Column field="status" header="Outcome">
        <template #body="{ data }">
          <Tag v-if="data.bounced_at" value="bounced" severity="danger" />
          <Tag v-else-if="data.status === 'sent'" value="sent" severity="success" />
          <Tag v-else value="failed" severity="danger" />
          <small v-if="data.skip_reason" class="muted block">{{ data.skip_reason }}</small>
        </template>
      </Column>
      <template #empty>
        <div class="empty">No outbound emails match — sends will appear here the moment anything is attempted.</div>
      </template>
    </DataTable>

    <div class="pager">
      <Button label="Previous" text :disabled="offset === 0 || loading" @click="page(-1)" />
      <Button label="Next" text :disabled="!hasMore || loading" @click="page(1)" />
    </div>

    <Dialog v-model:visible="showDetail" header="Outbound email" modal
      :style="{ width: '760px' }" data-testid="oel-detail">
      <div v-if="detail" class="detail">
        <div class="detail-grid">
          <div><label>To</label><span>{{ detail.to_name || '—' }} &lt;{{ detail.to_email }}&gt;</span></div>
          <div><label>Subject</label><span>{{ detail.subject }}</span></div>
          <div><label>When</label><span>{{ formatDateTime(detail.created_at) }}</span></div>
          <div><label>Outcome</label>
            <span>
              {{ detail.status }}<template v-if="detail.skip_reason"> — {{ detail.skip_reason }}</template>
              <template v-if="detail.bounced_at"> · bounced {{ formatDateTime(detail.bounced_at) }}</template>
            </span>
          </div>
          <div><label>Provider</label><span>{{ detail.provider || '—' }}</span></div>
          <div><label>Sent by</label><span>{{ initiatorLabel(detail) }}</span></div>
          <div><label>About</label><span>{{ detail.entity_type || '—' }} {{ detail.entity_id || '' }}</span></div>
          <div><label>Recipient chosen via</label><span>{{ detail.recipient_source || '—' }}</span></div>
          <div v-if="detail.attachments?.length"><label>Attachments</label>
            <span>{{ detail.attachments.map(a => a.name).join(', ') }}</span>
          </div>
        </div>
        <label class="body-label">Message as delivered</label>
        <iframe class="body-frame" sandbox="" :srcdoc="detail.body_html"
          title="Email body" data-testid="oel-body" />
      </div>
    </Dialog>
    <Toast />
  </section>
</template>

<script setup>
import { onMounted, ref } from "vue";
import { useToast } from "primevue/usetoast";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import Select from "primevue/select";
import Tag from "primevue/tag";
import Toast from "primevue/toast";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatDateTime } from "../composables/useFormatters";

const api = useApiWithToast();
const toast = useToast();

const items = ref([]);
const loading = ref(false);
const offset = ref(0);
const hasMore = ref(false);
const limit = 50;
const filters = ref({ status: null, kind: null, initiator_kind: null, to_email: "" });
const showDetail = ref(false);
const detail = ref(null);

const statusOptions = [
  { label: "Sent", value: "sent" },
  { label: "Failed", value: "failed" },
];
const kindOptions = [
  { label: "Documents (invoice/estimate)", value: "document" },
  { label: "Receipts", value: "receipt" },
  { label: "Reminders", value: "reminder" },
  { label: "Portal links", value: "magic_link" },
  { label: "Automations", value: "automation" },
  { label: "Plugins", value: "plugin" },
];
const initiatorOptions = [
  { label: "A person", value: "user" },
  { label: "Reminder task", value: "reminder_task" },
  { label: "Workflow rule", value: "workflow_rule" },
  { label: "Plugin", value: "plugin" },
];

function initiatorLabel(row) {
  const kind = row.initiator_kind || "user";
  if (kind === "user") return "Staff";
  if (kind === "workflow_rule") return `Rule ${row.initiator_ref || ""}`.trim();
  if (kind === "plugin") return `Plugin: ${row.initiator_ref || "?"}`;
  if (kind === "reminder_task") return "Reminder schedule";
  return kind;
}

async function fetchPage() {
  loading.value = true;
  try {
    const params = { limit, offset: offset.value };
    for (const [k, v] of Object.entries(filters.value)) {
      if (v) params[k] = v;
    }
    const data = await api.get("/api/outbound-emails", { params });
    const payload = data?.data || data;
    items.value = payload.items || [];
    hasMore.value = Boolean(payload.has_more);
  } catch (err) {
    toast.add({ severity: "error", summary: "Load failed", detail: err?.message || "", life: 4000 });
  } finally {
    loading.value = false;
  }
}

function reload() {
  offset.value = 0;
  fetchPage();
}

function page(dir) {
  offset.value = Math.max(0, offset.value + dir * limit);
  fetchPage();
}

async function openDetail(e) {
  const row = e?.data;
  if (!row?.id) return;
  try {
    const data = await api.get(`/api/outbound-emails/${row.id}`);
    detail.value = data?.data || data;
    showDetail.value = true;
  } catch (err) {
    toast.add({ severity: "error", summary: "Load failed", detail: err?.message || "", life: 4000 });
  }
}

onMounted(fetchPage);
</script>

<style scoped>
.outbound-email-log { padding: 1rem 1.25rem; }
.page-header { margin-bottom: 1rem; }
.page-header h1 { margin: 0 0 0.25rem; font-size: 1.35rem; }
.muted { color: var(--p-text-muted-color, #64748b); }
.block { display: block; }
.filters { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 1rem; }
.filter { min-width: 180px; }
.pager { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 0.75rem; }
.empty { padding: 1.5rem; text-align: center; color: var(--p-text-muted-color, #64748b); }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem 1.5rem; margin-bottom: 1rem; }
.detail-grid label, .body-label { display: block; font-size: 0.75rem; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--p-text-muted-color, #64748b); margin-bottom: 0.1rem; }
.body-label { margin-top: 0.5rem; }
.body-frame { width: 100%; height: 460px; border: 1px solid var(--p-surface-300, #d1d5db);
  border-radius: 6px; }
</style>
