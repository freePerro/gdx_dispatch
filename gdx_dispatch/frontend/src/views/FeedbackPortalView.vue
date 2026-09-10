<!--
  Feedback Portal (cc2-s49c).

  Tenant-side view at /feedback. Lets the user file feature requests +
  see their tenant's prior submissions (bugs and features). Bug
  reporting is also available via the floating BugReportButton —
  this view is where someone goes when they want to browse history
  or formally file a feature request.

  Posts to /api/support/feature; reads /api/support/my.
  Uses AppLayout per gdx/docs/frontend_view_pattern.md.
-->
<template>
    <section class="feedback-portal view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">Feedback &amp; Feature Requests</h1>
        </template>
        <template #end>
          <Select
            v-model="categoryFilter"
            :options="categoryOptions"
            optionLabel="label"
            optionValue="value"
            placeholder="All categories"
            class="filter-select"
            @change="fetchTickets"
          />
          <Button
            label="Refresh"
            icon="pi pi-refresh"
            severity="secondary"
            @click="fetchTickets"
          />
        </template>
      </Toolbar>

      <!-- Submit form -->
      <div class="submit-card">
        <h2>Suggest a feature</h2>
        <p class="submit-help">
          Tell us what would make GDX Dispatch work better for your
          shop. We read everything; the team triages weekly.
        </p>
        <div class="submit-form">
          <div class="field">
            <label>Subject</label>
            <InputText
              v-model="form.subject"
              placeholder="One-line summary"
              data-testid="feedback-subject"
            />
          </div>
          <div class="field">
            <label>Details</label>
            <Textarea
              v-model="form.body"
              rows="4"
              placeholder="What problem does this solve? Who would use it?"
              data-testid="feedback-body"
            />
          </div>
          <div class="field">
            <label>Priority</label>
            <Select
              v-model="form.priority"
              :options="priorityOptions"
              optionLabel="label"
              optionValue="value"
              data-testid="feedback-priority"
            />
          </div>
          <div class="submit-row">
            <Button
              label="Submit feature request"
              icon="pi pi-send"
              :loading="submitting"
              :disabled="!form.subject || !form.body"
              @click="submit"
              data-testid="feedback-submit"
            />
          </div>
        </div>
      </div>

      <!-- History -->
      <div v-if="error" class="error-banner">{{ error }}</div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <div v-else-if="tickets.length === 0" class="empty-state">
        <p>No prior submissions yet.</p>
      </div>

      <!-- #622: a row opens to what the reporter actually wrote — the body
           (with the page and browser the bug button appends) was stored and
           shown nowhere — and, for the office, a way to close it. -->
      <DataTable
        v-else
        v-model:expandedRows="expandedRows"
        :value="tickets"
        dataKey="id"
        responsiveLayout="scroll"
        stripedRows
        class="clickable-rows"
        @row-click="toggleTicket($event.data)"
      >
        <Column expander style="width: 3rem" />
        <Column field="created_at" header="Submitted">
          <template #body="{ data }">
            {{ shortDate(data.created_at) }}
          </template>
        </Column>
        <Column field="subject" header="Subject" />
        <Column field="category" header="Category">
          <template #body="{ data }">
            <Tag :value="data.category" severity="info" />
          </template>
        </Column>
        <Column field="status" header="Status">
          <template #body="{ data }">
            <Tag :value="data.status" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column field="priority" header="Priority" />
        <Column field="resolution_summary" header="Resolution">
          <template #body="{ data }">
            <span v-if="data.resolution_summary">
              {{ data.resolution_summary }}
            </span>
            <span v-else class="muted">—</span>
          </template>
        </Column>
        <template #expansion="{ data }">
          <div class="ticket-detail" :data-testid="`ticket-detail-${data.id}`">
            <p class="ticket-meta">
              <template v-if="data.opened_by_email">
                Reported by <strong>{{ data.opened_by_email }}</strong> on
              </template>
              <template v-else>Reported </template>{{ shortDate(data.created_at) }}
            </p>
            <!-- The server sends a body only to the team and to the ticket's
                 own reporter (it can carry customer details). -->
            <pre v-if="data.body != null" class="ticket-body" data-testid="ticket-body">{{ data.body || 'No description was given.' }}</pre>
            <p v-else class="muted" data-testid="ticket-body-withheld">
              Only the team and the person who reported it can read this report.
            </p>
            <p v-if="data.status === 'closed'" class="ticket-resolution" data-testid="ticket-resolution">
              Closed {{ shortDate(data.closed_at) }} — {{ data.resolution_summary || 'no resolution recorded' }}
            </p>
            <Button
              v-else-if="canClose"
              label="Close ticket…"
              icon="pi pi-check-circle"
              severity="secondary"
              size="small"
              data-testid="ticket-close"
              @click.stop="openClose(data)"
            />
          </div>
        </template>
      </DataTable>

      <Dialog
        v-model:visible="closeDialogVisible"
        header="Close ticket"
        modal
        :style="{ width: '520px' }"
        data-testid="ticket-close-dialog"
      >
        <p v-if="closeTarget" class="close-subject">{{ closeTarget.subject }}</p>
        <div class="field">
          <label for="ticket-resolution-input">Resolution *</label>
          <Textarea
            id="ticket-resolution-input"
            v-model="closeResolution"
            rows="4"
            placeholder="What was done — shown on the ticket"
            data-testid="ticket-resolution-input"
          />
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="closeDialogVisible = false" />
          <Button
            label="Close ticket"
            icon="pi pi-check"
            :loading="closing"
            :disabled="!closeResolution.trim()"
            data-testid="ticket-close-confirm"
            @click="confirmClose"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, ref, onMounted } from "vue";
