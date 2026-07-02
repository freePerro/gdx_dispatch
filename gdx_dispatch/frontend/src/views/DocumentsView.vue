<template>
    <section class="documents-view">
      <div class="folder-panel">
        <!-- role="tree": FolderTreeNode rows are role="treeitem" (2026-07-01
             a11y audit) and need a tree ancestor to be a valid structure. -->
        <div class="folder-list" role="tree" aria-label="Document folders">
          <div
            class="folder-item folder-item--all"
            :class="{ active: !selectedFolder, 'drop-target': rootDropActive }"
            role="treeitem"
            :aria-selected="!selectedFolder"
            tabindex="0"
            @keydown.enter="selectFolder(null)"
            @keydown.space.prevent="selectFolder(null)"
            @click="selectFolder(null)"
            @dragover.prevent="onRootDragOver"
            @dragenter.prevent="onRootDragEnter"
            @dragleave="rootDropActive = false"
            @drop.prevent="onRootDrop"
            data-testid="folder-all"
          >
            <span class="folder-toggle-spacer"></span>
            <i class="pi pi-inbox folder-icon"></i>
            <span class="folder-name">All Documents</span>
          </div>
          <div v-if="!folderTree.length" class="folder-empty">
            No folders yet. Upload one or click New Folder.
          </div>
          <FolderTreeNode
            v-for="root in folderTree"
            :key="root.id"
            :node="root"
            :selected-id="selectedFolder?.id || null"
            :can-delete="isAdmin"
            :can-move="isAdmin"
            @select="selectFolder"
            @delete="askDeleteFolder"
            @move="onFolderMove"
          />
        </div>
        <div class="folder-actions">
          <div v-if="isCreatingFolder" class="inline-create">
            <InputText v-model="newFolderName" size="small"
              :placeholder="selectedFolder ? `New folder under &quot;${selectedFolder.name}&quot;` : 'Folder name'"
              @keyup.enter="createFolder" @keyup.esc="isCreatingFolder = false" autofocus data-testid="new-folder-input" />
            <Button v-tooltip="'Create folder'" aria-label="Create folder" icon="pi pi-check" text size="small" @click="createFolder" />
          </div>
          <Button v-else
            :label="selectedFolder ? `New under ${selectedFolder.name}` : 'New Folder'"
            icon="pi pi-plus" text class="w-full"
            @click="isCreatingFolder = true" data-testid="new-folder-btn" />
        </div>
      </div>
      <div class="documents-main view-card">
      <Toolbar class="documents-toolbar">
        <template #start>
          <InputText
            id="documents-search"
            name="documents-search"
            v-model="searchQuery"
            placeholder="Search by filename..."
            class="search-input"
            data-testid="documents-search"
          />
          <Select
            v-model="categoryFilter"
            :options="categoryOptions"
            placeholder="All Categories"
            :showClear="true"
            data-testid="documents-category-filter"
            class="category-dropdown"
          />
          <Select
            v-model="linkFilter"
            :options="linkFilterOptions"
            optionLabel="label"
            optionValue="value"
            placeholder="All Linked"
            :showClear="true"
            data-testid="documents-link-filter"
            class="link-dropdown"
          />
        </template>
        <template #end>
          <Button
            v-if="isAdmin"
            label="Upload Folder"
            icon="pi pi-folder-open"
            severity="secondary"
            class="mr-2"
            data-testid="upload-folder-btn"
            @click="folderInputRef?.click()"
          />
          <input
            ref="folderInputRef"
            type="file"
            webkitdirectory
            directory
            multiple
            style="display: none"
            data-testid="folder-input"
            @change="onFolderSelected"
          />
          <Button
            label="Upload Document"
            icon="pi pi-upload"
            data-testid="upload-document-btn"
            @click="showUploadDialog = true"
          />
        </template>
      </Toolbar>

      <div v-if="loadError" class="inline-error" data-testid="documents-load-error">
        <i class="pi pi-exclamation-triangle"></i> {{ loadError }}
      </div>

      <div v-if="loading" class="spinner-wrap" data-testid="documents-loading">
        <ProgressSpinner />
      </div>

      <DataTable
        v-else-if="filteredDocuments.length"
        :value="filteredDocuments"
        :paginator="true"
        :rows="20"
        :rowsPerPageOptions="[10, 20, 50]"
        data-testid="documents-datatable"
        stripedRows
        responsiveLayout="scroll"
        sortField="created_at"
        :sortOrder="-1"
        class="documents-table"
      >
        <Column header="" style="width: 50px; text-align: center">
          <template #body="{ data }">
            <i :class="fileIcon(data)" :style="{ fontSize: '1.3rem', color: fileIconColor(data) }"></i>
          </template>
        </Column>
        <Column field="name" header="Name" sortable>
          <template #body="{ data }">
            <a
              v-if="canPreview(data)"
              href="#"
              class="doc-link"
              :data-testid="'doc-preview-' + data.id"
              @click.prevent="openPreview(data)"
            >{{ data.title || data.original_name || data.name || data.filename }}</a>
            <a
              v-else-if="data.url || data.download_url"
              :href="data.url || data.download_url"
              target="_blank"
              class="doc-link"
              :data-testid="'doc-download-' + data.id"
            >{{ data.title || data.original_name || data.name || data.filename }}</a>
            <span v-else>{{ data.title || data.original_name || data.name || data.filename }}</span>
          </template>
        </Column>
        <Column field="category" header="Category" sortable style="width: 130px">
          <template #body="{ data }">
            <Tag v-if="data.category" :value="data.category" severity="info" />
            <span v-else class="text-muted">-</span>
          </template>
        </Column>
        <Column header="Linked To" style="width: 180px">
          <template #body="{ data }">
            <span v-if="data.job_name" class="linked-badge">
              <i class="pi pi-wrench"></i> {{ data.job_name }}
            </span>
            <span v-else-if="data.customer_name" class="linked-badge">
              <i class="pi pi-user"></i> {{ data.customer_name }}
            </span>
            <span v-else class="text-muted">--</span>
          </template>
        </Column>
        <Column field="type" header="Type" sortable style="width: 110px">
          <template #body="{ data }">
            {{ formatMimeShort(data) }}
          </template>
        </Column>
        <Column field="size" header="Size" sortable style="width: 100px">
          <template #body="{ data }">{{ data.size ? formatSize(data.size) : '-' }}</template>
        </Column>
        <Column field="created_at" header="Uploaded" sortable style="width: 120px">
          <template #body="{ data }">{{ formatDate(data) }}</template>
        </Column>
        <Column field="uploaded_by" header="By" style="width: 120px">
          <template #body="{ data }">{{ data.uploaded_by || data.user_name || '-' }}</template>
        </Column>
        <Column header="Actions" :style="{ width: '100px', textAlign: 'center' }">
          <template #body="{ data }">
            <Button
              v-tooltip="'Download'"
              aria-label="Download"
              icon="pi pi-download"
              text
              rounded
              size="small"
              :data-testid="'doc-download-btn-' + data.id"
              @click="downloadDocument(data)"
            />
            <Button
              v-tooltip="'Delete'"
              icon="pi pi-trash" aria-label="Delete"
              severity="danger"
              text
              rounded
              size="small"
              :data-testid="'doc-delete-' + data.id"
              @click="openDeleteDialog(data)"
            />
          </template>
        </Column>
      </DataTable>

      <div v-else-if="!loading" class="empty-state" data-testid="documents-empty">
        <i class="pi pi-folder-open" style="font-size: 3rem; color: var(--p-text-muted-color)"></i>
        <h3>No documents found</h3>
        <p v-if="searchQuery || categoryFilter">Try adjusting your search or filters.</p>
        <p v-else>Upload your first document to get started.</p>
        <Button
          v-if="!searchQuery && !categoryFilter"
          label="Upload Document"
          icon="pi pi-upload"
          @click="showUploadDialog = true"
          data-testid="empty-upload-btn"
        />
      </div>

      <!-- Upload Dialog -->
      <Dialog
        v-model:visible="showUploadDialog"
        header="Upload Document"
        :style="{ width: '32rem' }"
        modal
        data-testid="upload-document-dialog"
        @hide="resetUploadForm"
      >
        <form class="dialog-form" @submit.prevent="submitUpload">
          <!-- Hybrid upload UI: polished drag-drop + native input fallback.
               Why both? In sandboxed browsers (snap Firefox, some Flatpaks) the
               OS file-picker portal is blocked. JS .click()/showPicker() silently
               no-op, breaking the drop-zone click. The visible native input below
               always works because the browser handles the click → picker call
               with no JS in between. 99% of users use the drop-zone; the 1% on
               broken portals still have a working path. -->
          <label
            class="drop-zone"
            for="doc-file-input"
            :class="{ 'drop-zone-active': isDragOver, 'drop-zone-has-file': uploadForm.file }"
            data-testid="doc-drop-zone"
            @dragover.prevent="isDragOver = true"
            @dragleave.prevent="isDragOver = false"
            @drop.prevent="onDrop"
          >
            <input
              id="doc-file-input"
              ref="fileInputRef"
              type="file"
              class="visually-hidden-file-input"
              data-testid="doc-file-input"
              :accept="acceptedTypes"
              @change="onFileSelect"
            />
            <div v-if="!uploadForm.file" class="drop-zone-prompt">
              <i class="pi pi-cloud-upload" style="font-size: 2.5rem; color: var(--p-primary-color)"></i>
              <p><strong>Drag & drop</strong> a file here, or <strong>click anywhere</strong> to browse</p>
              <small class="hint">Max 25MB. PDF, DOC, XLS, images, and more.</small>
            </div>
            <div v-else class="drop-zone-selected">
              <i :class="fileIconFromName(uploadForm.file.name)" style="font-size: 1.5rem"></i>
              <div class="selected-file-info">
                <strong>{{ uploadForm.file.name }}</strong>
                <small>{{ formatSize(uploadForm.file.size) }}</small>
              </div>
              <Button
                v-tooltip="'Remove'"
                icon="pi pi-times" aria-label="Remove"
                text
                rounded
                size="small"
                severity="danger"
                @click.stop.prevent="clearFile"
                data-testid="doc-clear-file-btn"
              />
            </div>
          </label>

          <!-- Always-works fallback for sandboxed browsers (snap/flatpak). -->
          <details class="upload-fallback" data-testid="doc-upload-fallback">
            <summary>File picker not opening? Use this</summary>
            <input
              type="file"
              class="native-file-input"
              data-testid="doc-file-input-fallback"
              :accept="acceptedTypes"
              @change="onFileSelect"
            />
          </details>

          <!-- Upload progress -->
          <ProgressBar
            v-if="uploadProgress > 0 && uploading"
            :value="uploadProgress"
            :showValue="true"
            data-testid="upload-progress"
            class="upload-progress"
          />

          <div class="form-field">
            <label for="doc-name">Document Name</label>
            <InputText
              id="doc-name"
              v-model="uploadForm.name"
              placeholder="Auto-filled from filename"
              data-testid="doc-name-input"
              class="w-full"
            />
          </div>

          <div class="form-row">
            <div class="form-field">
              <label for="doc-category">Category</label>
              <Select
                id="doc-category"
                v-model="uploadForm.category"
                :options="categoryOptions"
                data-testid="doc-category-dropdown"
                class="w-full"
              />
            </div>
            <div class="form-field">
              <label for="doc-link-type">Link To</label>
              <Select
                id="doc-link-type"
                v-model="uploadForm.linkType"
                :options="linkTypeOptions"
                data-testid="doc-link-type-dropdown"
                class="w-full"
              />
            </div>
          </div>

          <div v-if="uploadForm.linkType === 'Job'" class="form-field">
            <label for="doc-job">Select Job</label>
            <Select
              id="doc-job"
              v-model="uploadForm.job_id"
              :options="jobOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="Choose a job..."
              filter
              :showClear="true"
              data-testid="doc-job-dropdown"
              class="w-full"
            />
          </div>

          <div v-if="uploadForm.linkType === 'Customer'" class="form-field">
            <label for="doc-customer">Select Customer</label>
            <Select
              id="doc-customer"
              v-model="uploadForm.customer_id"
              :options="customerOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="Choose a customer..."
              filter
              :showClear="true"
              data-testid="doc-customer-dropdown"
              class="w-full"
            />
          </div>

          <div class="form-field">
            <label for="doc-notes">Notes</label>
            <Textarea
              id="doc-notes"
              v-model="uploadForm.notes"
              rows="2"
              data-testid="doc-notes-input"
              class="w-full"
            />
          </div>

          <div v-if="uploadError" class="inline-error" data-testid="upload-error">
            <i class="pi pi-exclamation-triangle"></i> {{ uploadError }}
          </div>

          <div class="form-actions">
            <Button type="button" label="Cancel" text @click="showUploadDialog = false" />
            <Button
              type="submit"
              label="Upload"
              icon="pi pi-upload"
              :loading="uploading"
              :disabled="!uploadForm.file"
              data-testid="doc-upload-submit-btn"
            />
          </div>
        </form>
      </Dialog>

      <!-- Bulk Folder Upload Dialog (admin-only) -->
      <Dialog
        v-model:visible="showBulkDialog"
        header="Upload Folder"
        :style="{ width: '40rem' }"
        :modal="true"
        :closable="!bulkUploading"
        data-testid="bulk-upload-dialog"
        @hide="resetBulk"
      >
        <div v-if="bulkFiles.length && !bulkUploading && !bulkDone">
          <p>
            <strong>{{ bulkFiles.length }}</strong> file{{ bulkFiles.length === 1 ? '' : 's' }}
            across <strong>{{ bulkFolderNames.length }}</strong> folder{{ bulkFolderNames.length === 1 ? '' : 's' }}
            ({{ formatSize(bulkTotalSize) }} total).
          </p>
          <p v-if="bulkOversize.length" class="bulk-warn">
            <i class="pi pi-exclamation-triangle"></i>
            {{ bulkOversize.length }} file{{ bulkOversize.length === 1 ? '' : 's' }} exceed the 25MB limit and will be skipped.
          </p>
          <p v-if="bulkFiles.length > 500" class="bulk-warn">
            <i class="pi pi-exclamation-triangle"></i>
            Large batch — uploads run sequentially and may take a while.
          </p>
          <details class="bulk-folders">
            <summary>Folders to create / use ({{ bulkFolderNames.length }})</summary>
            <ul>
              <li v-for="name in bulkFolderNames" :key="name">{{ name || '(root)' }}</li>
            </ul>
          </details>
          <div class="dialog-actions">
            <Button label="Cancel" text @click="showBulkDialog = false" />
            <Button label="Start Upload" icon="pi pi-cloud-upload" @click="runBulkUpload" data-testid="bulk-upload-start" />
          </div>
        </div>

        <div v-if="bulkUploading">
          <p>Uploading {{ bulkProgress.done + bulkProgress.failed }} / {{ bulkProgress.total }}…</p>
          <ProgressBar :value="bulkPercent" />
          <p class="bulk-current">{{ bulkProgress.current }}</p>
        </div>

        <div v-if="bulkDone">
          <p>
            <i class="pi pi-check-circle" style="color: var(--p-green-500)"></i>
            Uploaded <strong>{{ bulkProgress.done }}</strong> of {{ bulkProgress.total }}.
            <span v-if="bulkProgress.failed">
              <i class="pi pi-times-circle" style="color: var(--p-red-500)"></i>
              {{ bulkProgress.failed }} failed.
            </span>
          </p>
          <details v-if="bulkErrors.length" class="bulk-errors">
            <summary>{{ bulkErrors.length }} error{{ bulkErrors.length === 1 ? '' : 's' }}</summary>
            <ul>
              <li v-for="(err, i) in bulkErrors" :key="i">
                <code>{{ err.path }}</code> — {{ err.message }}
              </li>
            </ul>
          </details>
          <div class="dialog-actions">
            <Button label="Done" @click="showBulkDialog = false" data-testid="bulk-upload-done" />
          </div>
        </div>
      </Dialog>

      <!-- Delete Folder Dialog (admin) -->
      <Dialog
        v-model:visible="showDeleteFolderDialog"
        header="Delete folder?"
        :style="{ width: '28rem' }"
        :modal="true"
        :closable="!deletingFolder"
        data-testid="delete-folder-dialog"
      >
        <p v-if="folderToDelete">
          Delete folder <strong>"{{ folderToDelete.name }}"</strong>?
        </p>
        <p v-if="folderDocCount > 0" class="bulk-warn">
          <i class="pi pi-exclamation-triangle"></i>
          {{ folderDocCount }} document{{ folderDocCount === 1 ? '' : 's' }} in this folder will also be deleted.
        </p>
        <p v-else class="text-muted" style="font-size: 0.9rem; margin: 0.5rem 0;">
          This folder has no documents.
        </p>
        <div class="dialog-actions">
          <Button label="Cancel" text :disabled="deletingFolder" @click="showDeleteFolderDialog = false" />
          <Button
            label="Delete"
            severity="danger"
            icon="pi pi-trash"
            :loading="deletingFolder"
            data-testid="folder-delete-confirm"
            @click="confirmDeleteFolder"
          />
        </div>
      </Dialog>

      <!-- Preview Dialog -->
      <Dialog
        v-model:visible="showPreviewDialog"
        @hide="onPreviewClose"
        :header="previewDoc?.name || previewDoc?.filename || 'Preview'"
        :style="{ width: '50rem', maxHeight: '90vh' }"
        modal
        data-testid="doc-preview-dialog"
      >
        <div class="preview-content" data-testid="doc-preview-content">
          <img
            v-if="isImagePreview"
            :src="previewUrl"
            :alt="previewDoc?.name || 'Document preview'"
            class="preview-image"
          />
          <iframe
            v-else-if="isPdfPreview"
            :src="previewUrl"
            class="preview-pdf"
            title="PDF Preview"
            @error="onPreviewError"
          ></iframe>
          <video
            v-else-if="isVideoPreview"
            :src="previewUrl"
            class="preview-video"
            controls
            data-testid="doc-preview-video"
          ></video>
          <audio
            v-else-if="isAudioPreview"
            :src="previewUrl"
            class="preview-audio"
            controls
            data-testid="doc-preview-audio"
          ></audio>
          <pre
            v-else-if="isTextPreview"
            class="preview-text"
            data-testid="doc-preview-text"
          >{{ previewText }}</pre>
          <div v-else-if="previewError" class="preview-error" data-testid="doc-preview-error">
            {{ previewError }}
          </div>
          <div v-else class="preview-unsupported" data-testid="doc-preview-unsupported">
            <i class="pi pi-file" style="font-size: 2.5rem; color: var(--p-text-muted-color)"></i>
            <p>Preview not available for this file type.</p>
            <small>Use the Download button below to open it in an external app.</small>
          </div>
        </div>
        <template #footer>
          <Button
            label="Download"
            icon="pi pi-download"
            @click="downloadDocument(previewDoc)"
            data-testid="preview-download-btn"
          />
          <Button label="Close" text @click="showPreviewDialog = false" />
        </template>
      </Dialog>

      <!-- Delete Confirmation -->
      <Dialog
        v-model:visible="showDeleteDialog"
        header="Confirm Delete"
        :style="{ width: '400px' }"
        modal
        data-testid="doc-delete-dialog"
      >
        <div class="delete-confirm-content">
          <i class="pi pi-exclamation-triangle" style="font-size: 2rem; color: #e94560"></i>
          <p>Are you sure you want to delete "<strong>{{ deleteTarget?.name || deleteTarget?.filename }}</strong>"?</p>
          <p class="text-muted">This action cannot be undone.</p>
        </div>
        <template #footer>
          <Button label="Cancel" text @click="showDeleteDialog = false" />
          <Button
            label="Delete"
            severity="danger"
            icon="pi pi-trash" aria-label="Delete"
            :loading="deleting"
            data-testid="doc-confirm-delete-btn"
            @click="confirmDelete"
          />
        </template>
      </Dialog>

      <Toast data-testid="documents-toast" />
    </div>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useToast } from 'primevue/usetoast';
