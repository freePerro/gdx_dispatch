<template>
    <section class="photos-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Photos</h2>
        </template>
        <template #end>
          <Button label="+ Upload Photo" icon="pi pi-upload" severity="primary" @click="openUploadDialog" />
        </template>
      </Toolbar>

      <div class="filters">
        <Select
          v-model="selectedJob"
          :options="jobSelectOptions"
          optionLabel="label"
          optionValue="value"
          placeholder="Recent photos"
          showClear
          class="job-select"
        />
        <div class="kind-filters">
          <Button
            v-for="kind in kindFilters"
            :key="kind.value"
            :label="kind.label"
            :class="{ active: kindFilter === kind.value }"
            class="kind-chip"
            :severity="kindFilter === kind.value ? 'primary' : 'secondary'"
            size="small"
            text
            @click="kindFilter = kind.value"
          />
        </div>
      </div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <div v-else>
        <div v-if="filteredPhotos.length" class="photo-grid">
          <article v-for="photo in filteredPhotos" :key="photo.id" class="photo-card">
            <div class="photo-wrapper" @click="openLightbox(photo)">
              <img :src="photo.url" :alt="photo.filename || 'Photo'" />
              <Tag :value="photo.kind" class="photo-kind" />
            </div>
            <div class="photo-meta">
              <div class="meta-title">{{ photo.filename || 'Untitled' }}</div>
              <div class="meta-sub">
                <span>{{ photo.uploaded_by || 'Unknown' }}</span>
                <span>•</span>
                <span>{{ photo.uploaded_at?.split('T')[0] || 'Unknown date' }}</span>
              </div>
              <p v-if="photo.caption" class="photo-caption">{{ photo.caption }}</p>
            </div>
          </article>
        </div>
        <div v-else class="empty-state">
          <i class="pi pi-image" aria-hidden="true"></i>
          <h3>No photos yet</h3>
          <p>Upload before/during/after photos to build a project gallery.</p>
        </div>
      </div>

      <Dialog v-model:visible="uploadDialog" header="Upload Photo" :style="{ width: '520px' }" modal>
        <div class="form-grid">
          <div class="form-field full-width">
            <label class="form-label">Job *</label>
            <Select
              v-model="uploadForm.job_id"
              :options="jobSelectOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="Select job"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label class="form-label">Kind</label>
            <Select v-model="uploadForm.kind" :options="kindOptions" optionLabel="label" optionValue="value" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label class="form-label">Caption</label>
            <Textarea v-model="uploadForm.caption" rows="3" class="w-full" placeholder="Short description" />
          </div>
          <div class="form-field full-width">
            <label class="form-label">Photo file *</label>
            <input type="file" accept="image/*" @change="onFileSelect" />
            <p class="hint" v-if="uploadForm.file">{{ uploadForm.file.name }}</p>
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="closeUpload" />
          <Button label="Upload" icon="pi pi-cloud-upload" :disabled="!canUpload" :loading="uploading" @click="submitUpload" />
        </template>
      </Dialog>

      <Dialog
        v-model:visible="lightboxVisible"
        header="Photo details"
        :style="{ width: 'min(90vw, 720px)' }"
        modal
        :dismissable-mask="true"
      >
        <div v-if="editingPhoto" class="lightbox-content">
          <div class="lightbox-image">
            <img :src="editingPhoto.url" :alt="editingPhoto.filename || 'Photo'" />
          </div>
          <div class="lightbox-form">
            <div class="form-field">
              <label class="form-label">Kind</label>
              <Select v-model="lightboxForm.kind" :options="kindOptions" optionLabel="label" optionValue="value" />
            </div>
            <div class="form-field">
              <label class="form-label">Caption</label>
              <Textarea v-model="lightboxForm.caption" rows="3" class="w-full" />
            </div>
            <div class="form-field">
              <label class="form-label">Uploaded</label>
              <small>{{ editingPhoto.uploaded_by || 'Unknown' }} • {{ editingPhoto.uploaded_at?.split('T')[0] || 'Unknown date' }}</small>
            </div>
          </div>
        </div>
        <template #footer>
          <Button label="Close" severity="secondary" text @click="closeLightbox" />
          <Button label="Delete" severity="danger" icon="pi pi-trash" aria-label="Delete" :loading="lightboxBusy" @click="deletePhoto" />
          <Button label="Save" icon="pi pi-save" :loading="lightboxBusy" @click="savePhoto" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Button from "primevue/button";
import Dialog from "primevue/dialog";
import Select from "primevue/select";
import Textarea from "primevue/textarea";
import Tag from "primevue/tag";
import Toolbar from "primevue/toolbar";
import ProgressSpinner from "primevue/progressspinner";
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();
const jobs = ref([]);
const photos = ref([]);
const loading = ref(true);
const selectedJob = ref(null);
const kindFilter = ref("all");
const uploadDialog = ref(false);
const lightboxVisible = ref(false);
const editingPhoto = ref(null);
const uploading = ref(false);
const lightboxBusy = ref(false);

const uploadForm = reactive({
  job_id: null,
  kind: "during",
  caption: "",
  file: null,
});

const lightboxForm = reactive({ kind: "during", caption: "" });

