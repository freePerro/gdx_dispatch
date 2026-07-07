<template>
    <section class="invoice-reminders-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Invoice Reminders</h2>
        </template>
        <template #end>
          <Button
            label="Preview"
            icon="pi pi-eye"
            severity="secondary"
            class="preview-button"
            @click="openPreview"
          />
          <Button
            label="Save Settings"
            icon="pi pi-save"
            severity="success"
            :loading="saving"
            @click="saveSettings"
          />
        </template>
      </Toolbar>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <Card v-else class="settings-card">
        <div class="form-grid">
          <div class="form-field toggle-field">
            <label>Enabled</label>
            <ToggleSwitch v-model="settings.enabled" />
          </div>

          <!-- PR6-billing-capture (Doug 2026-07-07): automated dunning is
               OPT-IN, default OFF. Flipping it on shows exactly who gets
               emailed BEFORE saving. -->
          <div class="form-field full-width auto-send-field" data-testid="reminders-auto-send">
            <div class="toggle-field">
              <label>Send reminder emails automatically</label>
              <ToggleSwitch v-model="settings.auto_send_enabled" data-testid="reminders-auto-send-toggle" @change="onAutoSendToggle" />
            </div>
            <p class="field-helper">
              Daily at the start of the office day: overdue invoices past a
              schedule threshold get the email template above. Manual "I
              called them" logs never pause it; the per-invoice
              "Pause reminders" switch does.
            </p>
            <!-- Audit round 2: the weekly "dunning is off" nudge promised a
                 permanent dismiss — this is that control (it was a phantom). -->
            <label v-if="!settings.auto_send_enabled" class="nudge-dismiss" data-testid="reminders-nudge-dismiss">
              <input type="checkbox" v-model="settings.auto_send_nudge_dismissed" />
              <span>Don't remind me weekly that automated reminders are off</span>
            </label>
            <div v-if="settings.auto_send_enabled && autoSendPreview" class="auto-send-preview" data-testid="reminders-auto-send-preview">
              <strong>
                {{ autoSendPreview.count }} invoice(s) qualify right now —
                {{ currency(autoSendPreview.total_balance) }} outstanding.
              </strong>
              <ul v-if="autoSendPreview.invoices.length">
                <li v-for="inv in autoSendPreview.invoices.slice(0, 10)" :key="inv.invoice_id">
                  {{ inv.invoice_number }} — {{ currency(inv.balance_due) }},
                  {{ inv.days_overdue }} days overdue ({{ inv.stage }})
                </li>
              </ul>
              <p class="field-helper">These send on the next daily run after you save.</p>
            </div>
          </div>

          <div class="form-field full-width">
            <label>Schedule days</label>
            <Chips v-model="scheduleDaysInput" placeholder="Add day count (e.g., 7)" />
            <p class="field-helper">Create reminders for the days after an invoice is due.</p>
          </div>

          <div class="form-field full-width">
            <label>Subject template</label>
            <InputText
              v-model="settings.subject_template"
              maxlength="500"
              placeholder="Reminder for {invoice_number}"
              class="w-full"
            />
          </div>

          <div class="form-field full-width">
            <label>Body template</label>
            <Textarea
              v-model="settings.body_template"
              rows="10"
              maxlength="10000"
              placeholder="Hi {customer_name},\n\nYour invoice {invoice_number} is {days_overdue} days overdue."
              class="w-full"
            />
          </div>

          <div class="form-field full-width">
            <p class="field-helper">
              Available variables: {customer_name}, {invoice_number}, {amount_due}, {days_overdue}, {due_date}
            </p>
          </div>
        </div>
      </Card>
    </section>

    <Dialog
      v-model:visible="previewDialog"
      header="Preview Invoice Reminder"
      :style="{ width: '960px' }"
      modal
    >
      <div class="preview-dialog">
        <div class="preview-form">
          <h3>Sample data</h3>
          <div class="horizontal-fields">
            <div class="form-field">
              <label>Invoice #</label>
              <InputText v-model="previewSample.invoice_number" />
            </div>
            <div class="form-field">
              <label>Customer</label>
              <InputText v-model="previewSample.customer_name" />
            </div>
          </div>
          <div class="horizontal-fields">
            <div class="form-field">
              <label>Amount due</label>
              <InputText v-model="previewSample.amount_due" />
            </div>
            <div class="form-field">
              <label>Days overdue</label>
              <InputText v-model="previewSample.days_overdue" type="number" />
            </div>
            <div class="form-field">
              <label>Due date</label>
              <InputText v-model="previewSample.due_date" type="date" />
            </div>
          </div>

          <Button
            label="Render Preview"
            icon="pi pi-bolt"
            class="preview-render"
            :loading="previewLoading"
            @click="renderPreview"
          />
          <p v-if="previewError" class="preview-error">{{ previewError }}</p>
        </div>

        <div class="preview-columns">
          <div class="preview-column">
            <h3>Raw template</h3>
            <p class="template-label">Subject</p>
            <div class="template-box">
              <pre>{{ settings.subject_template || '—' }}</pre>
            </div>
            <p class="template-label">Body</p>
            <div class="template-box">
              <pre>{{ settings.body_template || '—' }}</pre>
            </div>
          </div>

          <div class="preview-column">
            <h3>Rendered</h3>
            <p class="template-label">Subject</p>
            <div class="template-box rendered">
              <pre>{{ previewResult.subject || 'Run preview to render subject.' }}</pre>
            </div>
            <p class="template-label">Body</p>
            <div class="template-box rendered">
              <pre>{{ previewResult.body || 'Run preview to render body.' }}</pre>
            </div>
          </div>
        </div>
      </div>
    </Dialog>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Card from "primevue/card";