import FolderTreeNode from '../components/FolderTreeNode.vue';
import { useApi } from '../composables/useApi';
import { formatDate as fmtDate } from '../composables/useFormatters';
import { ensureFolderPath, folderSegmentsFromFile } from '../composables/useFolderPath';
import { createAuthedBlobUrl, openAuthedFile } from '../composables/useAuthedFile';
import { useAuthStore } from '../stores/auth';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import Select from 'primevue/select';
import InputText from 'primevue/inputtext';
import ProgressBar from 'primevue/progressbar';
import ProgressSpinner from 'primevue/progressspinner';
import Tag from 'primevue/tag';
import Textarea from 'primevue/textarea';
import Toast from 'primevue/toast';
import Toolbar from 'primevue/toolbar';

const MAX_FILE_SIZE = 25 * 1024 * 1024; // 25MB

const api = useApi();
const toast = useToast();
const auth = useAuthStore();

const documents = ref([]);
const folders = ref([]);
const selectedFolder = ref(null);

const folderTree = computed(() => {
  const byId = new Map();
  for (const f of folders.value) {
    byId.set(f.id, { ...f, children: [] });
  }
  const roots = [];
  for (const node of byId.values()) {
    if (node.parent_id && byId.has(node.parent_id)) {
      byId.get(node.parent_id).children.push(node);
    } else {
      roots.push(node);
    }
  }
  const sortRec = (nodes) => {
    nodes.sort((a, b) => a.name.localeCompare(b.name));
    for (const n of nodes) sortRec(n.children);
  };
  sortRec(roots);
  return roots;
});
const isCreatingFolder = ref(false);
const newFolderName = ref('');
const loading = ref(false);
const uploading = ref(false);
const deleting = ref(false);
const loadError = ref('');
const uploadError = ref('');
const uploadProgress = ref(0);
const searchQuery = ref('');
const categoryFilter = ref(null);
const linkFilter = ref(null);
const showUploadDialog = ref(false);
const showDeleteDialog = ref(false);
const showPreviewDialog = ref(false);
const deleteTarget = ref(null);
const previewDoc = ref(null);
const previewUrl = ref('');
const previewText = ref('');
const previewError = ref('');
const isDragOver = ref(false);
const fileInputRef = ref(null);

