<template>
    <section class="tags-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Tags</h2>
        </template>
        <template #end>
          <Button label="+ New Tag" icon="pi pi-plus" severity="primary" @click="openCreate" />
        </template>
      </Toolbar>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <div v-else>
        <div v-if="tags.length" class="tags-grid">
          <article
            v-for="tag in tags"
            :key="tag.id"
            class="tag-card"
            :class="{ selected: selectedTag?.id === tag.id }"
            @click="selectTag(tag)"
          >
            <div class="tag-preview" :style="{ backgroundColor: tag.color || '#6366f1' }">
              <Tag :value="tag.name" severity="info" class="tag-chip" />
            </div>
            <p class="tag-description">{{ tag.description || 'No description yet.' }}</p>
            <div class="tag-meta">
              <span>Created {{ tag.created_at?.split('T')[0] || '—' }}</span>
              <div class="tag-actions">
                <Button v-tooltip="'Edit'" icon="pi pi-pencil" aria-label="Edit" class="p-button-text" text size="small" @click.stop="openEdit(tag)" />
                <Button
                  v-tooltip="'Delete'"
                  icon="pi pi-trash" aria-label="Delete"
                  class="p-button-text"
                  severity="danger"
                  text
                  size="small"
                  @click.stop="confirmDelete(tag)"
                />
              </div>
            </div>
          </article>
        </div>
        <div v-else class="empty-state">
          <i class="pi pi-tags" aria-hidden="true"></i>
          <h3>No tags yet</h3>
          <p>Tags help you categorize jobs and customers for reporting or automation.</p>
          <Button label="Create first tag" icon="pi pi-plus" @click="openCreate" />
        </div>

        <div v-if="selectedTag" class="assignment-panel">
          <header>
            <div class="header-labels">
              <strong>{{ selectedTag.name }}</strong>
              <Tag :value="selectedTag.color" :severity="'info'" class="color-tag" />
            </div>
            <p class="muted">{{ selectedTag.description || 'No description yet.' }}</p>
          </header>

          <div class="assignment-grid">
            <div class="assignment-card">
              <label class="form-label">Assign to Job</label>
              <Select
                v-model="jobTarget"
                :options="jobOptions"
                placeholder="Select job"
                showClear
                optionLabel="label"
                optionValue="value"
                class="w-full"
              />
              <div class="assignment-actions">
                <Button
                  label="Assign"
                  icon="pi pi-plus"
                  :disabled="!canAssignJob"
                  :loading="jobBusy"
                  @click="assignToJob"
                />
                <Button
                  label="Remove"
                  severity="danger"
                  icon="pi pi-minus"
                  :disabled="!jobAssignment.assigned || jobBusy"
                  @click="removeFromJob"
                />
              </div>
              <div class="assignment-status">
                <ProgressSpinner v-if="jobAssignment.checking" style="width: 1.5rem; height: 1.5rem" />
                <Tag
                  v-else
                  :value="jobAssignment.assigned ? 'Assigned' : 'Not assigned'"
                  :severity="jobAssignment.assigned ? 'success' : 'secondary'"
                />
              </div>
            </div>

            <div class="assignment-card">
              <label class="form-label">Assign to Customer</label>
              <Select
                v-model="customerTarget"
                :options="customerOptions"
                placeholder="Select customer"
                showClear
                optionLabel="label"
                optionValue="value"
                class="w-full"
              />
              <div class="assignment-actions">
                <Button
                  label="Assign"
                  icon="pi pi-plus"
                  :disabled="!canAssignCustomer"
                  :loading="customerBusy"
                  @click="assignToCustomer"
                />
                <Button
                  label="Remove"
                  severity="danger"
                  icon="pi pi-minus"
                  :disabled="!customerAssignment.assigned || customerBusy"
                  @click="removeFromCustomer"
                />
              </div>
              <div class="assignment-status">
                <ProgressSpinner v-if="customerAssignment.checking" style="width: 1.5rem; height: 1.5rem" />
                <Tag
                  v-else
                  :value="customerAssignment.assigned ? 'Assigned' : 'Not assigned'"
                  :severity="customerAssignment.assigned ? 'success' : 'secondary'"
                />
              </div>
            </div>
          </div>
        </div>
      </div>

      <Dialog
        v-model:visible="showDialog"
        :header="editingTag ? `Edit ${editingTag.name}` : 'New Tag'"
        :style="{ width: '480px' }"
        modal
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Name *</label>
            <InputText v-model="form.name" class="w-full" maxlength="80" />
          </div>
          <div class="form-field">
            <label>Color</label>
            <ColorPicker v-model="form.color" inline class="color-picker" />
          </div>
          <div class="form-field full-width">
            <label>Description</label>
            <Textarea v-model="form.description" rows="3" class="w-full" maxlength="500" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="showDialog = false" />
          <Button
            :label="editingTag ? 'Save' : 'Create'"
            icon="pi pi-check"
            :loading="saving"
            @click="saveTag"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Select from "primevue/select";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import ColorPicker from "primevue/colorpicker";
