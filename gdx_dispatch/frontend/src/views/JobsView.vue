<template>
    <section class="jobs-view view-card">
      <Toolbar class="jobs-toolbar">
        <template #start>
          <InputText
            id="jobs-search"
            name="jobs-search"
            v-model="searchQuery"
            data-testid="jobs-search"
            placeholder="Search jobs by title or customer..."
            class="search-input"
          />
        </template>
        <template #end>
          <Button label="+ New Job" icon="pi pi-plus" data-testid="new-job-btn" @click="openCreateDialog" />
        </template>
      </Toolbar>

      <div v-if="isLoading" class="spinner-wrap" data-testid="jobs-loading">
        <ProgressSpinner />
      </div>

      <!-- Status filter tabs (single source of truth — earlier dual stats-bar
           was deleted 2026-04-29 nav cleanup; the tabs already cover every
           lifecycle bucket plus "All"). -->
      <div class="status-tabs" data-testid="jobs-status-tabs">
        <Button
          v-for="tab in statusTabs"
          :key="tab.key"
          :label="tab.label"
          :badge="hasLoadedOnce ? String(tab.count) : ''"
          :class="{ 'p-button-outlined': activeStatus !== tab.key }"
          :data-testid="`jobs-status-${tab.key.toLowerCase().replace(/ /g, '-')}`"
          size="small"
          @click="setStatusFilter(tab.key)"
        />
      </div>

      <DataTable
        class="clickable-rows jobs-table"
        v-if="!isLoading"
        :value="filteredJobs"
        :paginator="true"
        :rows="20"
        :rowsPerPageOptions="[10, 20, 50, 100]"
        paginatorTemplate="FirstPageLink PrevPageLink PageLinks NextPageLink LastPageLink RowsPerPageDropdown CurrentPageReport"
        currentPageReportTemplate="{first}-{last} of {totalRecords}"
        data-testid="jobs-datatable"
        stripedRows
        responsiveLayout="scroll"
        @row-click="openJobDetail($event.data)"
        
      >
        <template #empty>
          <div class="empty-message">
            {{ searchQuery || activeStatus !== 'All' ? 'No matching jobs. Try clearing your filters.' : 'No jobs yet. Click "+ New Job" to create one.' }}
          </div>
        </template>
        <Column field="jobNumber" header="Job #" sortable style="width: 110px">
          <template #body="{ data }">
            <span class="job-number">{{ data.jobNumber }}</span>
          </template>
        </Column>
        <Column field="customer" header="Customer" sortable>
          <template #body="{ data }">
            <span v-if="data.customer">{{ data.customer }}</span>
            <span v-else class="muted-cell">—</span>
          </template>
        </Column>
        <Column field="title" header="Title" sortable />
        <Column field="status" header="Status" sortable style="width: 130px">
          <template #body="{ data }">
            <JobStateChip :job="data" />
          </template>
        </Column>
        <Column field="scheduledDate" header="Scheduled Date" sortable style="width: 150px">
          <template #body="{ data }">
            {{ formatDate(data.scheduledDate) }}
          </template>
        </Column>
        <Column field="tech" header="Tech" sortable style="width: 140px" />
        <Column field="priority" header="Priority" sortable style="width: 120px">
          <template #body="{ data }">
            <span class="priority-cell">
              <span class="priority-dot" :class="`priority-${(data.priority || 'Normal').toLowerCase()}`"></span>
              {{ data.priority || 'Normal' }}
            </span>
          </template>
        </Column>
        <Column header="Actions" style="width: 100px; text-align: center">
          <template #body="{ data }">
            <Button
              icon="pi pi-pencil" aria-label="Edit"
              text
              rounded
              size="small"
              :data-testid="`job-edit-${data.id}`"
              @click.stop="openEditDialog(data)"
            />
            <Button
              icon="pi pi-trash" aria-label="Delete"
              text
              rounded
              severity="danger"
              size="small"
              :data-testid="`job-delete-${data.id}`"
              @click.stop="promptDelete(data)"
            />
          </template>
        </Column>
      </DataTable>

      <!-- Create/Edit Dialog -->
      <Dialog
        v-model:visible="showFormDialog"
        :header="isEditMode ? 'Edit Job' : 'Create Job'"
        :style="{ width: '550px' }"
        :breakpoints="{ '768px': '95vw' }"
        modal
        data-testid="jobs-form-dialog"
      >
        <form class="dialog-form" @submit.prevent="submitForm">
          <div class="form-field">
            <label for="job-customer">Customer *</label>
            <Select
              id="job-customer"
              v-model="jobForm.customer_id"
              :options="customerOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="Select customer"
              filter
              showClear
              :disabled="jobForm.new_customer"
              data-testid="job-customer-dropdown"
              class="w-full"
            />
            <div class="toggle-row">
              <ToggleSwitch
                v-model="jobForm.new_customer"
                @change="handleNewCustomerToggle"
                data-testid="new-job-new-customer-toggle"
              />
              <span class="toggle-label">Create new customer instead</span>
            </div>
          </div>

          <div v-if="jobForm.new_customer" class="new-client-section">
            <div class="form-row">
              <div class="form-field">
                <label for="new-cust-name">Name *</label>
                <InputText
                  id="new-cust-name"
                  v-model="jobForm.new_cust_name"
                  placeholder="John Smith"
                  class="w-full"
                  data-testid="new-job-new-cust-name-input"
                />
              </div>
              <div class="form-field">
                <label for="new-cust-phone">Phone</label>
                <InputText
                  id="new-cust-phone"
                  v-model="jobForm.new_cust_phone"
                  placeholder="(555) 555-5555"
                  class="w-full"
                  data-testid="new-job-new-cust-phone-input"
                />
              </div>
            </div>
            <div class="form-row">
              <div class="form-field">
                <label for="new-cust-email">Email</label>
                <InputText
                  id="new-cust-email"
                  v-model="jobForm.new_cust_email"
                  placeholder="john@example.com"
                  class="w-full"
                  data-testid="new-job-new-cust-email-input"
                />
              </div>
              <div class="form-field">
                <label for="new-cust-address">Address</label>
                <Textarea
                  id="new-cust-address"
                  v-model="jobForm.new_cust_address"
                  rows="3"
                  placeholder="123 Main St, City, ST"
                  class="w-full"
                  data-testid="new-job-new-cust-address-input"
                />
              </div>
            </div>
          </div>

          <div class="form-field">
            <label for="job-title">{{ jobTitleLabel }}</label>
            <InputText id="job-title" v-model="jobForm.title" data-testid="job-title-input" class="w-full" />
          </div>

          <div class="form-field">
            <label for="job-description">Description</label>
            <Textarea id="job-description" v-model="jobForm.description" rows="3" data-testid="job-description-input" class="w-full" />
          </div>

          <div class="form-row">
            <div class="form-field">
              <label for="job-scheduled">Scheduled Date</label>
              <DatePicker
                id="job-scheduled"
                v-model="jobForm.scheduled_at"
                showTime
                hourFormat="12"
                dateFormat="mm/dd/yy"
                showIcon
                data-testid="job-scheduled-input"
                class="w-full"
              />
            </div>

            <div class="form-field">
              <label for="job-priority">Priority</label>
              <Select
                id="job-priority"
                v-model="jobForm.priority"
                :options="priorityOptions"
                data-testid="job-priority-dropdown"
                class="w-full"
              />
            </div>
          </div>

          <div class="form-row">
            <div class="form-field">
              <label for="job-type">Job Type</label>
              <Select
                id="job-type"
                v-model="jobForm.job_type"
                :options="jobTypeOptions"
                data-testid="job-type-dropdown"
                class="w-full"
              />
            </div>

            <div class="form-field">
              <label for="job-duration-hours">Estimated time (hours)</label>
              <InputText
                id="job-duration-hours"
                v-model="jobForm.scheduled_duration_hours"
                type="number"
                step="0.25"
                min="0"
                placeholder="e.g. 1.5"
                data-testid="job-duration-hours-input"
                class="w-full"
              />
              <small class="form-hint">Drives dispatch capacity bars + per-tech efficiency. Leave blank if unsure.</small>
            </div>
          </div>

          <!--
            Sprint customer-multi-location (2026-05-21) — only render when
            the picked customer has more than one site. Defaults to the
            primary (or first) location; the user can clear it to fall
            back to the customer's address.
          -->
          <div v-if="customerLocations.length > 1" class="form-field" data-testid="job-location-field">
            <label for="job-location">Service location</label>
            <Select
              id="job-location"
              v-model="jobForm.location_id"
              :options="locationOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="Use customer's primary"
              showClear
              data-testid="job-location-dropdown"
              class="w-full"
            />
            <small class="form-hint">{{ customerLocations.length }} sites on file. Leave blank to use the customer's primary.</small>
          </div>

          <div class="form-row">
            <div class="form-field">
              <label for="job-tech">Assigned Techs</label>
              <MultiSelect
                id="job-tech"
                v-model="jobForm.assigned_tech_ids"
                :options="techOptions"
                optionLabel="label"
                optionValue="value"
                placeholder="Select tech(s)"
                display="chip"
                filter
                showClear
                data-testid="job-tech-dropdown"
                class="w-full"
              />
              <small v-if="(jobForm.assigned_tech_ids || []).length > 1" class="lead-row">
                Lead:
                <Select
                  v-model="jobForm.lead_tech_id"
                  :options="leadTechOptions"
                  optionLabel="label"
                  optionValue="value"
                  placeholder="(first tech)"
                  showClear
                  class="lead-select"
                />
              </small>
            </div>
          </div>

          <div class="form-field">
            <label for="job-notes">Dispatch notes for tech</label>
            <Textarea id="job-notes" v-model="jobForm.notes" rows="2" data-testid="job-notes-input" class="w-full" />
          </div>

          <div class="form-field appointment-toggle-row">
            <div class="toggle-row">
              <ToggleSwitch
                v-model="jobForm.appt_schedule"
                data-testid="new-job-appt-schedule-toggle"
              />
              <span class="toggle-label">Schedule an appointment?</span>
            </div>
          </div>

          <div v-if="jobForm.appt_schedule" class="appointment-section">
            <div class="form-row">
              <div class="form-field">
              <label for="appt-date">Date *</label>
              <DatePicker
                  id="appt-date"
                  v-model="jobForm.appt_date"
                  dateFormat="mm/dd/yy"
                  showIcon
                  :minDate="new Date()"
                  allowInput
                  data-testid="new-job-appt-date"
                  class="w-full"
                />
              </div>
              <div class="form-field">
                <label for="appt-time">Time</label>
                <InputText
                  id="appt-time"
                  type="time"
                  v-model="jobForm.appt_time"
                  class="w-full"
                  data-testid="new-job-appt-time"
                />
              </div>
            </div>
            <div class="form-field">
              <label for="appt-notes">Appointment Notes (optional)</label>
              <Textarea
                id="appt-notes"
                v-model="jobForm.appt_notes"
                rows="3"
                placeholder="Call before arrival"
                class="w-full"
                data-testid="new-job-appt-notes"
              />
            </div>
          </div>

          <!-- Status transition (edit mode only) -->
          <div v-if="isEditMode" class="form-field">
            <label>Status</label>
            <div class="status-transition-bar">
              <Button
                v-for="s in statusFlow"
                :key="s"
                :label="s"
                :severity="jobForm.status === s ? 'primary' : 'secondary'"
                :outlined="jobForm.status !== s"
                size="small"
                :data-testid="`job-status-${s.toLowerCase().replace(/ /g, '-')}`"
                @click="jobForm.status = s"
              />
            </div>
          </div>

          <div v-if="formError" class="inline-error" data-testid="job-form-error">{{ formError }}</div>

          <div class="form-actions">
            <Button type="button" label="Cancel" text @click="showFormDialog = false" />
            <Button v-if="isEditMode && jobForm.status === 'Complete'" type="button"
              label="Create Invoice" icon="pi pi-dollar" severity="success"
              @click="createInvoiceFromJob" data-testid="job-create-invoice" />
            <Button
              type="submit"
              :label="isEditMode ? 'Save Changes' : 'Create Job'"
              :loading="isSaving"
              :disabled="hardGateBlocksSave"
              :title="hardGateBlocksSave ? 'A technician is required for scheduled jobs (tenant policy).' : undefined"
              data-testid="job-submit-btn"
            />
          </div>
        </form>
      </Dialog>

      <!-- Soft-gate: scheduled job with no tech (2026-05-01).
           Tenant has dispatch_warn_save_no_tech enabled. -->
      <Dialog
        v-model:visible="showSoftGateDialog"
        header="No tech assigned"
        :style="{ width: '420px' }"
        modal
        @hide="onSoftGateHide"
        data-testid="job-softgate-dialog"
      >
        <p style="margin-top:0">
          This job has a scheduled date but no technician assigned. It will land in the
          <strong>Scheduled — Not Assigned</strong> lane on Dispatch until someone is picked.
        </p>
        <div class="form-actions">
          <Button label="Go back" text @click="cancelSoftGate" data-testid="softgate-cancel" />
          <Button label="Save anyway" severity="warn" @click="confirmSoftGate" data-testid="softgate-confirm" />
        </div>
      </Dialog>

      <!-- Delete Confirmation -->
      <Dialog
        v-model:visible="showDeleteDialog"
        header="Confirm Delete"
        :style="{ width: '400px' }"
        modal
        data-testid="job-delete-dialog"
      >
        <p>Are you sure you want to delete job <strong>{{ deleteTarget?.jobNumber }}</strong>?</p>
        <p style="color: var(--p-text-muted-color)">This action cannot be undone.</p>
        <div class="form-actions">
          <Button label="Cancel" text @click="showDeleteDialog = false" />
          <Button
            label="Delete"
            severity="danger"
            :loading="isDeleting"
            data-testid="job-confirm-delete-btn"
            @click="confirmDelete"
          />
        </div>
      </Dialog>

      <Toast data-testid="jobs-toast" />
    </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useToast } from "primevue/usetoast";