// Bulk folder upload (admin)
const folderInputRef = ref(null);
const showBulkDialog = ref(false);
const bulkFiles = ref([]); // raw File[] (filtered, under MAX_FILE_SIZE)
const bulkOversize = ref([]); // File[] over the limit
const bulkUploading = ref(false);
const bulkDone = ref(false);
const bulkErrors = ref([]);
const bulkProgress = ref({ total: 0, done: 0, failed: 0, current: '' });

const isAdmin = computed(() => auth.isAdmin);

// Delete folder (admin)
const showDeleteFolderDialog = ref(false);
const folderToDelete = ref(null);
const folderDocCount = ref(0);
const deletingFolder = ref(false);

const bulkFolderPaths = computed(() => {
  const set = new Set();
  for (const f of bulkFiles.value) {
    const segs = folderSegmentsFromFile(f);
    if (segs.length) set.add(segs.join('/'));
  }
  return Array.from(set).sort();
});
// Back-compat alias for any template that still references bulkFolderNames.
const bulkFolderNames = bulkFolderPaths;
const bulkTotalSize = computed(() => bulkFiles.value.reduce((s, f) => s + f.size, 0));
const bulkPercent = computed(() => {
  const t = bulkProgress.value.total;
  if (!t) return 0;
  return Math.round(((bulkProgress.value.done + bulkProgress.value.failed) / t) * 100);
});


