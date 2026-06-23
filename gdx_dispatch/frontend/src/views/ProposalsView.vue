<template>
    <section class="proposals-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Proposals</h2>
        </template>
        <template #end>
          <Button label="+ New Proposal" icon="pi pi-plus" @click="openCreate" />
        </template>
      </Toolbar>

      <div class="proposals-toolbar-extra">
        <div class="toolbar-group">
          <label class="toolbar-label" for="proposal-template-select">Estimate Template</label>
          <Select
            id="proposal-template-select"
            :options="estimateTemplateOptions"
            optionLabel="label"
            optionValue="value"
            v-model="selectedTemplate"
            class="toolbar-select"
            data-testid="proposal-template-select"
          />
          <Button
            label="+ New Estimate"
            icon="pi pi-plus"
            severity="primary"
            data-testid="new-estimate-btn"
            @click="startEstimateFromTemplate"
          />
        </div>
        <div class="toolbar-group">
          <label class="toolbar-label" for="proposal-sort-select">Sort by</label>
          <Select
            id="proposal-sort-select"
            :options="sortOptions"
            optionLabel="label"
            optionValue="value"
            v-model="sortOption"
            class="toolbar-select"
            data-testid="proposal-sort-select"
          />
          <Button
            :label="dateSortLabel"
            icon="pi pi-sort-alt"
            class="p-button-text"
            size="small"
            :severity="isDateSortActive ? 'primary' : 'secondary'"
            data-testid="proposal-date-sort-toggle"
            @click="toggleDateSort"
          />
          <Button
            label="Email Selected"
            icon="pi pi-envelope"
            class="p-button-text"
            severity="info"
            :disabled="!selectedProposals.length"
            :loading="sendingBulk"
            data-testid="proposal-email-selected-btn"
            @click="sendSelectedProposals"
          />
        </div>
      </div>

      <div class="filter-tabs">
        <Button
          v-for="status in statusTabs"
          :key="status"
          :label="labelForStatus(status)"
          :severity="statusFilter === status ? undefined : 'secondary'"
          size="small"
          @click="statusFilter = status"
        />
      </div>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
      responsiveLayout="scroll"
        v-else
        :value="filteredProposals"
        dataKey="id"
        selectionMode="multiple"
        v-model:selection="selectedProposals"
        paginator
        :rows="20"
        striped-rows
        
        @row-click="openEdit($event.data)"
      >
        <Column selectionMode="multiple" style="width: 3rem" />
        <template #empty>
          <div class="empty-state">
            <i class="pi pi-file-edit" style="font-size:3rem; color:#64748b;"></i>
            <h3>No Proposals</h3>
            <p>Draft proposals to share pricing tiers with customers.</p>
            <Button label="+ Create First" @click="openCreate" />
          </div>
        </template>

        <Column field="title" header="Title" />
        <Column field="customer_name" header="Customer" />
        <Column header="Best Price" style="width:140px">
          <template #body="{ data }">{{ formatCurrency(data.best_price) }}</template>
        </Column>
        <Column header="Status" style="width:150px">
          <template #body="{ data }">
            <Badge :value="displayStatus(data.status)" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column header="Created" style="width:140px">
          <template #body="{ data }">{{ formatDate(data.created_at) }}</template>
        </Column>
        <Column header="Actions" style="width:140px">
          <template #body="{ data }">
            <Button
              label="Duplicate"
              icon="pi pi-clone"
              text
              size="small"
              :loading="duplicatingProposalId === data.id"
              :data-testid="`proposal-duplicate-${data.id}`"
              @click.stop="duplicateProposal(data)"
            />
          </template>
        </Column>
      </DataTable>

      <Dialog
        v-model:visible="showDialog"
        :modal="true"
        :header="editingProposal ? `Edit Proposal` : 'New Proposal'"
        :style="{ width: '650px' }"
      >
        <div class="form-grid">
          <div class="form-field full-width">
            <label>Proposal title *</label>
            <InputText v-model="form.title" class="w-full" placeholder="Proposal title" data-testid="proposal-title-input" />
          </div>
          <div class="form-field full-width">
            <label>Customer *</label>
            <InputText v-model="form.customer_name" class="w-full" placeholder="Customer name" data-testid="proposal-customer-input" />
          </div>
          <div class="form-field">
            <label>Price ($)</label>
            <InputText v-model="form.good_price" type="number" class="w-full" data-testid="proposal-price-input" />
          </div>
          <div class="form-field">
            <label>Good Description</label>
            <Textarea v-model="form.good_description" rows="2" class="w-full" />
          </div>
          <div class="form-field">
            <label>Better Price</label>
            <InputText v-model="form.better_price" type="number" class="w-full" />
          </div>
          <div class="form-field">
            <label>Better Description</label>
            <Textarea v-model="form.better_description" rows="2" class="w-full" />
          </div>
          <div class="form-field">
            <label>Best Price</label>
            <InputText v-model="form.best_price" type="number" class="w-full" />
          </div>
          <div class="form-field">
            <label>Best Description</label>
            <Textarea v-model="form.best_description" rows="2" class="w-full" />
          </div>
          <div class="form-field">
            <label>Default Tier</label>
            <Select v-model="form.chosen_tier" :options="tierOptions" class="w-full" />
          </div>
        </div>

        <div v-if="editingProposal" class="detail-actions">
          <Button
            label="Save Draft"
            icon="pi pi-save"
            severity="secondary"
            text
            data-testid="proposal-save-draft"
            @click="saveProposalDraft"
          />
          <Button
            label="Approve This Estimate"
            icon="pi pi-check-circle"
            severity="success"
            data-testid="proposal-approve"
            @click="approveProposal"
            v-if="editingProposal && editingProposal.status !== 'approved'"
          />
          <Button
            label="Print"
            icon="pi pi-print"
            class="p-button-text"
            data-testid="proposal-print"
            @click="printProposal"
          />
          <Button
            label="Convert to Job"
            icon="pi pi-briefcase"
            severity="success"
            :loading="converting"
            data-testid="proposal-convert-job"
            @click="convertToJob"
          />
        </div>

        <Divider v-if="editingProposal" />

        <section
          v-if="editingProposal"
          class="line-items-section"
          data-testid="proposal-line-items-section"
        >
          <div class="line-items-header">
            <h3>Line Items</h3>
          </div>
          <!-- D-S122-line-editor-proposals: migrated from a 100-line inline
               DataTable to the shared <LineItemEditor>. Line shape matches
               estimate/invoice. Tier system (good/better/best) is separate
               from line items — only line-shape was duplicated. -->
          <div v-if="lineItemsLoading" class="line-items-loading">
            <ProgressSpinner />
            <span>Loading line items…</span>
          </div>
          <LineItemEditor
            v-else
            v-model:lines="lineItems"
            :categories="lineCategoryOptions"
            data-testid="proposal-line-editor"
          />
          <div class="line-items-footer">
            <span>Subtotal</span>
            <strong>{{ formatCurrency(lineItemsSubtotal) }}</strong>
          </div>
        </section>

        <section
          v-if="editingProposal"
          class="notes-section"
          data-testid="proposal-notes-section"
        >
          <label>Estimate Notes</label>
          <Textarea
            v-model="notesDraft"
            rows="4"
            class="w-full"
            data-testid="proposal-notes"
            placeholder="Estimate notes visible to the customer"
          />
          <div class="notes-actions">
            <Button
              label="Save Notes"
              icon="pi pi-save"
              severity="secondary"
              :loading="savingNotes"
              data-testid="proposal-save-notes"
              @click="saveNotes"
            />
          </div>
        </section>

        <div v-if="editingProposal" class="status-actions">
          <div class="status-info">
            <Badge :value="displayStatus(editingProposal.status)" :severity="statusSeverity(editingProposal.status)" />
            <div class="dates">
              <p>Created: {{ formatDate(editingProposal.created_at) }}</p>
              <p v-if="editingProposal.sent_at">Sent: {{ formatDate(editingProposal.sent_at) }}</p>
              <p v-if="editingProposal.accepted_at">Accepted: {{ formatDate(editingProposal.accepted_at) }}</p>
            </div>
          </div>
          <div class="action-buttons">
            <Button
              label="Send"
              icon="pi pi-paper-plane"
              severity="primary"
              :disabled="!canSend"
              :loading="actionLoading"
              @click="sendProposal"
            />
            <Button
              label="Accept"
              icon="pi pi-check"
              severity="success"
              :disabled="!canAccept"
              :loading="actionLoading"
              @click="acceptProposal"
            />
            <Button
              label="Decline"
              icon="pi pi-times" aria-label="Remove"
              severity="danger"
              :disabled="!canDecline"
              :loading="actionLoading"
              @click="declineProposal"
            />
          </div>
        </div>

        <template #footer>
          <Button label="Cancel" severity="secondary" @click="closeDialog" />
          <Button
            :label="editingProposal ? 'Save Changes' : 'Create'"
            icon="pi pi-check"
            :loading="saving || lineItemsSaving"
            data-testid="proposal-save-changes"
            @click="saveProposal"
          />
        </template>
      </Dialog>
      <Toast data-testid="proposal-toast" />
    </section>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue';