import { useApiWithToast } from "../composables/useApiWithToast";
import { useListPrefs } from "../composables/useListPrefs";
import Button from "primevue/button";
import DatePicker from "primevue/datepicker";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Select from "primevue/select";
import MultiSelect from "primevue/multiselect";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import JobStateChip from "../components/JobStateChip.vue";
import Textarea from "primevue/textarea";
import ToggleSwitch from "primevue/toggleswitch";
import Toast from "primevue/toast";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();
const toast = useToast();
const router = useRouter();
const route = useRoute();

function createInvoiceFromJob() {
  // Navigate to billing with query params that BillingView can use to pre-fill
  const params = new URLSearchParams({
    customer_id: jobForm.value.customer_id || "",
    job_id: jobForm.value.id || "",
  });
  router.push(`/billing?${params.toString()}&action=create`);
}

const jobs = ref([]);
const customers = ref([]);
const technicians = ref([]);
// Global aggregates from /api/jobs API — covers ALL jobs, not just the current page.
// Shape: { total: number, status_counts: { "Scheduled": N, "Complete": M, ... } }
const globalTotal = ref(0);
const globalStatusCounts = ref({});
const searchQuery = ref("");
const activeStatus = ref("All");
// Persist the chosen status tab + search across reloads (and across the
// "Back to Jobs" buttons on JobDetailView, which hard-push /jobs). Valid set
// mirrors statusTabs; a stale/removed status falls back to "All" so the list
// never silently filters to empty.
const JOB_STATUS_KEYS = ["All", "Service Call", "Estimate", "Scheduled", "In Progress", "Complete"];
useListPrefs(
  "jobs",
  { activeStatus, searchQuery },
  {
    activeStatus: { default: "All", valid: (v) => JOB_STATUS_KEYS.includes(v) },
    searchQuery: { default: "", valid: (v) => typeof v === "string" },
  },
);
const isLoading = ref(false);
// Track whether the first fetch resolved so we can suppress the tab-count
// badges during the initial ~300ms load (else every tab flashes "0").
const hasLoadedOnce = ref(false);
const isSaving = ref(false);
const isDeleting = ref(false);
const formError = ref("");
const showFormDialog = ref(false);
const showDeleteDialog = ref(false);
const formMode = ref("create");
const deleteTarget = ref(null);