// Link data
const jobOptions = ref([]);
const customerOptions = ref([]);

const categoryOptions = [
  'Contracts',
  'Invoices',
  'Permits',
  'Photos',
  'Warranties',
  'Insurance',
  'Training',
  'Manuals',
  'Other',
];

const linkTypeOptions = ['None', 'Job', 'Customer'];
const linkFilterOptions = [
  { label: 'Linked to Job', value: 'job' },
  { label: 'Linked to Customer', value: 'customer' },
  { label: 'Unlinked', value: 'none' },
];

const acceptedTypes = '.pdf,.doc,.docx,.xls,.xlsx,.csv,.tsv,.png,.jpg,.jpeg,.gif,.webp,.svg,.bmp,.avif,.mp4,.webm,.mov,.m4v,.mp3,.wav,.ogg,.m4a,.txt,.rtf,.json,.xml,.md,.log,.html,.zip';

const uploadForm = ref(emptyUploadForm());

function emptyUploadForm() {
  return {
    file: null,
    name: '',
    category: 'Other',
    notes: '',
    linkType: 'None',
    job_id: null,
    customer_id: null,
  };
}

function resetUploadForm() {
  uploadForm.value = emptyUploadForm();
  uploadError.value = '';
  uploadProgress.value = 0;
  isDragOver.value = false;
}

// --- File helpers ---

function formatSize(bytes) {
  if (!bytes || bytes <= 0) return '0 B';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function formatDate(doc) {
  return fmtDate(doc.created_at || doc.uploaded_at);
}

function formatMimeShort(doc) {
  const mime = doc.type || doc.mime_type || doc.content_type || '';
  const name = doc.name || doc.filename || '';
  const ext = name.split('.').pop()?.toLowerCase();
  if (ext) return ext.toUpperCase();
  if (mime.includes('pdf')) return 'PDF';
  if (mime.includes('image')) return 'Image';
  if (mime.includes('spreadsheet') || mime.includes('excel')) return 'XLS';
  if (mime.includes('word') || mime.includes('document')) return 'DOC';
  return mime.split('/').pop()?.toUpperCase() || '-';
}

function fileIcon(doc) {
  const mime = doc.type || doc.mime_type || doc.content_type || '';
  const name = doc.name || doc.filename || '';
  return fileIconFromMimeOrName(mime, name);
}

function fileIconFromName(name) {
  return fileIconFromMimeOrName('', name);
}

function fileIconFromMimeOrName(mime, name) {
  const ext = (name || '').split('.').pop()?.toLowerCase();
  if (mime.includes('pdf') || ext === 'pdf') return 'pi pi-file-pdf';
  if (mime.includes('image') || ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext))
    return 'pi pi-image';
  if (
    mime.includes('spreadsheet') ||
    mime.includes('excel') ||
    ['xls', 'xlsx', 'csv'].includes(ext)
  )
    return 'pi pi-file-excel';
  if (mime.includes('word') || mime.includes('document') || ['doc', 'docx', 'rtf'].includes(ext))
    return 'pi pi-file-word';
  if (mime.includes('zip') || ['zip', 'tar', 'gz', 'rar'].includes(ext)) return 'pi pi-box';
  return 'pi pi-file';
}