import { useToast } from 'primevue/usetoast';
import { useApiWithToast } from '../composables/useApiWithToast';
import { openAuthedFile } from '../composables/useAuthedFile';
import Badge from 'primevue/badge';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Textarea from 'primevue/textarea';
import Divider from 'primevue/divider';
import Toolbar from 'primevue/toolbar';
import Toast from 'primevue/toast';
import LineItemEditor from '../components/LineItemEditor.vue';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();
const toast = useToast();
const proposals = ref([]);
const loading = ref(true);
const saving = ref(false);
const actionLoading = ref(false);
const showDialog = ref(false);
const editingProposal = ref(null);
const statusFilter = ref('All');
const selectedProposals = ref([]);
const sendingBulk = ref(false);
const duplicatingProposalId = ref(null);

const estimateTemplateOptions = [
  { label: 'Blank Estimate', value: 'blank', description: 'Start from scratch with a clean proposal.' },
  { label: 'Premium Upgrade', value: 'premium', description: 'Showcases premium door and opener packages.' },
  { label: 'Service Agreement', value: 'service', description: 'Recurring maintenance + inspection bundle.' },
];
const selectedTemplate = ref(estimateTemplateOptions[0].value);

const currentTemplate = computed(() => {
  return (
    estimateTemplateOptions.find((option) => option.value === selectedTemplate.value) ||
    estimateTemplateOptions[0]
  );
});