import { useToast } from "primevue/usetoast";
import { useApi } from "../composables/useApi";
import { useAuthStore } from "../stores/auth";
import Dialog from "primevue/dialog";
import { formatDate } from "../composables/useFormatters";
import Toolbar from "primevue/toolbar";
import Button from "primevue/button";
import InputText from "primevue/inputtext";
import Textarea from "primevue/textarea";
import Select from "primevue/select";
import DataTable from "primevue/datatable";
import Column from "primevue/column";
import Tag from "primevue/tag";
import ProgressSpinner from "primevue/progressspinner";

const api = useApi();
const toast = useToast();
const auth = useAuthStore();
// Closing is the team's call, not the reporter's — the same key the server
// gates the close on (settings.write, the admin pages' permission).
const canClose = computed(() => auth.hasPermission("settings.write"));

const expandedRows = ref({});
const closeDialogVisible = ref(false);
const closeTarget = ref(null);
const closeResolution = ref("");
const closing = ref(false);

const tickets = ref([]);
const loading = ref(false);
const error = ref(null);
const submitting = ref(false);
const categoryFilter = ref(null);

const categoryOptions = [
  { label: "All categories", value: null },
  { label: "Bug", value: "bug" },
  { label: "Feature", value: "feature" },
  { label: "Question", value: "question" },
  { label: "Other", value: "other" },
];

const priorityOptions = [
  { label: "Low", value: "low" },
  { label: "Medium", value: "medium" },
  { label: "High", value: "high" },
  { label: "Urgent", value: "urgent" },
];

const form = ref({
  subject: "",
  body: "",
  priority: "medium",
});

async function fetchTickets() {
  loading.value = true;
  error.value = null;
  try {
    // useApi.get() has no `params` option (silently ignored) and returns
    // the parsed body directly — the old `{ params }` + `res.data.items`
    // pair meant the filter never applied AND the success path threw on
    // undefined `.data`, so the portal always showed "Could not load".
    const qs = categoryFilter.value
      ? `?category=${encodeURIComponent(categoryFilter.value)}`
      : "";
    const res = await api.get(`/api/support/my${qs}`);
    tickets.value = res.items || [];
  } catch (e) {
    error.value = "Could not load your submissions.";
    tickets.value = [];
  } finally {
    loading.value = false;
  }
}

async function submit() {
  if (!form.value.subject || !form.value.body) return;
  submitting.value = true;
  try {
    await api.post("/api/support/feature", {
      subject: form.value.subject,
      body: form.value.body,
      priority: form.value.priority,
    });
    toast.add({
      severity: "success",
      summary: "Submitted",
      detail: "Thanks — we'll review it.",
      life: 4000,
    });
    form.value.subject = "";
    form.value.body = "";
    form.value.priority = "medium";
    await fetchTickets();
  } catch (e) {
    toast.add({
      severity: "error",
      summary: "Could not submit",
      detail: "Try again — if it persists, email support.",
      life: 4000,
    });
  } finally {
    submitting.value = false;
  }
}

function shortDate(iso) {
  return formatDate(iso);
}

function toggleTicket(row) {
  const next = { ...expandedRows.value };
  if (next[row.id]) delete next[row.id];
  else next[row.id] = true;
  expandedRows.value = next;
}

function openClose(row) {
  closeTarget.value = row;
  closeResolution.value = "";
  closeDialogVisible.value = true;
}

async function confirmClose() {
  const resolution = closeResolution.value.trim();
  if (!closeTarget.value || !resolution) return;
  closing.value = true;
  try {
    await api.post(`/api/support/tickets/${closeTarget.value.id}/close`, {
      resolution_summary: resolution,
    });
    toast.add({ severity: "success", summary: "Ticket closed", detail: closeTarget.value.subject, life: 4000 });
    closeDialogVisible.value = false;
    await fetchTickets();
  } catch (e) {
    toast.add({
      severity: "error",
      summary: "Could not close the ticket",
      detail: e?.body?.detail || e?.message || "Try again.",
      life: 5000,
    });
  } finally {
    closing.value = false;
  }
}

function statusSeverity(status) {
  if (status === "closed") return "success";
  if (status === "in_progress") return "warn";
  return "info";
}

onMounted(fetchTickets);
</script>

<style scoped>
.feedback-portal {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.submit-card {
  padding: 1.25rem 1.5rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 8px;
  background: var(--p-content-background);
}
.submit-card h2 {
  margin: 0 0 0.25rem 0;
  font-size: 1.1rem;
}
.submit-help {
  margin: 0 0 1rem 0;
  color: var(--p-text-muted-color);
  font-size: 0.9rem;
}
.submit-form {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.field label {
  font-weight: 600;
  font-size: 0.85rem;
}
.submit-row {
  display: flex;
  justify-content: flex-end;
}
.muted {
  color: var(--p-text-muted-color);
}
.error-banner {
  padding: 0.5rem 0.75rem;
  background: #fee;
  border: 1px solid #fcc;
  border-radius: 6px;
  color: #900;
}
.empty-state {
  text-align: center;
  padding: 2rem;
  color: var(--p-text-muted-color);
}
.ticket-detail {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  padding: 0.5rem 0.75rem;
}
.ticket-meta,
.ticket-resolution,
.close-subject {
  margin: 0;
}
.ticket-meta {
  color: var(--p-text-muted-color);
  font-size: 0.85rem;
}
.ticket-body {
  margin: 0;
  padding: 0.75rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-family: inherit;
  font-size: 0.9rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
  background: var(--p-content-hover-background, transparent);
  color: var(--p-text-color);
}
.ticket-resolution {
  font-weight: 600;
}
.close-subject {
  font-weight: 600;
  margin-bottom: 0.75rem;
}
</style>