function fileIconColor(doc) {
  const mime = doc.type || doc.mime_type || doc.content_type || '';
  const name = doc.name || doc.filename || '';
  const ext = (name || '').split('.').pop()?.toLowerCase();
  if (mime.includes('pdf') || ext === 'pdf') return '#e53935';
  if (mime.includes('image') || ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext))
    return '#4fc3f7';
  if (
    mime.includes('spreadsheet') ||
    mime.includes('excel') ||
    ['xls', 'xlsx', 'csv'].includes(ext)
  )
    return '#4caf50';
  if (mime.includes('word') || ['doc', 'docx'].includes(ext)) return '#2196f3';
  return '#9e9e9e';
}

const IMAGE_EXTS = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'bmp', 'avif'];
const VIDEO_EXTS = ['mp4', 'webm', 'mov', 'ogv', 'm4v'];
const AUDIO_EXTS = ['mp3', 'wav', 'ogg', 'm4a', 'aac', 'flac'];
const TEXT_EXTS = ['txt', 'csv', 'tsv', 'json', 'md', 'markdown', 'xml', 'yml', 'yaml', 'log', 'ini', 'conf', 'rtf', 'html', 'htm'];

function _previewMeta(doc) {
  const mime = doc?.type || doc?.mime_type || doc?.content_type || '';
  const name = doc?.name || doc?.filename || '';
  const ext = (name || '').split('.').pop()?.toLowerCase();
  return { mime, ext };
}

function canPreview(doc) {
  const { mime, ext } = _previewMeta(doc);
  return (
    mime.startsWith('image/') ||
    mime.startsWith('video/') ||
    mime.startsWith('audio/') ||
    mime.startsWith('text/') ||
    mime.includes('json') ||
    mime.includes('xml') ||
    mime.includes('pdf') ||
    [...IMAGE_EXTS, ...VIDEO_EXTS, ...AUDIO_EXTS, ...TEXT_EXTS, 'pdf'].includes(ext)
  );
}

const isImagePreview = computed(() => {
  if (!previewDoc.value) return false;
  const { mime, ext } = _previewMeta(previewDoc.value);
  return mime.startsWith('image/') || IMAGE_EXTS.includes(ext);
});

const isPdfPreview = computed(() => {
  if (!previewDoc.value) return false;
  const { mime, ext } = _previewMeta(previewDoc.value);
  return mime.includes('pdf') || ext === 'pdf';
});

const isVideoPreview = computed(() => {
  if (!previewDoc.value) return false;
  const { mime, ext } = _previewMeta(previewDoc.value);
  return mime.startsWith('video/') || VIDEO_EXTS.includes(ext);
});

const isAudioPreview = computed(() => {
  if (!previewDoc.value) return false;
  const { mime, ext } = _previewMeta(previewDoc.value);
  return mime.startsWith('audio/') || AUDIO_EXTS.includes(ext);
});

const isTextPreview = computed(() => {
  if (!previewDoc.value) return false;
  const { mime, ext } = _previewMeta(previewDoc.value);
  return mime.startsWith('text/') || mime.includes('json') || mime.includes('xml') || TEXT_EXTS.includes(ext);
});

// --- Filtering ---

const filteredDocuments = computed(() => {
  let list = documents.value;
  if (categoryFilter.value) {
    list = list.filter((d) => d.category === categoryFilter.value);
  }
  if (linkFilter.value === 'job') {
    list = list.filter((d) => d.job_id || d.job_name);
  } else if (linkFilter.value === 'customer') {
    list = list.filter((d) => d.customer_id || d.customer_name);
  } else if (linkFilter.value === 'none') {
    list = list.filter((d) => !d.job_id && !d.customer_id && !d.job_name && !d.customer_name);
  }
  const q = searchQuery.value.trim().toLowerCase();
  if (!q) return list;
  return list.filter(
    (d) =>
      (d.name || d.filename || '').toLowerCase().includes(q) ||
      (d.category || '').toLowerCase().includes(q) ||
      (d.uploaded_by || d.user_name || '').toLowerCase().includes(q),
  );
});

// --- File input handling ---

function onFileSelect(event) {
  const file = event.target.files?.[0] || null;
  setFile(file);
}

function onDrop(event) {
  isDragOver.value = false;
  const file = event.dataTransfer?.files?.[0] || null;
  setFile(file);
}

function setFile(file) {
  uploadError.value = '';
  if (!file) return;
  if (file.size > MAX_FILE_SIZE) {
    uploadError.value = `File exceeds 25MB limit (${formatSize(file.size)}).`;
    return;
  }
  uploadForm.value.file = file;
  if (!uploadForm.value.name) {
    uploadForm.value.name = file.name;
  }
}

function clearFile() {
  uploadForm.value.file = null;
  uploadForm.value.name = '';
  uploadError.value = '';
  if (fileInputRef.value) {
    fileInputRef.value.value = '';
  }
}

// --- Preview ---

async function openPreview(doc) {
  previewDoc.value = doc;
  previewError.value = '';
  previewText.value = '';
  revokePreviewUrl();
  showPreviewDialog.value = true;
  const url = doc.url || doc.download_url || `/api/documents/${doc.id}/download`;
  try {
    previewUrl.value = await createAuthedBlobUrl(url);
    if (isTextPreview.value) {
      // Read the same blob as text so we can render a <pre> view.
      const resp = await fetch(previewUrl.value);
      const blob = await resp.blob();
      const MAX_TEXT_BYTES = 1024 * 1024; // 1 MB cap — anything larger would lock the browser
      if (blob.size > MAX_TEXT_BYTES) {
        previewText.value = `(File is ${formatSize(blob.size)} — too large to preview as text. Use Download to view.)`;
      } else {
        previewText.value = await blob.text();
      }
    }
  } catch (e) {
    previewError.value = e?.message || 'Failed to load preview';
    console.error('document_preview_failed', url, e);
    toast.add({
      severity: 'error',
      summary: 'Preview failed',
      detail: previewError.value,
      life: 5000,
    });
  }
}

function onPreviewError() {
  previewError.value = 'The file could not be displayed in the preview pane.';
  console.error('document_preview_iframe_error', previewDoc.value?.id);
}