// Canonical lifecycle stages (matches jobs.lifecycle_stage PG enum).
// Phase D audit fix: dropped "Sold" (not in enum, write would silently
// fail) and "Invoiced" (lives on billing_status, not lifecycle).
const statusFlow = ["Service Call", "Estimate", "Scheduled", "In Progress", "Complete", "Cancelled"];
const jobTypeOptions = ["Service Call", "Installation", "Repair", "Maintenance"];
const priorityOptions = ["Low", "Normal", "High", "Urgent"];

const jobForm = ref(emptyForm());

function emptyForm() {
  return {
    id: null,
    title: "",
    description: "",
    customer_id: null,
    job_type: "Service Call",
    priority: "Normal",
    scheduled_at: null,
    // Sprint dispatch-capacity (2026-05-21) — scheduler's expected
    // duration in decimal hours. Powers the dispatch board's capacity
    // bar + per-tech efficiency report. Null = no estimate yet.
    scheduled_duration_hours: null,
    // Sprint customer-multi-location (2026-05-21) — null = use the
    // customer's primary location (existing JobDetailView fallback path).
    location_id: null,
    assigned_tech_id: null,
    assigned_tech_ids: [],
    lead_tech_id: null,
    notes: "",
    status: "Estimate",
    new_customer: false,
    new_cust_name: "",
    new_cust_phone: "",
    new_cust_email: "",
    new_cust_address: "",
    appt_schedule: false,
    appt_date: null,
    appt_time: "",
    appt_notes: "",
  };
}

