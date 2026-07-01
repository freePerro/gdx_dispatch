<template>
    <section class="techs-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Technicians</h2>
        </template>
        <template #end>
          <Button label="+ New Technician" icon="pi pi-plus" @click="openCreate" data-testid="new-tech-btn" />
        </template>
      </Toolbar>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll" v-if="!loading" :value="filtered" paginator :rows="20" striped-rows
        @row-click="openEdit($event.data)" >
        <template #empty>
          <div class="empty-state">
            <i class="pi pi-user" style="font-size:3rem; color:#64748b;"></i>
            <h3>No Technicians Yet</h3>
            <p>Add technicians to assign jobs and track performance.</p>
            <Button label="+ Add First Technician" @click="openCreate" />
          </div>
        </template>
        <Column field="name" header="Name" sortable>
          <template #body="{ data }">
            <div class="tech-name-cell">
              <div class="tech-avatar" :style="{ background: avatarColor(data.name) }">
                {{ initials(data.name) }}
              </div>
              <div>
                <div class="tech-name">{{ data.name || data.user_id }}</div>
                <div class="tech-email muted">{{ data.email || '' }}</div>
              </div>
            </div>
          </template>
        </Column>
        <Column field="phone" header="Phone" />
        <Column field="skills" header="Skills">
          <template #body="{ data }">
            <div class="skills-list">
              <Tag v-for="skill in (data.skills || []).slice(0, 3)" :key="skill"
                :value="skill" severity="info" style="margin-right: 0.25rem" />
              <span v-if="(data.skills || []).length > 3" class="muted">+{{ data.skills.length - 3 }}</span>
            </div>
          </template>
        </Column>
        <Column field="hourly_rate" header="Rate" sortable style="width:120px">
          <template #body="{ data }">${{ Number(data.hourly_rate || 0).toFixed(2) }}/hr</template>
        </Column>
        <Column field="active" header="Status" style="width:100px">
          <template #body="{ data }">
            <Tag :value="data.active ? 'Active' : 'Inactive'" :severity="data.active ? 'success' : 'secondary'" />
          </template>
        </Column>
        <Column header="Actions" style="width:100px">
          <template #body="{ data }">
            <Button v-tooltip="'Edit'" icon="pi pi-pencil" aria-label="Edit" text size="small" @click.stop="openEdit(data)" />
            <Button v-tooltip="'Delete'" icon="pi pi-trash" aria-label="Delete" severity="danger" text size="small" @click.stop="confirmDelete(data)" />
          </template>
        </Column>
      </DataTable>

      <Dialog v-model:visible="showDialog" :header="editing ? 'Edit Technician' : 'New Technician'"
        modal :style="{width: '550px'}">
        <div class="form-grid">
          <div class="form-field">
            <label>Name *</label>
            <InputText v-model="form.name" placeholder="Full name" class="w-full" />
          </div>
          <div class="form-field">
            <label>Email</label>
            <InputText v-model="form.email" type="email" placeholder="tech@company.com" class="w-full" />
          </div>
          <div class="form-field">
            <label>Phone</label>
            <InputText v-model="form.phone" class="w-full" />
          </div>
          <div class="form-field">
            <label>Hourly Rate</label>
            <InputNumber v-model="form.hourly_rate" mode="currency" currency="USD" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Skills</label>
            <Chips v-model="form.skills" placeholder="Add skill and press Enter" class="w-full" />
            <small class="muted">e.g., Torsion spring, Commercial doors, Smart openers, Electrical</small>
          </div>
          <div class="form-field full-width">
            <label>Certifications</label>
            <Chips v-model="form.certifications" placeholder="Add cert and press Enter" class="w-full" />
          </div>
          <div class="form-field">
            <label>Work Hours Start</label>
            <InputText v-model="form.work_start" placeholder="08:00" class="w-full" />
          </div>
          <div class="form-field">
            <label>Work Hours End</label>
            <InputText v-model="form.work_end" placeholder="17:00" class="w-full" />
          </div>
          <div class="form-field full-width">
            <div class="checkbox-row">
              <Checkbox v-model="form.active" :binary="true" inputId="tech-active" />
              <label for="tech-active">Active (dispatchable)</label>
            </div>
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showDialog = false" />
          <Button :label="editing ? 'Save' : 'Create'" icon="pi pi-check" @click="saveTech" :loading="saving" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Checkbox from "primevue/checkbox";