function revokePreviewUrl() {
  if (previewUrl.value && previewUrl.value.startsWith('blob:')) {
    URL.revokeObjectURL(previewUrl.value);
  }
  previewUrl.value = '';
}

function onPreviewClose() {
  revokePreviewUrl();
  previewDoc.value = null;
  previewError.value = '';
  previewText.value = '';
}

// --- Download ---

async function downloadDocument(doc) {
  const url = doc.url || doc.download_url || `/api/documents/${doc.id}/download`;
  try {
    await openAuthedFile(url);
  } catch (e) {
    console.error('document_download_failed', url, e);
    toast.add({
      severity: 'error',
      summary: 'Download failed',
      detail: e?.message || 'Failed to download document',
      life: 5000,
    });
  }
}

// --- API calls ---

async function fetchFolders() {
  try {
    const result = await api.get('/api/document-folders');
    const payload = result?.data || result;
    folders.value = Array.isArray(payload) ? payload : payload?.items || [];
  } catch { folders.value = []; }
}

function selectFolder(folder) {
  selectedFolder.value = folder;
  fetchDocuments();
}

async function createFolder() {
  if (!newFolderName.value.trim()) { isCreatingFolder.value = false; return; }
  try {
    await api.post('/api/document-folders', {
      name: newFolderName.value,
      parent_id: selectedFolder.value?.id || null,
    });
    newFolderName.value = '';
    isCreatingFolder.value = false;
    await fetchFolders();
  } catch { /* toast handled by useApiWithToast */ }
}

const rootDropActive = ref(false);
const FOLDER_DRAG_MIME = 'application/x-gdx-folder-id';

function onRootDragOver(e) {
  if (e.dataTransfer.types.includes(FOLDER_DRAG_MIME)) {
    e.dataTransfer.dropEffect = 'move';
  }
}

function onRootDragEnter(e) {
  if (e.dataTransfer.types.includes(FOLDER_DRAG_MIME)) {
    rootDropActive.value = true;
  }
}

function onRootDrop(e) {
  rootDropActive.value = false;
  const draggedId = e.dataTransfer.getData(FOLDER_DRAG_MIME);
  if (!draggedId) return;
  onFolderMove({ folderId: draggedId, newParentId: null });
}

async function onFolderMove({ folderId, newParentId }) {
  try {
    await api.patch(`/api/document-folders/${folderId}/move`, { parent_id: newParentId });
    toast.add({ severity: 'success', summary: 'Folder moved', life: 2500 });
    await fetchFolders();
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Move failed',
      detail: err?.message || 'Could not move folder.',
      life: 5000,
    });
  }
}

// --- Delete folder (admin) ---

async function askDeleteFolder(folder) {
  folderToDelete.value = folder;
  folderDocCount.value = 0;
  showDeleteFolderDialog.value = true;
  try {
    const result = await api.get(`/api/documents?folder_id=${folder.id}`);
    const payload = result?.data || result;
    const list = Array.isArray(payload) ? payload : payload?.items || [];
    folderDocCount.value = list.length;
  } catch {
    folderDocCount.value = 0;
  }
}

async function confirmDeleteFolder() {
  if (!folderToDelete.value) return;
  deletingFolder.value = true;
  try {
    await api.del(`/api/document-folders/${folderToDelete.value.id}`);
    toast.add({
      severity: 'success',
      summary: 'Folder deleted',
      detail: folderDocCount.value
        ? `"${folderToDelete.value.name}" and ${folderDocCount.value} document${folderDocCount.value === 1 ? '' : 's'} deleted.`
        : `"${folderToDelete.value.name}" deleted.`,
      life: 3000,
    });
    if (selectedFolder.value?.id === folderToDelete.value.id) {
      selectedFolder.value = null;
    }
    showDeleteFolderDialog.value = false;
    folderToDelete.value = null;
    await fetchFolders();
    await fetchDocuments();
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Delete failed',
      detail: err?.message || 'Could not delete folder.',
      life: 5000,
    });
  } finally {
    deletingFolder.value = false;
  }
}

// --- Bulk folder upload ---

const BULK_HARD_CAP = 2000;

function resetBulk() {
  if (bulkUploading.value) return;
  bulkFiles.value = [];
  bulkOversize.value = [];
  bulkErrors.value = [];
  bulkDone.value = false;
  bulkProgress.value = { total: 0, done: 0, failed: 0, current: '' };
  if (folderInputRef.value) folderInputRef.value.value = '';
}

function onFolderSelected(event) {
  const all = Array.from(event.target.files || []);
  if (!all.length) return;
  if (all.length > BULK_HARD_CAP) {
    toast.add({
      severity: 'error',
      summary: 'Too many files',
      detail: `Max ${BULK_HARD_CAP} per upload. You selected ${all.length}.`,
      life: 6000,
    });
    if (folderInputRef.value) folderInputRef.value.value = '';
    return;
  }
  bulkOversize.value = all.filter((f) => f.size > MAX_FILE_SIZE);
  bulkFiles.value = all.filter((f) => f.size <= MAX_FILE_SIZE);
  bulkErrors.value = [];
  bulkDone.value = false;
  bulkProgress.value = { total: 0, done: 0, failed: 0, current: '' };
  showBulkDialog.value = true;
}

async function _createFolderApi({ name, parent_id }) {
  const created = await api.post('/api/document-folders', { name, parent_id });
  const payload = created?.data || created;
  return { id: payload?.id || payload?.folder?.id || null };
}