const isEditMode = computed(() => formMode.value === "edit");
const isServiceCall = computed(() => jobForm.value.job_type === 'Service Call');
const jobTitleLabel = computed(() => (isServiceCall.value ? 'Problem description *' : 'Job title *'));

// Dispatch settings: tenant can warn or block when a job is scheduled
// without a tech. Flags loaded once at mount; defaults are off.
const dispatchSettings = ref({
  dispatch_warn_save_no_tech: false,
  dispatch_block_save_no_tech: false,
});
const scheduledWithoutTech = computed(() =>
  Boolean(jobForm.value.scheduled_at) && !(jobForm.value.assigned_tech_ids || []).length
);
const hardGateBlocksSave = computed(() =>
  dispatchSettings.value.dispatch_block_save_no_tech && scheduledWithoutTech.value
);
const showSoftGateDialog = ref(false);
const softGateResolver = ref(null);
const softGateAcknowledged = ref(false);
function confirmSoftGate() {
  softGateAcknowledged.value = true;
  showSoftGateDialog.value = false;
  softGateResolver.value?.(true);
  softGateResolver.value = null;
}
function cancelSoftGate() {
  showSoftGateDialog.value = false;
  softGateResolver.value?.(false);
  softGateResolver.value = null;
}
// Dialog closed via the X / Esc — treat as cancel so submitForm doesn't hang.
function onSoftGateHide() {
  if (softGateResolver.value) {
    softGateResolver.value(false);
    softGateResolver.value = null;
  }
}

const customerOptions = computed(() =>
  customers.value.map((c) => ({ label: c.name, value: String(c.id) }))
);

// Sprint customer-multi-location (2026-05-21) — re-fetched whenever the
// picker's customer_id changes. Hidden when the chosen customer has 0
// or 1 location (the 90% case); a picker appears at 2+.
const customerLocations = ref([]);
const locationOptions = computed(() =>
  customerLocations.value.map((loc) => ({
    label: loc.label
      ? `${loc.label}${loc.address ? ` — ${loc.address}` : ""}`
      : (loc.address || "(unlabeled)"),
    value: String(loc.id),
  }))
);

async function fetchCustomerLocations(customerId) {
  if (!customerId) {
    customerLocations.value = [];
    return;
  }
  try {
    const res = await api.get(`/api/customers/${customerId}/locations`);
    customerLocations.value = Array.isArray(res) ? res : [];
  } catch (err) {
    // Don't silently present 5xx/auth failures as "this customer has 0
    // sites" — that hides the picker and lets the user submit with a
    // stale location_id from the prior customer. Log so devtools surfaces
    // the real error; empty the list as the safe fallback.
    console.warn("fetchCustomerLocations failed", err);
    customerLocations.value = [];
  }
}

// Transient flag set by openEditDialog/openCreateDialog so the customer_id
// watcher's "clear stale location_id" branch doesn't fire on dialog seed.
// Without this, opening job A then job B (without closing the dialog)
// trips prev=A.customer, next=B.customer, both truthy → wipes job B's
// seeded location_id one tick after openEditDialog set it. /audit
// 2026-05-21 caught the race; this guards it.
const _seedingLocation = ref(false);