import Textarea from "primevue/textarea";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import Toolbar from "primevue/toolbar";
import { useToast } from "primevue/usetoast";
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();
const toast = useToast();
const tags = ref([]);
const jobs = ref([]);
const customers = ref([]);
const loading = ref(true);
const showDialog = ref(false);
const editingTag = ref(null);
const saving = ref(false);
const selectedTag = ref(null);
const jobTarget = ref(null);
const customerTarget = ref(null);
const jobBusy = ref(false);
const customerBusy = ref(false);
const jobAssignment = reactive({ assigned: false, checking: false });
const customerAssignment = reactive({ assigned: false, checking: false });

const emptyForm = () => ({ name: "", color: "#6366f1", description: "" });
const form = reactive(emptyForm());

const jobOptions = computed(() =>
  jobs.value.map((job) => ({ label: job.label, value: job.id }))
);

const customerOptions = computed(() =>
  customers.value.map((customer) => ({ label: customer.label, value: customer.id }))
);

function selectTag(tag) {
  selectedTag.value = tag;
}

function resetForm() {
  Object.assign(form, emptyForm());
}

function openCreate() {
  editingTag.value = null;
  resetForm();
  showDialog.value = true;
}

function openEdit(tag) {
  editingTag.value = tag;
  Object.assign(form, {
    name: tag.name,
    color: tag.color,
    description: tag.description || "",
  });
  showDialog.value = true;
}

async function loadTags() {
  loading.value = true;
  try {
    const data = await api.get("/api/tags");
    tags.value = Array.isArray(data) ? data : data?.items || [];
    if (selectedTag.value) {
      selectedTag.value = tags.value.find((t) => t.id === selectedTag.value?.id) || selectedTag.value;
    } else if (tags.value.length) {
      selectedTag.value = tags.value[0];
    }
  } finally {
    loading.value = false;
  }
}

async function loadJobs() {
  try {
    const data = await api.get("/api/jobs?page_size=200");
    const list = Array.isArray(data) ? data : data?.items || [];
    jobs.value = list.map((job) => ({
      id: job.id,
      label: `${job.job_number || job.id?.slice(0, 8)} — ${job.customer_name || job.title || "Job"}`,
    }));
  } catch {
    jobs.value = [];
  }
}

async function loadCustomers() {
  try {
    const data = await api.get("/api/customers?per_page=500");
    const list = Array.isArray(data) ? data : data?.items || [];
    customers.value = list.map((cust) => ({
      id: cust.id,
      label: cust.name || cust.company_name || cust.full_name || cust.id?.slice(0, 8),
    }));
  } catch {
    customers.value = [];
  }
}

async function saveTag() {
  if (!form.name.trim()) {
    toast.add({ severity: "warn", summary: "Name required", detail: "Please provide a name for the tag." });
    return;
  }
  saving.value = true;
  try {
    if (editingTag.value) {
      await api.patch(`/api/tags/${editingTag.value.id}`, form, { successMessage: "Tag updated" });
    } else {
      await api.post("/api/tags", form, { successMessage: "Tag created" });
    }
    showDialog.value = false;
    await loadTags();
  } finally {
    saving.value = false;
  }
}

async function confirmDelete(tag) {
  if (!(await confirmAsync({ header: 'Confirm', message: `Delete ${tag.name}?` }))) return;
  await api.del(`/api/tags/${tag.id}`, { successMessage: "Tag deleted" });
  if (selectedTag.value?.id === tag.id) {
    selectedTag.value = null;
  }
  await loadTags();
}