const sortOptions = [
  { label: 'Created (newest)', value: 'created_desc' },
  { label: 'Created (oldest)', value: 'created_asc' },
  { label: 'Best price (high→low)', value: 'best_price_desc' },
  { label: 'Best price (low→high)', value: 'best_price_asc' },
];
const sortOption = ref(sortOptions[0].value);
const dateSortDirection = ref('desc');
const dateSortLabel = computed(() => (dateSortDirection.value === 'desc' ? 'Date ↓' : 'Date ↑'));
const isDateSortActive = computed(() => sortOption.value?.startsWith('created_'));

const statusTabs = ['All', 'Draft', 'Sent', 'Approved', 'Declined', 'Converted'];
const tierOptions = [
  { label: 'Good', value: 'good' },
  { label: 'Better', value: 'better' },
  { label: 'Best', value: 'best' },
];

const lineCategoryOptions = [
  { label: 'Labor', value: 'labor' },
  { label: 'Materials', value: 'materials' },
  { label: 'Parts', value: 'parts' },
  { label: 'Other', value: 'other' },
];

const emptyForm = () => ({
  title: '',
  customer_name: '',
  description: '',
  good_price: '',
  better_price: '',
  best_price: '',
  good_description: '',
  better_description: '',
  best_description: '',
  chosen_tier: 'best',
});
const form = ref(emptyForm());

const notesDraft = ref('');
const savingNotes = ref(false);
const lineItems = ref([]);
const lineItemsLoading = ref(false);
const lineItemsSaving = ref(false);
const converting = ref(false);
let nextLineTempId = 1;

watch(notesDraft, (value) => {
  form.value.description = value;
});