watch(
  () => jobForm.value.customer_id,
  async (next, prev) => {
    if (next === prev) return;
    await fetchCustomerLocations(next);
    if (_seedingLocation.value) {
      _seedingLocation.value = false;
      return;
    }
    // User genuinely switched the customer mid-dialog. Wipe the stale
    // location_id — it belongs to the old customer and the backend will 400.
    if (prev) {
      jobForm.value.location_id = null;
    }
  },
);


const techOptions = computed(() =>
  technicians.value.map((t) => ({
    label: t.name || t.display_name || `${t.first_name || ""} ${t.last_name || ""}`.trim(),
    value: t.id,
  }))
);

// Lead-tech dropdown shown only when ≥2 techs are picked. Restricted to
// the currently-selected techs so you can't accidentally name a lead who
// isn't on the crew.
const leadTechOptions = computed(() => {
  const selected = new Set((jobForm.value.assigned_tech_ids || []).map(String));
  return techOptions.value.filter((opt) => selected.has(String(opt.value)));
});

// Stats computation — prefer server-provided global counts when available
// (covers ALL jobs, not just current page), else fall back to local count.
const statusCounts = computed(() => {
  // 2026-05-13: jobs lifecycle "Lead" was retired in favor of "Service Call".
  // Pre-migration rows still emit "Lead" — fold those into the Service Call
  // bucket so the UI matches the new taxonomy regardless of which rows the
  // tenant has migrated yet.
  const counts = { "Service Call": 0, Estimate: 0, Scheduled: 0, "In Progress": 0, Complete: 0, Cancelled: 0 };
  const server = globalStatusCounts.value || {};
  if (Object.keys(server).length > 0) {
    for (const k of Object.keys(counts)) {
      counts[k] = server[k] || 0;
    }
    counts["Service Call"] += server.Lead || 0;
    return counts;
  }
  jobs.value.forEach((j) => {
    const s = j.status || "";
    if (s === "Lead") counts["Service Call"]++;
    else if (s in counts) counts[s]++;
  });
  return counts;
});

const statCards = computed(() => [
  { key: "servicecalls", label: "Service Calls", filterKey: "Service Call", color: "#ffa726", count: statusCounts.value["Service Call"] || 0 },
  { key: "estimates", label: "Estimates", filterKey: "Estimate", color: "#4fc3f7", count: statusCounts.value.Estimate || 0 },
  { key: "scheduled", label: "Scheduled", filterKey: "Scheduled", color: "#ffa726", count: statusCounts.value.Scheduled || 0 },
  { key: "inprogress", label: "In Progress", filterKey: "In Progress", color: "#4fc3f7", count: statusCounts.value["In Progress"] || 0 },
  { key: "complete", label: "Complete", filterKey: "Complete", color: "#4caf50", count: statusCounts.value.Complete || 0 },
]);

const statusTabs = computed(() => [
  { key: "All", label: "All", count: globalTotal.value || jobs.value.length },
  { key: "Service Call", label: "Service Calls", count: statusCounts.value["Service Call"] || 0 },
  { key: "Estimate", label: "Estimates", count: statusCounts.value.Estimate || 0 },
  { key: "Scheduled", label: "Scheduled", count: statusCounts.value.Scheduled || 0 },
  { key: "In Progress", label: "In Progress", count: statusCounts.value["In Progress"] || 0 },
  { key: "Complete", label: "Completed", count: statusCounts.value.Complete || 0 },
]);

const filteredJobs = computed(() => {
  const query = searchQuery.value.trim().toLowerCase();
  return jobs.value.filter((job) => {
    let matchesStatus = true;
    if (activeStatus.value === "All") {
      matchesStatus = true;
    } else if (activeStatus.value === "Service Call") {
      // Pre-migration "Lead" rows still count as Service Call from a user POV.
      matchesStatus = job.status === "Service Call" || job.status === "Lead";
    } else {
      matchesStatus = job.status === activeStatus.value;
    }
    const matchesSearch =
      !query ||
      String(job.jobNumber || "").toLowerCase().includes(query) ||
      (job.customer || "").toLowerCase().includes(query) ||
      (job.title || "").toLowerCase().includes(query);
    return matchesStatus && matchesSearch;
  });
});

function setStatusFilter(key) {
  activeStatus.value = activeStatus.value === key ? "All" : key;
}

function formatDate(dateStr) {
  if (!dateStr) return "";
  try {
    const d = new Date(dateStr);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  } catch {
    return dateStr;
  }
}

function formatDateForApi(value) {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toISOString().split("T")[0];
}

function extractId(payload) {
  if (payload == null) return null;
  if (typeof payload === "number" || typeof payload === "string") return payload;
  return payload.id ?? payload.data?.id ?? payload.job?.id ?? null;
}

function openCreateDialog() {
  formMode.value = "create";
  _seedingLocation.value = true;
  jobForm.value = emptyForm();
  formError.value = "";
  showFormDialog.value = true;
}

function openJobDetail(job) {
  if (!job?.id) return;
  router.push({ name: "job-detail", params: { id: String(job.id) } });
}