import Chips from "primevue/chips";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import Toolbar from "primevue/toolbar";
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();

const techs = ref([]);
const loading = ref(true);
const showDialog = ref(false);
const editing = ref(null);
const saving = ref(false);

const emptyForm = () => ({
  user_id: "", name: "", email: "", phone: "",
  hourly_rate: 0, skills: [], certifications: [],
  work_start: "08:00", work_end: "17:00", active: true,
});
const form = ref(emptyForm());

const filtered = computed(() => techs.value);

function initials(name) {
  if (!name) return "?";
  return name.split(" ").map((w) => w[0]).join("").toUpperCase().slice(0, 2);
}

function avatarColor(name) {
  if (!name) return "#64748b";
  const colors = ["#0ea5e9", "#10b981", "#f59e0b", "#8b5cf6", "#ec4899", "#ef4444"];
  let hash = 0;
  for (let i = 0; i < name.length; i++) hash = name.charCodeAt(i) + ((hash << 5) - hash);
  return colors[Math.abs(hash) % colors.length];
}

async function loadTechs() {
  loading.value = true;
  try {
    const data = await api.get("/api/technicians");
    techs.value = Array.isArray(data) ? data : data?.items || [];
  } catch (err) {
    console.error('load_technicians_failed', err?.message || err);
    techs.value = [];
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  editing.value = null;
  form.value = emptyForm();
  showDialog.value = true;
}

function openEdit(tech) {
  editing.value = tech;
  form.value = {
    ...emptyForm(),
    ...tech,
    skills: Array.isArray(tech.skills) ? tech.skills : [],
    certifications: Array.isArray(tech.certifications) ? tech.certifications : [],
  };
  showDialog.value = true;
}

async function saveTech() {
  if (!form.value.name?.trim()) return;
  saving.value = true;
  try {
    const payload = {
      user_id: form.value.user_id || form.value.email || form.value.name.toLowerCase().replace(/\s+/g, "."),
      name: form.value.name,
      email: form.value.email,
      phone: form.value.phone,
      skills: form.value.skills,
      certifications: form.value.certifications,
      hourly_rate: form.value.hourly_rate,
      work_start: form.value.work_start,
      work_end: form.value.work_end,
      active: form.value.active,
    };
    if (editing.value) {
      await api.patch(`/api/technicians/${editing.value.id}`, payload);
    } else {
      await api.post("/api/technicians", payload);
    }
    showDialog.value = false;
    await loadTechs();
  } catch (err) {
    console.error('save_technician_failed', err?.message || err);
  } finally {
    saving.value = false;
  }
}

async function confirmDelete(tech) {
  if (!(await confirmAsync({ header: 'Confirm', message: `Remove ${tech.name || tech.user_id}?` }))) return;
  await api.del(`/api/technicians/${tech.id}`);
  await loadTechs();
}

onMounted(loadTechs);
</script>

<style scoped>
.page-title { margin: 0; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
.form-field { display: flex; flex-direction: column; gap: 0.3rem; }
.form-field label { font-size: 0.82rem; font-weight: 600; color: var(--p-text-muted-color); }
.full-width { grid-column: 1 / -1; }
.w-full { width: 100%; }
.checkbox-row { display: flex; align-items: center; gap: 0.5rem; }
.muted { color: var(--p-text-muted-color); font-size: 0.78rem; }

.tech-name-cell { display: flex; align-items: center; gap: 0.75rem; }
.tech-avatar {
  width: 2.5rem; height: 2.5rem; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  color: #fff; font-weight: 700; font-size: 0.85rem;
}
.tech-name { font-weight: 600; }
.tech-email { font-size: 0.78rem; }
.skills-list { display: flex; align-items: center; flex-wrap: wrap; }
.clickable-row { cursor: pointer; }
.empty-state { text-align: center; padding: 3rem; color: var(--p-text-muted-color); }
.empty-state h3 { margin: 1rem 0 0.5rem; color: var(--text-color); }
.spinner-wrap { display: flex; justify-content: center; padding: 3rem; }
</style>