watch(sortOption, (value) => {
  if (value?.startsWith('created_')) {
    const [, direction] = value.split('_');
    if (direction === 'asc' || direction === 'desc') {
      dateSortDirection.value = direction;
    }
  }
});

function capitalize(s) {
  if (!s) return "";
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}

function normalizeProposalStatus(status) {
  const normalized = (status || '').toString().trim().toLowerCase();
  if (!normalized) return 'Draft';
  if (['approved', 'accepted'].includes(normalized)) return 'Approved';
  if (normalized === 'converted') return 'Converted';
  if (normalized === 'declined') return 'Declined';
  if (normalized === 'sent') return 'Sent';
  if (normalized === 'draft') return 'Draft';
  return capitalize(normalized);
}

const counts = computed(() => {
  const tally = { All: proposals.value.length };
  statusTabs.forEach((tab) => {
    if (tab !== 'All') {
      tally[tab] = 0;
    }
  });
  proposals.value.forEach((proposal) => {
    const normalized = normalizeProposalStatus(proposal.status);
    if (tally[normalized] !== undefined) {
      tally[normalized] += 1;
    }
  });
  return tally;
});

const filteredProposals = computed(() => {
  const baseList =
    statusFilter.value === 'All'
      ? proposals.value
      : proposals.value.filter(
          (proposal) => normalizeProposalStatus(proposal.status) === statusFilter.value,
        );
  return sortProposals(baseList);
});

