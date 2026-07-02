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
          Tell us what would make DispatchApp work better for your
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

      <DataTable
      responsiveLayout="scroll" v-else :value="tickets" stripedRows>
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
      </DataTable>
    </section>
</template>

<script setup>
import { ref, onMounted } from "vue";
import { useToast } from "primevue/usetoast";
import { useApi } from "../composables/useApi";
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
    const params = categoryFilter.value ? { category: categoryFilter.value } : {};
    const res = await api.get("/api/support/my", { params });
    tickets.value = res.data.items || [];
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

function statusSeverity(status) {
  if (status === "closed") return "success";
  if (status === "in_progress") return "warning";
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
</style>