async function refreshJobAssignment() {
  jobAssignment.checking = true;
  jobAssignment.assigned = false;
  if (!selectedTag.value || !jobTarget.value) {
    jobAssignment.checking = false;
    return;
  }
  try {
    const data = await api.get(`/api/jobs/${jobTarget.value}/tags`);
    jobAssignment.assigned = Array.isArray(data)
      ? data.some((tag) => tag.id === selectedTag.value?.id)
      : false;
  } finally {
    jobAssignment.checking = false;
  }
}

async function refreshCustomerAssignment() {
  customerAssignment.checking = true;
  customerAssignment.assigned = false;
  if (!selectedTag.value || !customerTarget.value) {
    customerAssignment.checking = false;
    return;
  }
  try {
    const data = await api.get(`/api/customers/${customerTarget.value}/tags`);
    customerAssignment.assigned = Array.isArray(data)
      ? data.some((tag) => tag.id === selectedTag.value?.id)
      : false;
  } finally {
    customerAssignment.checking = false;
  }
}

const canAssignJob = computed(() => jobTarget.value && selectedTag.value && !jobBusy.value);
const canAssignCustomer = computed(() => customerTarget.value && selectedTag.value && !customerBusy.value);

async function assignToJob() {
  if (!canAssignJob.value) return;
  jobBusy.value = true;
  try {
    await api.post(
      `/api/jobs/${jobTarget.value}/tags`,
      { tag_id: selectedTag.value.id },
      { successMessage: "Tag assigned to job" }
    );
    await refreshJobAssignment();
  } finally {
    jobBusy.value = false;
  }
}

async function removeFromJob() {
  if (!selectedTag.value || !jobTarget.value) return;
  jobBusy.value = true;
  try {
    await api.del(`/api/jobs/${jobTarget.value}/tags/${selectedTag.value.id}`, {
      successMessage: "Tag removed from job",
    });
    await refreshJobAssignment();
  } finally {
    jobBusy.value = false;
  }
}

async function assignToCustomer() {
  if (!canAssignCustomer.value) return;
  customerBusy.value = true;
  try {
    await api.post(
      `/api/customers/${customerTarget.value}/tags`,
      { tag_id: selectedTag.value.id },
      { successMessage: "Tag assigned to customer" }
    );
    await refreshCustomerAssignment();
  } finally {
    customerBusy.value = false;
  }
}

async function removeFromCustomer() {
  if (!selectedTag.value || !customerTarget.value) return;
  customerBusy.value = true;
  try {
    await api.del(`/api/customers/${customerTarget.value}/tags/${selectedTag.value.id}`, {
      successMessage: "Tag removed from customer",
    });
    await refreshCustomerAssignment();
  } finally {
    customerBusy.value = false;
  }
}

watch([selectedTag, jobTarget], refreshJobAssignment);
watch([selectedTag, customerTarget], refreshCustomerAssignment);

onMounted(async () => {
  await Promise.all([loadJobs(), loadCustomers(), loadTags()]);
});
</script>

<style scoped>
.tags-grid {
  display: grid;
  gap: 1rem;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  margin-bottom: 2rem;
}
.tag-card {
  border: 1px solid var(--surface-border);
  border-radius: 0.5rem;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  cursor: pointer;
  transition: border-color 0.2s ease;
}
.tag-card.selected {
  border-color: var(--p-primary-color);
}
.tag-preview {
  border-radius: 0.75rem;
  min-height: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.tag-description {
  min-height: 2.4rem;
  font-size: 0.9rem;
  color: var(--p-text-muted-color);
}
.tag-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}
.tag-actions {
  display: flex;
  gap: 0.25rem;
}
.empty-state {
  text-align: center;
  padding: 3rem 1rem;
  color: var(--p-text-muted-color);
}
.empty-state i {
  font-size: 2.5rem;
  margin-bottom: 0.5rem;
}
.assignment-panel {
  margin-top: 2rem;
  border-top: 1px solid var(--surface-border);
  padding-top: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.assignment-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 1rem;
}
.assignment-card {
  border: 1px solid var(--surface-border);
  border-radius: 0.5rem;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
}
.assignment-actions {
  display: flex;
  gap: 0.5rem;
}
.assignment-status {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.form-field.full-width {
  grid-column: 1 / -1;
}
.form-label {
  font-weight: 600;
  color: var(--p-text-muted-color);
}
.tags-view .page-title {
  margin: 0;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem 0;
}
.color-picker {
  width: 100%;
}
.header-labels {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}
.color-tag {
  padding: 0.25rem 0.75rem;
}
</style>