async function openEditDialog(job) {
  formMode.value = "edit";
  formError.value = "";
  _seedingLocation.value = true;
  jobForm.value = {
    id: job.id,
    title: job.title || "",
    description: job.description || "",
    customer_id: job.customer_id ? String(job.customer_id) : null,
    job_type: job.job_type || "Service Call",
    priority: job.priority || "Normal",
    scheduled_at: job.scheduledDate ? new Date(job.scheduledDate) : null,
    scheduled_duration_hours: job.scheduled_duration_hours != null
      ? Number(job.scheduled_duration_hours) : null,
    location_id: job.location_id ? String(job.location_id) : null,
    assigned_tech_id: job.assigned_tech_id ?? null,
    assigned_tech_ids: [],
    lead_tech_id: null,
    notes: job.notes || "",
    status: job.status || "Estimate",
    new_customer: false,
    new_cust_name: "",
    new_cust_phone: "",
    new_cust_email: "",
    new_cust_address: "",
    appt_schedule: false,
    appt_date: null,
    appt_time: "",
    appt_notes: "",
  };
  showFormDialog.value = true;

  // Load existing crew so the MultiSelect/Lead controls reflect reality.
  // .catch — endpoint is permission-gated; on a 403 we fall back to the
  // legacy single-tech assigned_to that came in via the row.
  try {
    const assignments = await api.get(`/api/jobs/${job.id}/assignments`);
    if (Array.isArray(assignments) && assignments.length) {
      jobForm.value.assigned_tech_ids = assignments.map((a) => String(a.tech_id));
      const lead = assignments.find((a) => a.is_lead);
      jobForm.value.lead_tech_id = lead ? String(lead.tech_id) : null;
    } else if (job.assigned_tech_id || job.assigned_to) {
      jobForm.value.assigned_tech_ids = [String(job.assigned_tech_id || job.assigned_to)];
    }
  } catch {
    if (job.assigned_tech_id || job.assigned_to) {
      jobForm.value.assigned_tech_ids = [String(job.assigned_tech_id || job.assigned_to)];
    }
  }
}

function handleNewCustomerToggle(value) {
  if (value) {
    jobForm.value.customer_id = null;
    return;
  }
  jobForm.value.new_cust_name = "";
  jobForm.value.new_cust_phone = "";
  jobForm.value.new_cust_email = "";
  jobForm.value.new_cust_address = "";
}

function promptDelete(job) {
  deleteTarget.value = job;
  showDeleteDialog.value = true;
}

async function submitForm() {
  formError.value = "";
  // Each save re-evaluates the soft gate from scratch — acknowledging once
  // shouldn't grant a permanent pass for the rest of the dialog session.
  softGateAcknowledged.value = false;
  const title = jobForm.value.title?.trim();
  if (!title) {
    formError.value = "Title is required.";
    return;
  }

  if (!jobForm.value.customer_id && !jobForm.value.new_customer) {
    formError.value = "Please select or create a customer.";
    return;
  }

  if (jobForm.value.new_customer && !jobForm.value.new_cust_name?.trim()) {
    formError.value = "New customer name is required.";
    return;
  }

  if (jobForm.value.appt_schedule && !jobForm.value.appt_date) {
    formError.value = "Appointment date is required.";
    return;
  }

  // Soft gate: scheduled job with no tech → confirm before continuing.
  // Hard gate is server-side (422) and also disables the Save button via
  // hardGateBlocksSave, so we don't need to re-check it here. The dialog
  // resolves via cancelSoftGate/confirmSoftGate, which await this same
  // promise so submitForm continues only if the user clicked Save anyway.
  if (
    dispatchSettings.value.dispatch_warn_save_no_tech &&
    !dispatchSettings.value.dispatch_block_save_no_tech &&
    scheduledWithoutTech.value &&
    !softGateAcknowledged.value
  ) {
    const proceed = await new Promise((resolve) => {
      softGateResolver.value = resolve;
      showSoftGateDialog.value = true;
    });
    if (!proceed) return;
  }

  isSaving.value = true;
  try {
    let customerId = jobForm.value.customer_id;

    if (jobForm.value.new_customer) {
      const newCustomerPayload = {
        name: jobForm.value.new_cust_name.trim(),
        phone: jobForm.value.new_cust_phone?.trim() || null,
        email: jobForm.value.new_cust_email?.trim() || null,
        address: jobForm.value.new_cust_address?.trim() || null,
      };
      const createdCustomer = await api.post("/api/customers", newCustomerPayload);
      customerId = extractId(createdCustomer);
      if (!customerId) {
        throw new Error("Customer creation did not return an ID.");
      }
    }

    const techIds = (jobForm.value.assigned_tech_ids || []).filter(Boolean).map(String);
    const leadTechId = techIds.length > 1 ? (jobForm.value.lead_tech_id || null) : null;
    const payload = {
      title,
      description: jobForm.value.description || "",
      customer_id: customerId,
      job_type: jobForm.value.job_type,
      priority: jobForm.value.priority,
      scheduled_at: jobForm.value.scheduled_at ? jobForm.value.scheduled_at.toISOString() : null,
      // Sprint dispatch-capacity — pass through as Number so the API
      // gets a NUMERIC, not a string. Blank or non-numeric input = null
      // (cleared). 0 round-trips as 0 — external writers (importers,
      // API clients) may store 0 deliberately; coercing it to null
      // would clobber their data on the first JobsView save. /audit
      // 2026-05-21 finding 2.
      scheduled_duration_hours: (() => {
        const raw = jobForm.value.scheduled_duration_hours;
        if (raw == null || raw === "") return null;
        const n = Number(raw);
        return Number.isFinite(n) ? n : null;
      })(),
      // Sprint customer-multi-location — picker only renders at 2+
      // locations, so single-site customers naturally send null and use
      // the customer's primary address.
      location_id: jobForm.value.location_id || null,
      assigned_tech_ids: techIds,
      lead_tech_id: leadTechId,
      // Legacy fields kept for any older read paths that still inspect them.
      assigned_tech_id: techIds[0] || null,
      notes: jobForm.value.notes || "",
    };

    if (isEditMode.value) {
      payload.lifecycle_stage = jobForm.value.status;
      payload.status = jobForm.value.status;
      await api.patch(`/api/jobs/${jobForm.value.id}`, payload);
      toast.add({ severity: "success", summary: "Job Updated", detail: "Job saved successfully.", life: 3000 });
    } else {
      const createdJob = await api.post("/api/jobs", payload);
      const jobId = extractId(createdJob);

      if (jobForm.value.appt_schedule) {
        if (!jobId) {
          throw new Error("Job creation did not return an ID for the appointment.");
        }
        const appointmentPayload = {
          job_id: jobId,
          date: formatDateForApi(jobForm.value.appt_date),
          time: jobForm.value.appt_time?.trim() || null,
          notes: jobForm.value.appt_notes || "",
        };
        await api.post("/api/appointments", appointmentPayload);
      }

      toast.add({ severity: "success", summary: "Job Created", detail: "New job created successfully.", life: 3000 });
    }

    showFormDialog.value = false;
    await fetchJobs();
  } catch (error) {
    formError.value = error?.message || "Failed to save job.";
    toast.add({ severity: "error", summary: "Error", detail: formError.value, life: 5000 });
  } finally {
    isSaving.value = false;
  }
}