import Chips from "primevue/chips";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Textarea from "primevue/textarea";
import ToggleSwitch from "primevue/toggleswitch";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();

const loading = ref(true);
const saving = ref(false);
const previewDialog = ref(false);
const previewLoading = ref(false);
const previewError = ref("");

const settings = ref({
  enabled: false,
  schedule_days: [],
  subject_template: "",
  body_template: "",
  auto_send_enabled: false,
  auto_send_nudge_dismissed: false,
});

// PR6 — who would get emailed if auto-send were on (rendered on toggle).
const autoSendPreview = ref(null);

function currency(v) {
  const n = Number(v) || 0;
  return n.toLocaleString("en-US", { style: "currency", currency: "USD" });
}

async function onAutoSendToggle() {
  autoSendPreview.value = null;
  if (!settings.value.auto_send_enabled) return;
  try {
    autoSendPreview.value = await api.get("/api/invoice-reminders/auto-send-preview");
  } catch (_) {
    autoSendPreview.value = { count: 0, total_balance: 0, invoices: [] };
  }
}

const previewResult = ref({ subject: "", body: "" });
const previewSample = ref({
  invoice_number: "INV-1001",
  customer_name: "Acme Corp",
  amount_due: "1250.00",
  days_overdue: "7",
  due_date: new Date().toISOString().split("T")[0],
});

const scheduleDaysInput = computed({
  get: () => settings.value.schedule_days ?? [],
  set: (value) => {
    const numbers = Array.isArray(value)
      ? value.map((item) => Number(item)).filter((n) => Number.isFinite(n))
      : [];
    settings.value.schedule_days = Array.from(new Set(numbers));
  },
});

async function loadSettings() {
  loading.value = true;
  try {
    const data = await api.get("/api/invoice-reminders/settings");
    settings.value = {
      enabled: Boolean(data?.enabled),
      schedule_days: Array.isArray(data?.schedule_days) ? data.schedule_days : [],
      subject_template: data?.subject_template ?? "",
      body_template: data?.body_template ?? "",
      auto_send_enabled: Boolean(data?.auto_send_enabled),
      auto_send_nudge_dismissed: Boolean(data?.auto_send_nudge_dismissed),
    };
    if (settings.value.auto_send_enabled) onAutoSendToggle();
  } finally {
    loading.value = false;
  }
}

async function saveSettings() {
  saving.value = true;
  try {
    const payload = {
      enabled: settings.value.enabled,
      schedule_days: settings.value.schedule_days,
      subject_template: settings.value.subject_template,
      body_template: settings.value.body_template,
      auto_send_enabled: settings.value.auto_send_enabled,
      auto_send_nudge_dismissed: settings.value.auto_send_nudge_dismissed,
    };
    await api.post("/api/invoice-reminders/settings", payload);
  } finally {
    saving.value = false;
  }
}

function openPreview() {
  previewDialog.value = true;
  previewError.value = "";
}

async function renderPreview() {
  previewLoading.value = true;
  previewError.value = "";
  try {
    const payload = {
      invoice_number: previewSample.value.invoice_number,
      customer_name: previewSample.value.customer_name,
      amount_due: previewSample.value.amount_due,
      days_overdue: Number(previewSample.value.days_overdue || 0),
      due_date: previewSample.value.due_date,
    };
    const data = await api.post("/api/invoice-reminders/preview", payload);
    previewResult.value = {
      subject: data?.subject ?? "",
      body: data?.body ?? "",
    };
  } catch (error) {
    previewError.value = error?.message || "Unable to generate preview.";
  } finally {
    previewLoading.value = false;
  }
}

onMounted(loadSettings);
</script>

<style scoped>
.invoice-reminders-view {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.settings-card {
  padding: 1rem;
}

.form-grid {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.full-width {
  width: 100%;
}

.toggle-field {
  align-items: center;
  flex-direction: row;
  justify-content: space-between;
}

.field-helper {
  font-size: 0.85rem;
  color: var(--text-muted, #6b7280);
  margin: 0;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem 0;
}

.preview-button {
  margin-right: 0.5rem;
}

.preview-dialog {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.preview-form {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.horizontal-fields {
  display: flex;
  gap: 1rem;
}

.preview-render {
  align-self: flex-start;
}

.preview-error {
  color: #c53030;
  margin: 0;
}

.preview-columns {
  display: flex;
  gap: 1rem;
}

.preview-column {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.template-label {
  font-weight: 600;
  margin: 0;
}

.template-box {
  padding: 0.75rem;
  border: 1px solid #e5e7eb;
  border-radius: 0.375rem;
  background: #f8fafc;
  min-height: 120px;
  white-space: pre-wrap;
  word-break: break-word;
}

.rendered {
  background: #111827;
  color: #f8fafc;
}

pre {
  margin: 0;
  white-space: pre-wrap;
}

@media (max-width: 960px) {
  .horizontal-fields {
    flex-direction: column;
  }

  .preview-columns {
    flex-direction: column;
  }
}
</style>