const kindFilters = [
  { value: "all", label: "All" },
  { value: "before", label: "Before" },
  { value: "during", label: "During" },
  { value: "after", label: "After" },
  { value: "progress", label: "Progress" },
  { value: "other", label: "Other" },
];

const kindOptions = kindFilters.filter((kind) => kind.value !== "all");

const jobSelectOptions = computed(() =>
  jobs.value.map((job) => ({ label: job.label, value: job.id }))
);

const filteredPhotos = computed(() => {
  const base = kindFilter.value === "all" ? photos.value : photos.value.filter((photo) => photo.kind === kindFilter.value);
  return base;
});

const canUpload = computed(() => uploadForm.job_id && uploadForm.file && !uploading.value);

function onFileSelect(event) {
  uploadForm.file = event.target.files?.[0] || null;
}

function openUploadDialog() {
  uploadDialog.value = true;
}

function closeUpload() {
  uploadDialog.value = false;
  uploadForm.file = null;
  uploadForm.caption = "";
  uploadForm.kind = "during";
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

async function loadPhotos() {
  loading.value = true;
  try {
    const endpoint = selectedJob.value ? `/api/jobs/${selectedJob.value}/photos` : "/api/photos/recent";
    const data = await api.get(endpoint);
    photos.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loading.value = false;
  }
}

watch(selectedJob, loadPhotos);

async function submitUpload() {
  if (!canUpload.value) return;
  uploading.value = true;
  try {
    const formData = new FormData();
    formData.append("job_id", uploadForm.job_id);
    formData.append("file", uploadForm.file);
    // Phase B2 (2026-04-24): switched from deleted /api/documents/upload
    // to canonical POST /api/documents. Form fields are the same; the
    // canonical route writes tenant_id + job_id correctly, which the
    // deleted route left NULL on some paths (root cause of the
    // 2026-04-22 preview bug).
    const doc = await api.post("/api/documents", formData);
    const payload = {
      url: `/api/documents/${doc.id}/download`,
      kind: uploadForm.kind,
      filename: doc.original_name || doc.filename,
      mime_type: doc.content_type,
      size_bytes: doc.size_bytes,
      caption: uploadForm.caption,
    };
    await api.post(`/api/jobs/${uploadForm.job_id}/photos`, payload, { successMessage: "Photo added" });
    closeUpload();
    await loadPhotos();
  } finally {
    uploading.value = false;
  }
}

function openLightbox(photo) {
  editingPhoto.value = photo;
  lightboxForm.kind = photo.kind;
  lightboxForm.caption = photo.caption || "";
  lightboxVisible.value = true;
}

function closeLightbox() {
  lightboxVisible.value = false;
  editingPhoto.value = null;
}

async function savePhoto() {
  if (!editingPhoto.value) return;
  lightboxBusy.value = true;
  try {
    await api.patch(`/api/jobs/${editingPhoto.value.job_id}/photos/${editingPhoto.value.id}`, {
      kind: lightboxForm.kind,
      caption: lightboxForm.caption,
    }, { successMessage: "Photo updated" });
    await loadPhotos();
    lightboxVisible.value = false;
  } finally {
    lightboxBusy.value = false;
  }
}

async function deletePhoto() {
  if (!editingPhoto.value) return;
  if (!(await confirmAsync({ header: 'Confirm', message: "Delete this photo?" }))) return;
  lightboxBusy.value = true;
  try {
    await api.del(`/api/jobs/${editingPhoto.value.job_id}/photos/${editingPhoto.value.id}`, {
      successMessage: "Photo deleted",
    });
    await loadPhotos();
    closeLightbox();
  } finally {
    lightboxBusy.value = false;
  }
}

onMounted(async () => {
  await loadJobs();
  await loadPhotos();
});
</script>

<style scoped>
.filters {
  display: flex;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
  margin: 1rem 0;
}
.job-select {
  min-width: 220px;
}
.kind-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.kind-chip.active {
  font-weight: 600;
}
.photo-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 1rem;
}
.photo-card {
  border: 1px solid var(--surface-border);
  border-radius: 0.65rem;
  overflow: hidden;
  background: var(--surface-ground);
  cursor: pointer;
  display: flex;
  flex-direction: column;
}
.photo-wrapper {
  position: relative;
  overflow: hidden;
  padding-top: 60%;
}
.photo-wrapper img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.photo-kind {
  position: absolute;
  bottom: 0.5rem;
  right: 0.5rem;
  background: rgba(0, 0, 0, 0.65);
  color: #fff;
}
.photo-meta {
  padding: 0.75rem 0.9rem;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}
.meta-title {
  font-weight: 600;
}
.meta-sub {
  display: flex;
  gap: 0.2rem;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}
.photo-caption {
  margin: 0;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
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
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.form-field.full-width {
  grid-column: 1 / -1;
}
.form-label {
  font-weight: 600;
  color: var(--p-text-muted-color);
}
.lightbox-content {
  display: flex;
  flex-wrap: wrap;
  gap: 1.5rem;
}
.lightbox-image {
  flex: 1 1 280px;
  border-radius: 0.5rem;
  overflow: hidden;
  border: 1px solid var(--surface-border);
}
.lightbox-image img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.lightbox-form {
  flex: 1 1 240px;
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem 0;
}
</style>