async function confirmDelete() {
  if (!deleteTarget.value?.id) return;
  isDeleting.value = true;
  try {
    await api.del(`/api/jobs/${deleteTarget.value.id}`);
    toast.add({ severity: "success", summary: "Job Deleted", detail: `Job ${deleteTarget.value.jobNumber} deleted.`, life: 3000 });
    showDeleteDialog.value = false;
    showFormDialog.value = false;
    deleteTarget.value = null;
    await fetchJobs();
  } catch (error) {
    toast.add({ severity: "error", summary: "Error", detail: error?.message || "Failed to delete job.", life: 5000 });
  } finally {
    isDeleting.value = false;
  }
}

async function fetchJobs() {
  isLoading.value = true;
  try {
    const [jobsResult, customersResult, techResult] = await Promise.all([
      // Server defaults to page_size=50; bump to 1000 (server cap) so the
      // /jobs view shows all 180 GDX jobs in one page instead of the first
      // 50 — the paginator stayed local to those 50 even though total=180.
      api.get("/api/jobs?per_page=1000"),
      api.get("/api/customers?per_page=1000").catch(() => []),
      api.get("/api/technicians").catch(() => ({ data: [] })),
    ]);

    const rawJobs = Array.isArray(jobsResult) ? jobsResult : jobsResult?.items || jobsResult?.data || [];
    const rawCustomers = Array.isArray(customersResult) ? customersResult : customersResult?.items || customersResult?.data || [];
    const rawTechs = Array.isArray(techResult) ? techResult : techResult?.items || techResult?.data || [];

    // Capture server-side aggregates when returned (preferred over local page count).
    if (!Array.isArray(jobsResult)) {
      globalTotal.value = Number(jobsResult?.total || 0);
      globalStatusCounts.value = jobsResult?.status_counts || {};
    } else {
      globalTotal.value = 0;
      globalStatusCounts.value = {};
    }

    customers.value = rawCustomers;
    technicians.value = rawTechs;

    const customerMap = Object.fromEntries(rawCustomers.map((c) => [String(c.id), c.name]));
    const techMap = Object.fromEntries(rawTechs.map((t) => [String(t.id), t.name || t.display_name || `${t.first_name || ""} ${t.last_name || ""}`.trim()]));

    // 2026-04-29: replace QB-import boilerplate titles with the job_type so
    // the list reads "Service Call" / "Install" rather than 163 rows of
    // "QuickBooks Import — <name>" — same fix as DashboardView.jobDisplayTitle.
    // Match both "QuickBooks Import — <name>" (recent imports) and the older
    // "QB Import" format. Either way → fall back to job_type.
    const _isQbBoilerplate = (t) =>
      /^(quickbooks|qb)\s+import(\s*[—-].*)?$/i.test((t || "").trim());

    jobs.value = rawJobs.map((job) => {
      const rawTitle = (job.title || "").trim();
      const display = !rawTitle || _isQbBoilerplate(rawTitle) ? job.job_type || "Service Call" : rawTitle;
      return {
        ...job,
        jobNumber: job.job_number || job.jobNumber || `JOB-${String(job.id).substring(0, 8).toUpperCase()}`,
        title: display,
        customer: job.customer?.name || job.customer_name || customerMap[String(job.customer_id)] || "",
        customer_id: job.customer_id,
        status: job.lifecycle_stage || job.status || "Estimate",
        scheduledDate: job.scheduled_at || job.scheduledDate || "",
        tech: job.tech_name || job.assigned_tech?.name || job.tech || techMap[String(job.assigned_to || job.assigned_tech_id)] || "",
        assigned_tech_id: job.assigned_tech_id,
        priority: job.priority || "Normal",
        job_type: job.job_type || "Service Call",
      };
    });
  } catch (error) {
    toast.add({ severity: "error", summary: "Load Error", detail: error?.message || "Failed to load jobs.", life: 5000 });
  } finally {
    isLoading.value = false;
    hasLoadedOnce.value = true;
  }
}

