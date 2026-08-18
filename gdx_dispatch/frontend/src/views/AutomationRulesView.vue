<template>
  <section class="automation-rules">
    <div class="page-header">
      <div>
        <h1>Event Rules</h1>
        <p class="muted">
          When a business event happens — an invoice is paid, an estimate is
          sent — run an action automatically. Every email an active rule sends
          is recorded in the <RouterLink to="/email-log">Email Log</RouterLink>.
        </p>
      </div>
      <Button label="New rule" icon="pi pi-plus" data-testid="ar-new" @click="openCreate" />
    </div>

    <Message v-if="!emailsEnabled" severity="warn" :closable="false" class="mb-3"
      data-testid="ar-disabled-banner">
      Automation emails are OFF — rules with an email action will record
      "skipped_disabled" instead of sending. Turn them on in
      <RouterLink to="/settings">Settings → Feature Settings → Automation emails</RouterLink>.
    </Message>

    <DataTable :value="rules" :loading="loading" data-key="id" striped-rows
      data-testid="ar-table">
      <Column field="name" header="Rule" />
      <Column field="trigger_event" header="When">
        <template #body="{ data }">
          <Tag :value="triggerLabel(data.trigger_event)" severity="secondary" />
        </template>
      </Column>
      <Column header="Does">
        <template #body="{ data }">
          <span>{{ actionsSummary(data.actions) }}</span>
        </template>
      </Column>
      <Column header="Runs">
        <template #body="{ data }">
          <a href="#" data-testid="ar-runs-link" @click.prevent="openRuns(data)">
            {{ data.run_count || 0 }}
          </a>
          <small v-if="data.last_run_at" class="muted block">last {{ formatDateTime(data.last_run_at) }}</small>
        </template>
      </Column>
      <Column header="">
        <template #body="{ data }">
          <Button icon="pi pi-pencil" text size="small" data-testid="ar-edit" @click="openEdit(data)" />
          <Button v-if="confirmingDelete !== data.id" icon="pi pi-trash" text size="small"
            severity="danger" data-testid="ar-delete" @click="confirmingDelete = data.id" />
          <Button v-else label="Confirm delete?" size="small" severity="danger"
            data-testid="ar-delete-confirm" @click="removeRule(data)" />
        </template>
      </Column>
      <template #empty>
        <div class="empty">
          No rules yet. Create one — for example: when an invoice is paid,
          email the customer a thank-you.
        </div>
      </template>
    </DataTable>

    <!-- Create / edit -->
    <Dialog v-model:visible="showEditor" :header="editing ? 'Edit rule' : 'New rule'" modal
      :style="{ width: '640px' }" data-testid="ar-editor">
      <div class="form-field">
        <label>Name</label>
        <InputText v-model="form.name" class="w-full" data-testid="ar-name"
          placeholder="e.g. Thank-you email when an invoice is paid" />
      </div>
      <div class="form-field">
        <label>When this happens</label>
        <Select v-model="form.trigger_event" :options="TRIGGERS" option-label="label"
          option-value="value" class="w-full" data-testid="ar-trigger" />
        <small class="muted">The rule runs on every matching event. Conditions
          (e.g. only invoices over $500) aren't configurable here yet.</small>
      </div>
      <div class="form-field">
        <label>Send this email to the customer</label>
        <InputText v-model="form.subject" class="w-full mb-2" placeholder="Subject"
          data-testid="ar-subject" />
        <Textarea v-model="form.body" rows="6" class="w-full" data-testid="ar-body"
          placeholder="Hi {{customer_name}}, ..." />
        <small class="muted">
          Placeholders: <code v-pre>{{customer_name}}</code>,
          <code v-pre>{{company_name}}</code>, plus any field the event carries
          (e.g. <code v-pre>{{invoice_number}}</code> on invoice events). The
          message is sent branded, to the account's default contact, from the
          Send-as account in Settings.
        </small>
      </div>
      <template #footer>
        <Button label="Cancel" text @click="showEditor = false" />
        <Button :label="editing ? 'Save' : 'Create rule'" icon="pi pi-check"
          :loading="saving" :disabled="!canSave" data-testid="ar-save" @click="saveRule" />
      </template>
    </Dialog>

    <!-- Runs -->
    <Dialog v-model:visible="showRuns" :header="`Runs — ${runsRule?.name || ''}`" modal
      :style="{ width: '680px' }" data-testid="ar-runs">
      <DataTable :value="runs" :loading="runsLoading" data-key="id" striped-rows>
        <Column field="triggered_at" header="When">
          <template #body="{ data }">{{ formatDateTime(data.triggered_at) }}</template>
        </Column>
        <Column field="status" header="Status">
          <template #body="{ data }">
            <Tag :value="data.status" :severity="data.status === 'success' ? 'success' : data.status === 'skipped' ? 'secondary' : 'danger'" />
          </template>
        </Column>
        <Column header="Actions">
          <template #body="{ data }">
            <div v-for="(a, i) in data.actions_run || []" :key="i">
              <code>{{ a.action_type }}</code> → {{ a.result }}
            </div>
            <span v-if="!(data.actions_run || []).length" class="muted">conditions not met</span>
          </template>
        </Column>
      </DataTable>
    </Dialog>
    <Toast />
  </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import Message from "primevue/message";