function parseProposalDate(value) {
  const timestamp = Date.parse(value || "");
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function sortProposals(list) {
  const sorted = [...list];
  const option = sortOption.value;
  sorted.sort((a, b) => {
    if (option === "created_asc") {
      return parseProposalDate(a.created_at) - parseProposalDate(b.created_at);
    }
    if (option === "created_desc") {
      return parseProposalDate(b.created_at) - parseProposalDate(a.created_at);
    }
    if (option === "best_price_asc") {
      return (Number(a.best_price) || 0) - (Number(b.best_price) || 0);
    }
    if (option === "best_price_desc") {
      return (Number(b.best_price) || 0) - (Number(a.best_price) || 0);
    }
    return 0;
  });
  return sorted;
}

function toggleDateSort() {
  const nextDirection = dateSortDirection.value === 'desc' ? 'asc' : 'desc';
  dateSortDirection.value = nextDirection;
  sortOption.value = `created_${nextDirection}`;
}

const lineItemsSubtotal = computed(() =>
  lineItems.value.reduce(
    (sum, item) => sum + (Number(item.quantity) || 0) * (Number(item.unit_price) || 0),
    0,
  ),
);

const statusSeverity = (status) => {
  const normalized = normalizeProposalStatus(status);
  return {
    Draft: 'secondary',
    Sent: 'info',
    Approved: 'success',
    Converted: 'success',
    Declined: 'danger',
  }[normalized] || 'secondary';
};

const canSend = computed(() => editingProposal.value && normalizeProposalStatus(editingProposal.value.status) === 'Draft');
const canAccept = computed(() => editingProposal.value && normalizeProposalStatus(editingProposal.value.status) === 'Sent');
const canDecline = computed(() => editingProposal.value && normalizeProposalStatus(editingProposal.value.status) === 'Sent');

function labelForStatus(status) {
  const count = counts.value[status];
  return count ? `${status} (${count})` : status;
}

function displayStatus(status) {
  return status ? status.replace('_', ' ') : '—';
}

function formatCurrency(value) {
  if (value === null || value === undefined || value === '') return '—';
  return `$${Number(value).toFixed(2)}`;
}

function formatDate(value) {
  if (!value) return '—';
  return value.split('T')[0];
}

function createLineTempId() {
  return `line-${nextLineTempId++}`;
}

function blankLineItem() {
  return {
    id: null,
    tempId: createLineTempId(),
    category: lineCategoryOptions[0]?.value || '',
    description: '',
    quantity: 1,
    unit_price: 0,
  };
}

function resetLineItems() {
  nextLineTempId = 1;
  lineItems.value = [blankLineItem()];
}

function normalizeLineItems(payload) {
  const items = Array.isArray(payload?.line_items)
    ? payload.line_items
    : Array.isArray(payload)
      ? payload
      : payload?.items || [];
  if (!items.length) {
    return [blankLineItem()];
  }
  return items.map((entry) => ({
    id: entry.id ?? null,
    tempId: entry.tempId || entry.id || createLineTempId(),
    category: entry.category || lineCategoryOptions[0]?.value || '',
    description: entry.description || '',
    quantity: entry.quantity ?? 1,
    unit_price: entry.unit_price ?? 0,
  }));
}

async function loadLineItems(proposalId) {
  if (!proposalId) {
    resetLineItems();
    return;
  }
  lineItemsLoading.value = true;
  try {
    const payload = await api.get(`/api/proposals/${proposalId}/line-items`);
    lineItems.value = normalizeLineItems(payload);
  } catch (error) {
    resetLineItems();
  } finally {
    lineItemsLoading.value = false;
  }
}

function addLineItem() {
  lineItems.value.push(blankLineItem());
}

function copyLineItem(source) {
  const clone = {
    id: null,
    tempId: createLineTempId(),
    category: source.category || lineCategoryOptions[0]?.value || '',
    description: source.description || '',
    quantity: source.quantity ?? 1,
    unit_price: source.unit_price ?? 0,
  };
  lineItems.value.push(clone);
}

function deleteLineItem(index) {
  if (index < 0 || index >= lineItems.value.length) return;
  lineItems.value.splice(index, 1);
  if (!lineItems.value.length) {
    lineItems.value.push(blankLineItem());
  }
}

function getLineTotal(item) {
  const qty = Number(item.quantity) || 0;
  const price = Number(item.unit_price) || 0;
  return qty * price;
}

function sanitizeLineItemsForSave() {
  return lineItems.value.map((item) => ({
    id: item.id,
    category: item.category,
    description: item.description,
    quantity: Number(item.quantity) || 0,
    unit_price: Number(item.unit_price) || 0,
  }));
}

async function saveLineItems() {
  if (!editingProposal.value) return;
  lineItemsSaving.value = true;
  try {
    const payload = { line_items: sanitizeLineItemsForSave() };
    await api.patch(
      `/api/proposals/${editingProposal.value.id}/line-items`,
      payload,
      { successMessage: 'Line items saved' },
    );
  } catch (err) {
    console.error('save_line_items_failed', err?.message || err);
  } finally {
    lineItemsSaving.value = false;
  }
}

async function saveNotes() {
  if (!editingProposal.value) return;
  savingNotes.value = true;
  try {
    await api.patch(
      `/api/proposals/${editingProposal.value.id}`,
      { description: notesDraft.value },
      { successMessage: 'Notes saved' },
    );
    editingProposal.value.description = notesDraft.value;
    form.value.description = notesDraft.value;
  } catch (err) {
    console.error('save_proposal_notes_failed', err?.message || err);
  } finally {
    savingNotes.value = false;
  }
}

async function printProposal() {
  if (!editingProposal.value) return;
  const pdfUrl = `/api/proposals/${editingProposal.value.id}/pdf`;
  try {
    await openAuthedFile(pdfUrl);
  } catch (e) {
    console.error('proposal_pdf_failed', e);
    toast.add({
      severity: 'error',
      summary: 'PDF failed',
      detail: e?.message || 'Could not open proposal PDF',
      life: 5000,
    });
  }
}

async function convertToJob() {
  if (!editingProposal.value) return;
  converting.value = true;
  try {
    await api.post(
      `/api/proposals/${editingProposal.value.id}/convert-to-job`,
      {},
      { successMessage: 'Proposal converted to job' },
    );
    closeDialog();
    await loadProposals();
  } catch (err) {
    console.error('convert_proposal_to_job_failed', err?.message || err);
  } finally {
    converting.value = false;
  }
}

function closeDialog() {
  showDialog.value = false;
  editingProposal.value = null;
  form.value = emptyForm();
  notesDraft.value = '';
  resetLineItems();
}

function openCreate() {
  editingProposal.value = null;
  form.value = emptyForm();
  notesDraft.value = '';
  resetLineItems();
  showDialog.value = true;
}

function startEstimateFromTemplate() {
  editingProposal.value = null;
  form.value = {
    ...emptyForm(),
    title: `${currentTemplate.value.label} Estimate`,
    description: currentTemplate.value.description || '',
  };
  notesDraft.value = form.value.description;
  resetLineItems();
  showDialog.value = true;
}

async function openEdit(proposal) {
  editingProposal.value = proposal;
  const descriptionValue = proposal.description || '';
  form.value = {
    title: proposal.title || '',
    customer_name: proposal.customer_name || '',
    description: descriptionValue,
    good_price: proposal.good_price ?? '',
    better_price: proposal.better_price ?? '',
    best_price: proposal.best_price ?? '',
    good_description: proposal.good_description || '',
    better_description: proposal.better_description || '',
    best_description: proposal.best_description || '',
    chosen_tier: proposal.chosen_tier || 'best',
  };
  notesDraft.value = descriptionValue;
  showDialog.value = true;
  await loadLineItems(proposal.id);
}

async function loadProposals() {
  loading.value = true;
  try {
    const data = await api.get('/api/proposals');
    proposals.value = Array.isArray(data) ? data : data?.items || [];
    selectedProposals.value = [];
  } catch (err) {
    console.error('load_proposals_failed', err?.message || err);
    proposals.value = [];
  } finally {
    loading.value = false;
  }
}

async function sendSelectedProposals() {
  const toSend = [...selectedProposals.value];
  if (!toSend.length) return;
  sendingBulk.value = true;
  try {
    await Promise.all(
      toSend.map((proposal) => api.post(`/api/proposals/${proposal.id}/send`))
    );
    toast.add({
      severity: 'success',
      summary: 'Proposals Sent',
      detail: `${toSend.length} proposal${toSend.length === 1 ? '' : 's'} emailed.`,
      life: 3000,
    });
    selectedProposals.value = [];
    await loadProposals();
  } catch (err) {
    console.error('send_selected_proposals_failed', err?.message || err);
  } finally {
    sendingBulk.value = false;
  }
}

async function duplicateProposal(proposal) {
  if (!proposal || duplicatingProposalId.value) return;
  duplicatingProposalId.value = proposal.id;
  try {
    const payload = {
      customer_id: proposal.customer_id || proposal.customer?.id || null,
      title: proposal.title ? `Copy of ${proposal.title}` : 'Copy of Proposal',
      description: proposal.description || '',
      good_price: proposal.good_price ?? 0,
      better_price: proposal.better_price ?? 0,
      best_price: proposal.best_price ?? 0,
      good_description: proposal.good_description || '',
      better_description: proposal.better_description || '',
      best_description: proposal.best_description || '',
      chosen_tier: proposal.chosen_tier || 'best',
    };
    await api.post('/api/proposals', payload, { successMessage: 'Proposal duplicated' });
    await loadProposals();
  } catch (err) {
    console.error('duplicate_proposal_failed', err?.message || err);
  } finally {
    duplicatingProposalId.value = null;
  }
}

async function saveProposalDraft() {
  if (!editingProposal.value) return;
  try {
    await api.patch(`/api/proposals/${editingProposal.value.id}`, { status: 'draft' }, { successMessage: 'Draft saved' });
    await loadProposals();
  } catch (err) {
    // toast handled by useApiWithToast
  }
}

async function approveProposal() {
  if (!editingProposal.value) return;
  try {
    await api.post(`/api/proposals/${editingProposal.value.id}/approve`, {}, { successMessage: 'Estimate approved' });
    editingProposal.value.status = 'approved';
    await loadProposals();
  } catch (err) {
    // toast handled by useApiWithToast
  }
}

async function saveProposal() {
  if (!form.value.title.trim() || !form.value.customer_name.trim()) return;
  saving.value = true;
  try {
    form.value.description = notesDraft.value;
    const payload = {
      title: form.value.title,
      customer_name: form.value.customer_name,
      description: form.value.description,
      good_price: form.value.good_price,
      better_price: form.value.better_price,
      best_price: form.value.best_price,
      good_description: form.value.good_description,
      better_description: form.value.better_description,
      best_description: form.value.best_description,
      chosen_tier: form.value.chosen_tier,
    };

    if (editingProposal.value) {
      await api.patch(`/api/proposals/${editingProposal.value.id}`, payload);
      await saveLineItems();
    } else {
      await api.post('/api/proposals', payload);
    }

    closeDialog();
    await loadProposals();
  } catch (err) {
    console.error('save_proposal_failed', err?.message || err);
  } finally {
    saving.value = false;
  }
}

async function sendProposal() {
  if (!editingProposal.value) return;
  actionLoading.value = true;
  try {
    await api.post(`/api/proposals/${editingProposal.value.id}/send`);
    closeDialog();
    await loadProposals();
  } catch (err) {
    console.error('send_proposal_failed', err?.message || err);
  } finally {
    actionLoading.value = false;
  }
}

async function acceptProposal() {
  if (!editingProposal.value) return;
  actionLoading.value = true;
  try {
    await api.post(`/api/proposals/${editingProposal.value.id}/accept`, { tier: form.value.chosen_tier || 'best' });
    closeDialog();
    await loadProposals();
  } catch (err) {
    console.error('accept_proposal_failed', err?.message || err);
  } finally {
    actionLoading.value = false;
  }
}

async function declineProposal() {
  if (!editingProposal.value) return;
  if (!(await confirmAsync({ header: 'Confirm', message: 'Decline this proposal?' }))) return;
  actionLoading.value = true;
  try {
    await api.post(`/api/proposals/${editingProposal.value.id}/decline`);
    closeDialog();
    await loadProposals();
  } catch (err) {
    console.error('decline_proposal_failed', err?.message || err);
  } finally {
    actionLoading.value = false;
  }
}

onMounted(loadProposals);
</script>

<style scoped>
.page-title {
  margin: 0;
}
.proposals-view {
  display: flex;
  flex-direction: column;
}
.filter-tabs {
  display: flex;
  gap: 0.5rem;
  margin: 1rem 0;
  flex-wrap: wrap;
}
.proposals-toolbar-extra {
  display: flex;
  flex-wrap: wrap;
  gap: 1rem;
  align-items: center;
  justify-content: space-between;
  margin-top: 1rem;
}
.toolbar-group {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
}
.toolbar-group .toolbar-label {
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--p-text-muted-color);
}
.toolbar-select {
  min-width: 180px;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 3rem;
}
.clickable-row {
  cursor: pointer;
}
.empty-state {
  text-align: center;
  padding: 3rem;
  color: var(--p-text-muted-color);
}
.empty-state h3 {
  margin: 1rem 0 0.5rem;
  color: var(--text-color);
}
.form-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.full-width {
  grid-column: 1 / -1;
}
.w-full {
  width: 100%;
}
.status-actions {
  margin-top: 1rem;
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  align-items: center;
}
.status-info {
  display: flex;
  gap: 1rem;
  align-items: center;
}
.status-info .dates {
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
}
.action-buttons {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.detail-actions {
  margin-top: 1rem;
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.line-items-section {
  margin-top: 1.5rem;
  border-top: 1px solid var(--surface-border, #d0d7de);
  padding-top: 1rem;
}
.line-items-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.5rem;
}
.line-items-frame {
  border: 1px solid var(--surface-border, #d0d7de);
  border-radius: 0.75rem;
  padding: 0.5rem;
  background: var(--surface-ground, #fafbfc);
}
.line-items-loading {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  color: var(--p-text-muted-color);
  padding: 0.25rem 0.5rem;
}
.line-items-table .p-datatable-tbody td {
  padding: 0.35rem 0.4rem;
}
.line-items-select,
.line-items-desc,
.line-items-number {
  width: 100%;
}
.line-items-desc {
  min-width: 200px;
}
.line-items-footer {
  display: flex;
  justify-content: flex-end;
  gap: 1rem;
  margin-top: 0.5rem;
  font-weight: 600;
}
.line-items-empty {
  padding: 0.5rem;
  color: var(--p-text-muted-color);
}
.notes-section {
  margin-top: 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.notes-actions {
  display: flex;
  justify-content: flex-end;
}
</style>