async function loadDispatchSettings() {
  try {
    const f = await api.get("/api/dispatch-settings");
    if (f) dispatchSettings.value = { ...dispatchSettings.value, ...f };
  } catch (_e) {
    // module not deployed yet — gates stay off (current behavior)
  }
}

onMounted(async () => {
  await fetchJobs();
  loadDispatchSettings();
  // Dashboard "+ New Job" passes ?new=1 to auto-open the create modal so the
  // user gets the form in one click instead of landing here and clicking again.
  // Customer detail "+ New Job" additionally passes ?customer_id=<id> to
  // pre-select the customer in the dialog.
  if (route.query.new === "1") {
    openCreateDialog();
    if (route.query.customer_id) {
      jobForm.value.customer_id = String(route.query.customer_id);
    }
    // Strip the query without triggering a Vue Router navigation —
    // App.vue keys <ErrorBoundary> on $route.fullPath, so router.replace
    // here would remount JobsView and close the dialog we just opened.
    window.history.replaceState({}, "", route.path);
    return;
  }
  // JobDetailView's Edit button routes here with ?edit=<id> so the
  // already-built dialog handles the form. Without this hook the
  // landing was silent and the user saw the jobs list, not their job.
  // 2026-05-21 — Doug "now when in a job and i click edit it returns
  // me to the /jobs page."
  const editId = route.query.edit ? String(route.query.edit) : null;
  if (editId) {
    const target = (jobs.value || []).find((j) => String(j.id) === editId);
    if (target) {
      openEditDialog(target);
    } else {
      // Fall back to a direct fetch if the row wasn't on the page
      // (uncommon; jobs.value is the paginated list which the user just
      // came from — but a deep-link from a stale tab could miss it).
      try {
        const j = await api.get(`/api/jobs/${editId}`);
        if (j) openEditDialog(j);
      } catch (_e) { /* api composable surfaces the toast */ }
    }
    window.history.replaceState({}, "", route.path);
  }
});

// Sprint dispatch-capacity (2026-05-21) — expose the surface the
// vitest specs reach via wrapper.vm. Without defineExpose, <script
// setup> only auto-exposes under dev-mode escape hatches; a strict
// prod build seals them and the test file would go silent. /audit
// 2026-05-21 finding 3.
defineExpose({
  jobForm,
  openCreateDialog,
  openEditDialog,
  submitForm,
});
</script>

<style scoped>
.muted-cell { color: var(--p-text-muted-color); }

.jobs-view {
  max-width: 1400px;
}

.search-input {
  width: 280px;
}

.stats-bar {
  display: flex;
  gap: 12px;
  margin: 1rem 0;
  flex-wrap: wrap;
}

.stat-card {
  flex: 1;
  min-width: 120px;
  padding: 12px 16px;
  border: 1px solid var(--surface-border, #dee2e6);
  border-radius: 8px;
  cursor: pointer;
  text-align: center;
  transition: border-color 0.15s, transform 0.1s;
}

.stat-card:hover {
  transform: translateY(-2px);
  border-color: var(--p-primary-color);
}

.stat-card.highlighted {
  border-color: var(--p-primary-color);
  background: rgba(79, 195, 247, 0.08);
}

.stat-val {
  font-size: 1.5rem;
  font-weight: 700;
}

.stat-lbl {
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
  margin-top: 2px;
}

.status-tabs {
  display: flex;
  gap: 0.5rem;
  margin: 0.5rem 0 1rem;
  flex-wrap: wrap;
}

.jobs-table {
  cursor: pointer;
}

.job-number {
  color: var(--p-primary-color);
  font-weight: 600;
}

.priority-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.priority-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}

.priority-low {
  background: #667085;
}

.priority-normal {
  background: #4fc3f7;
}

.priority-high {
  background: #ffa726;
}

.priority-urgent {
  background: #e94560;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  margin: 2rem 0;
}

.dialog-form {
  display: grid;
  gap: 0.75rem;
}

.form-field {
  display: grid;
  gap: 0.25rem;
}

.toggle-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.toggle-label {
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}

.new-client-section {
  border: 1px solid var(--surface-border, #dee2e6);
  border-radius: 8px;
  padding: 0.75rem;
  background: var(--surface-card, #ffffff);
  margin-bottom: 0.5rem;
}

.new-client-section .form-row {
  gap: 0.75rem;
}

.appointment-toggle-row {
  margin-top: 0.5rem;
}

.appointment-section {
  border: 1px solid var(--surface-border, #dee2e6);
  border-radius: 8px;
  padding: 0.75rem;
  background: var(--surface-ground, #f8f9fb);
  display: grid;
  gap: 0.75rem;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.75rem;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 0.75rem;
}

.status-transition-bar {
  display: flex;
  gap: 0.35rem;
  flex-wrap: wrap;
}

.inline-error {
  color: #b42318;
  margin: 0.5rem 0;
  font-size: 0.9rem;
}

.empty-message {
  text-align: center;
  padding: 2rem;
  color: var(--p-text-muted-color);
}

.w-full {
  width: 100%;
}

.lead-row {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  margin-top: 0.5rem;
  color: var(--p-text-muted-color);
}

.lead-select {
  min-width: 14rem;
}
</style>