import Select from "primevue/select";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
import Toast from "primevue/toast";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatDateTime } from "../composables/useFormatters";

const api = useApiWithToast();

// Mirrors modules/workflows/engine.SUPPORTED_TRIGGERS — the server 422s
// anything else, so drift shows up loudly, not as a dead rule.
const TRIGGERS = [
  { value: "invoice.paid", label: "Invoice paid" },
  { value: "estimate.sent", label: "Estimate sent" },
  { value: "job.created", label: "Job created" },
  { value: "customer.created", label: "Customer created" },
];

const rules = ref([]);
const loading = ref(false);
const emailsEnabled = ref(true);
const showEditor = ref(false);
const editing = ref(null);
const saving = ref(false);
const confirmingDelete = ref(null);
const form = ref({ name: "", trigger_event: "invoice.paid", subject: "", body: "" });
const showRuns = ref(false);
const runsRule = ref(null);
const runs = ref([]);
const runsLoading = ref(false);

const canSave = computed(() =>
  form.value.name.trim() && form.value.trigger_event
  && form.value.subject.trim() && form.value.body.trim()
);

function triggerLabel(v) {
  return TRIGGERS.find((t) => t.value === v)?.label || v;
}

function actionsSummary(actions) {
  const mail = (actions || []).find((a) => a.action_type === "send_email");
  if (mail) return `Email: "${mail.params?.subject || "(no subject)"}"`;
  return (actions || []).map((a) => a.action_type).join(", ") || "—";
}

async function load() {
  loading.value = true;
  try {
    const data = await api.get("/api/workflows");
    rules.value = (data?.data || data) ?? [];
  } finally {
    loading.value = false;
  }
  try {
    const s = await api.get("/api/settings", { suppressErrorToast: true });
    emailsEnabled.value = !!(s?.data || s)?.automation_emails_enabled;
  } catch { /* banner defaults to enabled-look; settings needs admin */ }
}

function openCreate() {
  editing.value = null;
  form.value = { name: "", trigger_event: "invoice.paid", subject: "", body: "" };
  showEditor.value = true;
}

function openEdit(rule) {
  editing.value = rule;
  const mail = (rule.actions || []).find((a) => a.action_type === "send_email");
  form.value = {
    name: rule.name,
    trigger_event: rule.trigger_event,
    subject: mail?.params?.subject || "",
    body: mail?.params?.body || "",
  };
  showEditor.value = true;
}

async function saveRule() {
  saving.value = true;
  try {
    const payload = {
      name: form.value.name.trim(),
      trigger_event: form.value.trigger_event,
      actions: [{
        action_type: "send_email",
        params: { subject: form.value.subject.trim(), body: form.value.body },
      }],
    };
    if (editing.value) {
      await api.put(`/api/workflows/${editing.value.id}`, payload,
        { successMessage: "Rule updated." });
    } else {
      await api.post("/api/workflows", payload,
        { successMessage: "Rule created — it runs on the next matching event." });
    }
    showEditor.value = false;
    await load();
  } finally {
    saving.value = false;
  }
}

async function removeRule(rule) {
  // Two-click confirm on purpose: the global confirm dialog auto-accepts
  // silently (issue #215) and must not guard deletes.
  confirmingDelete.value = null;
  await api.delete(`/api/workflows/${rule.id}`, { successMessage: "Rule deleted." });
  await load();
}

async function openRuns(rule) {
  runsRule.value = rule;
  showRuns.value = true;
  runsLoading.value = true;
  try {
    const data = await api.get(`/api/workflows/${rule.id}/runs`);
    runs.value = (data?.data || data) ?? [];
  } finally {
    runsLoading.value = false;
  }
}

onMounted(load);
</script>

<style scoped>
.automation-rules { padding: 1rem 1.25rem; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 1rem; gap: 1rem; }
.page-header h1 { margin: 0 0 0.25rem; font-size: 1.35rem; }
.muted { color: var(--p-text-muted-color, #64748b); }
.block { display: block; }
.mb-2 { margin-bottom: 0.5rem; }
.mb-3 { margin-bottom: 1rem; }
.form-field { margin-bottom: 1rem; }
.form-field > label { display: block; font-weight: 600; margin-bottom: 0.35rem; }
.empty { padding: 1.5rem; text-align: center; color: var(--p-text-muted-color, #64748b); }
</style>