function uploadOneFile(file, folderId) {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('title', file.name);
    if (folderId) fd.append('folder_id', folderId);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/documents');

    const storedSlug = sessionStorage.getItem('gdx_tenant_slug');
    const hostParts = window.location.hostname.split('.');
    const subdomain = hostParts.length >= 3 ? hostParts[0] : null;
    const tenantId = storedSlug || (subdomain && subdomain !== 'www' ? subdomain : null);
    if (tenantId) xhr.setRequestHeader('x-tenant-id', tenantId);
    if (auth.accessToken) xhr.setRequestHeader('Authorization', `Bearer ${auth.accessToken}`);
    xhr.withCredentials = true;

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      else reject(new Error(`HTTP ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error('network error'));
    xhr.send(fd);
  });
}

async function runBulkUpload() {
  bulkUploading.value = true;
  bulkErrors.value = [];
  bulkProgress.value = { total: bulkFiles.value.length, done: 0, failed: 0, current: '' };

  await fetchFolders();
  const pathCache = new Map();
  const walkCtx = {
    cache: pathCache,
    folders: folders.value,
    createFolder: _createFolderApi,
    onError: (e) => bulkErrors.value.push(e),
  };

  for (const file of bulkFiles.value) {
    const segments = folderSegmentsFromFile(file);
    const folderId = segments.length ? await ensureFolderPath(segments, walkCtx) : null;
    const displayPath = file.webkitRelativePath || file.name;
    bulkProgress.value.current = displayPath;
    try {
      await uploadOneFile(file, folderId);
      bulkProgress.value.done += 1;
    } catch (err) {
      bulkProgress.value.failed += 1;
      bulkErrors.value.push({ path: displayPath, message: err.message || 'failed' });
    }
  }

  bulkUploading.value = false;
  bulkDone.value = true;
  bulkProgress.value.current = '';
  await fetchFolders();
  await fetchDocuments();

  toast.add({
    severity: bulkProgress.value.failed ? 'warn' : 'success',
    summary: 'Folder upload complete',
    detail: `${bulkProgress.value.done} uploaded, ${bulkProgress.value.failed} failed.`,
    life: 4000,
  });
}

async function fetchDocuments() {
  loading.value = true;
  loadError.value = '';
  try {
    const params = selectedFolder.value ? `?folder_id=${selectedFolder.value.id}` : '';
    const result = await api.get(`/api/documents${params}`);
    const payload = result?.data || result;
    documents.value = Array.isArray(payload) ? payload : payload?.items || [];
  } catch (e) {
    loadError.value = e.message || 'Failed to load documents';
    toast.add({
      severity: 'error',
      summary: 'Load Error',
      detail: loadError.value,
      life: 5000,
    });
  } finally {
    loading.value = false;
  }
}

async function fetchLinkData() {
  try {
    const [jobsResult, customersResult] = await Promise.all([
      api.get('/api/jobs').catch(() => []),
      api.get('/api/customers?per_page=500').catch(() => []),
    ]);
    const rawJobs = Array.isArray(jobsResult)
      ? jobsResult
      : jobsResult?.items || jobsResult?.data || [];
    const rawCustomers = Array.isArray(customersResult)
      ? customersResult
      : customersResult?.items || customersResult?.data || [];

    jobOptions.value = rawJobs.map((j) => ({
      label: `${j.job_number || j.jobNumber || ''} - ${j.customer_name || j.title || 'Job'}`.trim(),
      value: j.id,
    }));
    customerOptions.value = rawCustomers.map((c) => ({
      label: c.name,
      value: c.id,
    }));
  } catch {
    jobOptions.value = [];
    customerOptions.value = [];
  }
}

async function submitUpload() {
  uploadError.value = '';
  if (!uploadForm.value.file) {
    uploadError.value = 'Please select a file.';
    return;
  }
  if (uploadForm.value.file.size > MAX_FILE_SIZE) {
    uploadError.value = 'File exceeds 25MB limit.';
    return;
  }

  uploading.value = true;
  uploadProgress.value = 0;

  try {
    const formData = new FormData();
    formData.append('file', uploadForm.value.file);
    formData.append('title', uploadForm.value.name || uploadForm.value.file.name);
    if (uploadForm.value.category) formData.append('tags', uploadForm.value.category);
    if (uploadForm.value.notes) formData.append('description', uploadForm.value.notes);
    if (uploadForm.value.linkType === 'Job' && uploadForm.value.job_id) {
      formData.append('job_id', uploadForm.value.job_id);
    }
    if (uploadForm.value.linkType === 'Customer' && uploadForm.value.customer_id) {
      formData.append('customer_id', uploadForm.value.customer_id);
    }
    if (selectedFolder.value?.id) {
      formData.append('folder_id', selectedFolder.value.id);
    }

    // Use XMLHttpRequest for progress tracking
    await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/api/documents');

      const storedSlug = sessionStorage.getItem('gdx_tenant_slug');
      const hostParts = window.location.hostname.split('.');
      const subdomain = hostParts.length >= 3 ? hostParts[0] : null;
      const tenantId = storedSlug || (subdomain && subdomain !== 'www' ? subdomain : null);
      if (tenantId) xhr.setRequestHeader('x-tenant-id', tenantId);

      if (auth.accessToken) {
        xhr.setRequestHeader('Authorization', `Bearer ${auth.accessToken}`);
      }
      xhr.withCredentials = true;

      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) {
          uploadProgress.value = Math.round((e.loaded / e.total) * 100);
        }
      };

      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve();
        } else {
          reject(new Error(`Upload failed (${xhr.status})`));
        }
      };

      xhr.onerror = () => reject(new Error('Upload failed (network error)'));
      xhr.send(formData);
    });

    toast.add({
      severity: 'success',
      summary: 'Document Uploaded',
      detail: `"${uploadForm.value.name || uploadForm.value.file.name}" uploaded successfully.`,
      life: 3000,
    });
    showUploadDialog.value = false;
    resetUploadForm();
    await fetchDocuments();
  } catch (err) {
    uploadError.value = err.message || 'Upload failed.';
    toast.add({
      severity: 'error',
      summary: 'Upload Failed',
      detail: uploadError.value,
      life: 5000,
    });
  } finally {
    uploading.value = false;
  }
}

function openDeleteDialog(doc) {
  deleteTarget.value = doc;
  showDeleteDialog.value = true;
}

async function confirmDelete() {
  if (!deleteTarget.value?.id) return;
  deleting.value = true;
  try {
    await api.del(`/api/documents/${deleteTarget.value.id}`);
    toast.add({
      severity: 'success',
      summary: 'Document Deleted',
      detail: `"${deleteTarget.value.name || deleteTarget.value.filename}" deleted.`,
      life: 3000,
    });
    showDeleteDialog.value = false;
    deleteTarget.value = null;
    await fetchDocuments();
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: 'Delete Failed',
      detail: err.message || 'Failed to delete document.',
      life: 5000,
    });
  } finally {
    deleting.value = false;
  }
}

onMounted(() => {
  Promise.all([fetchDocuments(), fetchFolders(), fetchLinkData()]);
});
</script>

<style scoped>
.documents-view {
  max-width: 1400px;
}

.search-input {
  width: 250px;
}

.category-dropdown {
  min-width: 160px;
  margin-left: 0.5rem;
}

.link-dropdown {
  min-width: 160px;
  margin-left: 0.5rem;
}

.documents-table {
  margin-top: 1rem;
}

/* Drop zone */
.drop-zone {
  border: 2px dashed var(--surface-border, #dee2e6);
  border-radius: 8px;
  padding: 2rem;
  text-align: center;
  cursor: pointer;
  transition: border-color 0.2s, background 0.2s;
}

.drop-zone:hover {
  border-color: var(--p-primary-color);
  background: rgba(79, 195, 247, 0.04);
}

.drop-zone-active {
  border-color: var(--p-primary-color);
  background: rgba(79, 195, 247, 0.08);
}

.drop-zone-has-file {
  border-style: solid;
  border-color: var(--p-primary-color);
  background: rgba(79, 195, 247, 0.04);
  padding: 1rem;
}

.drop-zone-prompt p {
  margin: 0.5rem 0 0.25rem;
}

.drop-zone-selected {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.selected-file-info {
  flex: 1;
  text-align: left;
  display: flex;
  flex-direction: column;
}

.selected-file-info small {
  color: var(--p-text-muted-color);
}

.visually-hidden-file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
.upload-fallback {
  margin-top: 0.5rem;
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
}
.upload-fallback summary {
  cursor: pointer;
  user-select: none;
  padding: 0.25rem 0;
}
.upload-fallback[open] summary {
  margin-bottom: 0.4rem;
}
.native-file-input {
  display: block;
  margin: 0.25rem 0;
  padding: 0.25rem;
  font-size: 0.85rem;
  cursor: pointer;
}
.native-file-input::file-selector-button {
  margin-right: 0.75rem;
  padding: 0.5rem 1rem;
  border: 1px solid var(--p-primary-color);
  background: transparent;
  color: var(--p-primary-color);
  border-radius: 6px;
  cursor: pointer;
  font-size: 0.9rem;
}
.native-file-input::file-selector-button:hover {
  background: rgba(79, 195, 247, 0.08);
}

.upload-progress {
  margin-top: 0.5rem;
}

/* Preview */
.preview-content {
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 300px;
}

.preview-image {
  max-width: 100%;
  max-height: 70vh;
  border-radius: 4px;
}

.preview-pdf {
  width: 100%;
  height: 70vh;
  border: none;
  border-radius: 4px;
}

.preview-video {
  max-width: 100%;
  max-height: 70vh;
  border-radius: 4px;
  background: black;
}

.preview-audio {
  width: 100%;
}

.preview-text {
  width: 100%;
  max-height: 70vh;
  overflow: auto;
  margin: 0;
  padding: 1rem;
  background: var(--p-content-background, #fafafa);
  border: 1px solid var(--p-content-border-color, #e0e0e0);
  border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.85rem;
  white-space: pre-wrap;
  word-wrap: break-word;
}

.preview-unsupported {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.5rem;
  padding: 2rem;
  color: var(--p-text-muted-color);
  text-align: center;
}

/* Delete dialog */
.delete-confirm-content {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 0.5rem;
}

/* Links */
.doc-link {
  color: var(--p-primary-color, #4fc3f7);
  text-decoration: none;
  font-weight: 600;
}

.doc-link:hover {
  text-decoration: underline;
}

.linked-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  font-size: 0.85rem;
}

/* Empty state */
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  padding: 3rem 1rem;
  color: var(--p-text-muted-color);
}

.empty-state h3 {
  margin: 1rem 0 0.25rem;
  color: var(--text-color);
}

.empty-state p {
  margin: 0 0 1rem;
}

/* Common */
.spinner-wrap {
  display: flex;
  justify-content: center;
  margin: 2rem 0;
}

.inline-error {
  color: #b42318;
  margin: 0.5rem 0;
  display: flex;
  align-items: center;
  gap: 0.3rem;
}

.bulk-warn {
  color: #92400e;
  background: #fef3c7;
  padding: 0.5rem 0.75rem;
  border-radius: 0.4rem;
  margin: 0.5rem 0;
  display: flex;
  align-items: center;
  gap: 0.4rem;
}

.bulk-folders, .bulk-errors {
  margin: 0.75rem 0;
  font-size: 0.9rem;
}
.bulk-folders ul, .bulk-errors ul {
  max-height: 180px;
  overflow-y: auto;
  margin: 0.4rem 0 0;
  padding-left: 1.2rem;
}
.bulk-current {
  font-family: ui-monospace, monospace;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
  margin: 0.5rem 0 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.text-muted {
  color: var(--p-text-muted-color, #9e9e9e);
}

.hint {
  color: var(--p-text-muted-color, #888);
  font-size: 0.8rem;
}

.dialog-form {
  display: grid;
  gap: 0.75rem;
}

.form-field {
  display: grid;
  gap: 0.25rem;
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

.w-full {
  width: 100%;
}

.documents-view {
  display: flex;
  gap: 0;
  min-height: 0;
}

.folder-panel {
  width: 260px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--p-content-border-color, #334155);
  padding: 0.75rem 0.5rem;
  background: var(--p-content-background, #0f172a);
  border-radius: 8px 0 0 8px;
}

.folder-list {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  overflow-y: auto;
  min-height: 0;
}

/* "All Documents" row — visually matches FolderTreeNode rows. */
.folder-item--all {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.4rem 0.5rem;
  cursor: pointer;
  border-radius: 4px;
  font-size: 0.875rem;
  line-height: 1.25;
  color: var(--p-text-color, #e2e8f0);
  user-select: none;
  border-left: 3px solid transparent;
  margin-bottom: 0.25rem;
}

.folder-item--all:hover {
  background: var(--p-content-hover-background, rgba(255, 255, 255, 0.04));
}

.folder-item--all.active {
  background: rgba(16, 185, 129, 0.08);
  border-left-color: var(--p-primary-color, #10b981);
}

.folder-item--all.active .folder-name {
  color: var(--p-primary-color, #10b981);
  font-weight: 500;
}

.folder-item--all.drop-target {
  background: rgba(16, 185, 129, 0.18);
  outline: 1px dashed var(--p-primary-color, #10b981);
  outline-offset: -2px;
}

.folder-item--all .folder-toggle-spacer {
  display: inline-block;
  width: 1.1rem;
}

.folder-item--all .folder-icon {
  color: var(--p-primary-color, #10b981);
  opacity: 0.85;
  font-size: 0.95rem;
}

.folder-item--all .folder-name {
  flex: 1 1 auto;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.folder-empty {
  font-size: 0.78rem;
  color: var(--p-text-muted-color, #94a3b8);
  padding: 0.6rem 0.5rem;
  font-style: italic;
}

.folder-actions {
  padding: 0.6rem 0.25rem 0;
  border-top: 1px solid var(--p-content-border-color, #334155);
  margin-top: 0.5rem;
}

.inline-create {
  display: flex;
  gap: 0.25rem;
  align-items: center;
}

.documents-main {
  flex: 1;
  min-width: 0;
}

@media (max-width: 768px) {
  .documents-view { flex-direction: column; }
  .folder-panel { width: 100%; border-right: none; border-bottom: 1px solid var(--p-content-border-color); border-radius: 8px 8px 0 0; max-height: 300px; }
}
</style>
