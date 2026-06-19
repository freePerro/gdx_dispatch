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

      <div class="estimate-builder" data-testid="customer-estimate-builder">
        <div class="estimate-builder__header">
          <div>
            <h3>Customer Estimate Builder</h3>
            <p class="estimate-builder__description">
              Configure one or more garage door openings exactly like the public portal calculator and fetch a live CHI price.
            </p>
          </div>
          <Button
            label="+ Add Another Door"
            icon="pi pi-plus"
            class="p-button-text"
            severity="success"
            :disabled="builderLoading"
            @click="appendDoor"
            data-testid="add-door-btn"
          />
        </div>

        <div
          v-for="(door, index) in doorConfigs"
          :key="door.id"
          class="door-card"
          :data-testid="`door-config-${index}`"
        >
          <div class="door-card__header">
            <div>
              <p class="door-card__label">Door {{ index + 1 }}</p>
              <p class="door-card__subtitle">Opening {{ door.widthFt }}' × {{ door.heightFt }}' · Qty {{ door.quantity }}</p>
            </div>
            <Button
              v-if="doorConfigs.length > 1"
              icon="pi pi-trash" aria-label="Delete"
              class="p-button-text"
              severity="danger"
              text
              @click="removeDoor(index)"
              data-testid="remove-door-btn"
            />
          </div>

          <div class="form-grid builder-grid">
          <div class="form-field">
            <label>Door Type</label>
            <Select
              v-model="door.doorType"
              :options="doorTypeOptions"
              optionLabel="label"
              class="w-full"
              @change="() => onDoorTypeChange(door)"
              :data-testid="`door-${door.id}-door-type`"
            />
          </div>
          <div class="form-field">
            <label>Opening Size</label>
            <Select
              v-model="door.openingSize"
              :options="openingSizeOptionsFor(door.doorType)"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              @change="() => onOpeningSizeChange(door)"
              :data-testid="`door-${door.id}-opening-size`"
            />
          </div>
          <div class="form-field">
            <label>Collection</label>
            <Select
              v-model="door.collection"
              :options="collectionOptionsFor(door.doorType)"
              optionLabel="label"
              class="w-full"
              :data-testid="`door-${door.id}-collection`"
            />
          </div>
          <div class="form-field">
            <label>Model</label>
            <Select
              v-model="door.model"
              :options="modelOptions"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              :data-testid="`door-${door.id}-model`"
            />
          </div>
          <div class="form-field">
            <label>Color</label>
            <Select
              v-model="door.color"
              :options="colorOptions"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              :data-testid="`door-${door.id}-color`"
            />
          </div>
          <div class="form-field">
            <label>Width</label>
            <div class="dimension-row">
              <div class="dimension-stack">
                <label class="dimension-sub-label">Width (ft)</label>
                <InputNumber
                  v-model.number="door.widthFt"
                  :min="1"
                  :max="25"
                  :step="1"
                  size="4"
                  class="dimension-input"
                  placeholder="ft"
                  :data-testid="`door-${door.id}-width-ft`"
                />
              </div>
              <div class="dimension-stack">
                <label class="dimension-sub-label">Width (in)</label>
                <InputNumber
                  v-model.number="door.widthIn"
                  :min="0"
                  :max="11"
                  :step="1"
                  size="3"
                  class="dimension-input"
                  placeholder="in"
                  :data-testid="`door-${door.id}-width-in`"
                />
              </div>
            </div>
          </div>
          <div class="form-field">
            <label>Height</label>
            <div class="dimension-row">
              <div class="dimension-stack">
                <label class="dimension-sub-label">Height (ft)</label>
                <InputNumber
                  v-model.number="door.heightFt"
                  :min="6"
                  :max="20"
                  :step="1"
                  size="4"
                  class="dimension-input"
                  placeholder="ft"
                  :data-testid="`door-${door.id}-height-ft`"
                />
              </div>
              <div class="dimension-stack">
                <label class="dimension-sub-label">Height (in)</label>
                <InputNumber
                  v-model.number="door.heightIn"
                  :min="0"
                  :max="11"
                  :step="1"
                  size="3"
                  class="dimension-input"
                  placeholder="in"
                  :data-testid="`door-${door.id}-height-in`"
                />
              </div>
            </div>
          </div>
            <div class="form-field">
              <label>Quantity</label>
              <InputNumber
                v-model.number="door.quantity"
                :min="1"
                :max="10"
                :step="1"
                class="w-full"
                :data-testid="`door-${door.id}-quantity`"
              />
            </div>
            <div class="form-field">
              <label>Cyclage</label>
              <Select
                v-model="door.cyclage"
                :options="cyclageOptions"
                optionLabel="label"
                class="w-full"
                :data-testid="`door-${door.id}-cyclage`"
              />
            </div>
            <div class="form-field">
              <label>Spring</label>
              <Select
                v-model="door.spring"
                :options="springOptions"
                optionLabel="label"
                class="w-full"
                :data-testid="`door-${door.id}-spring`"
              />
            </div>
            <div class="form-field">
              <label>Track Type</label>
              <Select
                v-model="door.track"
                :options="trackOptions"
                optionLabel="label"
                class="w-full"
                :data-testid="`door-${door.id}-track`"
              />
            </div>
            <div v-if="shouldShowTrackRadius(door.track)" class="form-field">
              <label>Track Radius</label>
              <Select
                v-model="door.trackRadius"
                :options="trackRadiusOptions"
                optionLabel="label"
                class="w-full"
                :data-testid="`door-${door.id}-track-radius`"
              />
            </div>
            <div class="form-field">
              <label>Panel Length</label>
              <Select
                v-model="door.panelLength"
                :options="panelLengthOptionsFor(door)"
                optionLabel="label"
                class="w-full"
                :data-testid="`door-${door.id}-panel-length`"
              />
            </div>
            <div class="form-field">
              <label>Jamb Mount</label>
              <div class="jamb-mount-row">
                <ToggleSwitch
                  :model-value="door.jambMountFlag"
                  onLabel="Flag bracket"
                  offLabel="Angle mount"
                  @change="(value) => (door.jambMountFlag = value)"
                  :data-testid="`door-${door.id}-jamb-mount-toggle`"
                />
                <Select
                  v-model="door.jambMountStyle"
                  :options="jambMountOptions"
                  optionLabel="label"
                  class="w-full"
                  :data-testid="`door-${door.id}-jamb-mount-style`"
                />
              </div>
            </div>
          <div class="form-field">
            <label>Lock</label>
            <div class="lock-row">
              <ToggleSwitch
                :model-value="door.lockEnabled"
                onLabel="With lock"
                offLabel="No lock"
                @change="(value) => { door.lockEnabled = value; if (!value) door.lockType = 'Omit Lock'; }"
                :data-testid="`door-${door.id}-lock-toggle`"
              />
              <Select
                v-model="door.lockType"
                :options="lockOptions"
                optionLabel="label"
                class="w-full"
                :disabled="!door.lockEnabled"
                :data-testid="`door-${door.id}-lock-type`"
              />
            </div>
          </div>
        </div>
        <div class="door-card__decoration">
          <Button
            label="Add Windows to this door"
            icon="pi pi-image"
            class="p-button-text"
            :severity="door.windowsRequested ? 'success' : 'info'"
            @click="toggleDoorWindows(door)"
            :data-testid="`door-${door.id}-add-windows-btn`"
          />
          <p class="door-card__decoration-note">
            {{ door.windowsRequested ? 'Window placement saved' : 'Request decorative panels after pricing' }}
          </p>
        </div>
      </div>

        <div class="estimate-builder__windows">
          <ToggleSwitch
            v-model="windowsEnabled"
            onLabel="Add windows"
            offLabel="Skip windows"
            data-testid="window-toggle"
          />
          <p class="estimate-builder__windows-note">
            Windows will be requested after the core door selection is priced.
          </p>
        </div>

        <div class="portal-parity-helper" aria-hidden="true">
          <button type="button">' + (cat.name || 'category ' + (i+1)) + '</button>
          <button type="button">' + displaytext + '</button>
          <label>' + ' ' + label + '</label>
        </div>

        <div class="builder-actions">
          <Button
            label="Apply Windows & Get Final Price"
            icon="pi pi-arrow-circle-right"
            severity="primary"
            :loading="builderLoading"
            :disabled="builderLoading"
            @click="applyWindows"
            data-testid="apply-windows-btn"
          />
          <Button
            label="Get My Price"
            icon="pi pi-dollar"
            severity="success"
            :loading="builderLoading"
            :disabled="builderLoading"
            @click="getPrice"
            data-testid="get-price-btn"
          />
        </div>

        <div v-if="builderResult" class="result-panel" data-testid="estimate-result">
          <div class="result-panel__header">
            <div>
              <p class="result-panel__label">{{ builderActionLabel }}</p>
              <p class="result-panel__description">{{ builderResult.description || 'Door configuration processed' }}</p>
            </div>
            <div class="result-panel__amount">{{ formatCurrency(builderResult.price) }}</div>
          </div>
          <ul class="result-panel__list">
            <li v-for="(summary, idx) in builderResult.doorSummaries" :key="idx">
              {{ summary }}
            </li>
          </ul>
          <div class="result-panel__actions">
            <Button
              label="Save This Estimate"
              icon="pi pi-save"
              severity="info"
              :loading="builderSaving"
              :disabled="builderSaving"
              @click="saveBuilderEstimate"
              data-testid="save-estimate-btn"
            />
            <Button
              label="Dismiss"
              severity="secondary"
              text
              @click="dismissBuilderResult"
              data-testid="dismiss-btn"
            />
          </div>
        </div>
      </div>

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
import InputNumber from 'primevue/inputnumber';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import ToggleSwitch from 'primevue/toggleswitch';
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

const doorTypeOptions = [
  { label: 'Steel', value: 'Steel' },
  { label: 'Insulated Steel', value: 'Insulated Steel' },
  { label: 'Wood', value: 'Wood' },
  { label: 'Composite', value: 'Composite' },
  { label: 'Glass', value: 'Glass' },
  { label: 'Aluminum', value: 'Aluminum' },
];

const doorCollectionsByType = {
  Steel: ['Classic', 'Modern', 'Premium', 'Carriage House'],
  'Insulated Steel': ['Classic Insulated', 'Modern Energy', 'Premium R-Value'],
  Wood: ['Carriage House', 'Classic Wood', 'Rustic'],
  Composite: ['Classic Composite', 'Modern Composite', 'Timeless Composite'],
  Glass: ['Full View', 'Contemporary', 'Modern Glass'],
  Aluminum: ['Full View Aluminum', 'Commercial Aluminum'],
};
const commercialDoorTypes = new Set(['Aluminum']);

const openingSizePresets = {
  Residential: [
    { label: "8' × 7'", value: '8x7', widthFt: 8, heightFt: 7 },
    { label: "9' × 7'", value: '9x7', widthFt: 9, heightFt: 7 },
    { label: "9' × 8'", value: '9x8', widthFt: 9, heightFt: 8 },
    { label: "10' × 8'", value: '10x8', widthFt: 10, heightFt: 8 },
    { label: "16' × 7'", value: '16x7', widthFt: 16, heightFt: 7 },
    { label: "16' × 8'", value: '16x8', widthFt: 16, heightFt: 8 },
    { label: "18' × 7'", value: '18x7', widthFt: 18, heightFt: 7 },
  ],
  Commercial: [
    { label: `9'2" × 10'`, value: '9x10', widthFt: 9, widthIn: 2, heightFt: 10 },
    { label: `10' × 10'`, value: '10x10', widthFt: 10, heightFt: 10 },
    { label: `10'2" × 10'`, value: '10x10c', widthFt: 10, widthIn: 2, heightFt: 10 },
    { label: `12' × 12'`, value: '12x12', widthFt: 12, heightFt: 12 },
    { label: `12'2" × 12'`, value: '12x12c', widthFt: 12, widthIn: 2, heightFt: 12 },
    { label: `12'2" × 14'`, value: '12x14', widthFt: 12, widthIn: 2, heightFt: 14 },
    { label: `14'2" × 14'`, value: '14x14', widthFt: 14, widthIn: 2, heightFt: 14 },
  ],
};
const residentialPanelLengthOptions = [
  { label: 'Short Panel', value: 'Short Panel' },
  { label: 'Long Panel', value: 'Long Panel' },
];
const commercialPanelLengthOptions = [
  { label: 'Micro-grooved', value: 'Micro-grooved' },
  { label: 'Flush', value: 'Flush' },
];

const modelOptions = [
  { label: 'Auto (best match)', value: '' },
  { label: '2216 — Polyurethane sandwich', value: '2216' },
  { label: '4206 — Polystyrene sandwich', value: '4206' },
  { label: '3295 — Non-insulated aluminum full-view', value: '3295' },
];

const colorOptions = [
  { label: 'Default (White)', value: '' },
  { label: 'White', value: 'White' },
  { label: 'Almond', value: 'Almond' },
  { label: 'Sandstone', value: 'Sandstone' },
  { label: 'Brown', value: 'Brown' },
  { label: 'Bronze', value: 'Bronze' },
  { label: 'Gray', value: 'Gray' },
  { label: 'Desert Tan', value: 'Desert Tan' },
  { label: 'Black', value: 'Black' },
  { label: 'Graphite', value: 'Graphite' },
  { label: 'Evergreen', value: 'Evergreen' },
];

const jambMountOptions = [
  { label: 'Clip Angle', value: 'Clip Angle' },
  { label: 'Full Angle', value: 'Full Angle' },
  { label: 'Reverse Clip Angle', value: 'Reverse Clip Angle' },
  { label: 'Reverse Full Angle', value: 'Reverse Full Angle' },
];

const cyclageOptions = [
  { label: '10k cycles', value: '10k cycles' },
  { label: '25k cycles', value: '25k cycles' },
  { label: '50k cycles', value: '50k cycles' },
  { label: '100k cycles', value: '100k cycles' },
];

const springOptions = [
  { label: 'Torsion', value: 'Torsion' },
  { label: 'Extension', value: 'Extension' },
  { label: 'High-Cycle Torsion', value: 'High-Cycle Torsion' },
];

const trackOptions = [
  { label: 'Standard Lift', value: 'Standard Lift' },
  { label: 'High Lift', value: 'High Lift' },
  { label: 'Vertical Lift', value: 'Vertical Lift' },
  { label: 'Low Headroom', value: 'Low Headroom' },
];

const trackRadiusOptions = [
  { label: '10" radius', value: '10" radius' },
  { label: '12" radius', value: '12" radius' },
  { label: '15" radius', value: '15" radius' },
  { label: '20" radius', value: '20" radius' },
  { label: '32" radius', value: '32" radius' },
];

const lockOptions = [
  { label: 'Omit Lock', value: 'Omit Lock' },
  { label: 'Inside Slide Lock', value: 'Inside Slide Lock' },
  { label: '2 Inside Slide Locks', value: '2 Inside Slide Locks' },
  { label: 'Outside Center Lock', value: 'Outside Center Lock' },
  { label: 'Double Lock Bar', value: 'Double Lock Bar' },
];

const trackRadiusTriggers = new Set(['High Lift', 'Low Headroom', 'Vertical Lift']);
const customOpeningSizeOption = { label: 'Custom...', value: 'custom' };

function doorSizeCategoryFor(type) {
  return commercialDoorTypes.has(type) ? 'Commercial' : 'Residential';
}

function openingSizeEntriesFor(type) {
  return openingSizePresets[doorSizeCategoryFor(type)] || openingSizePresets.Residential;
}

function openingSizeOptionsFor(type) {
  const entries = openingSizeEntriesFor(type);
  return [...entries, customOpeningSizeOption];
}

function openingSizePreset(type, value) {
  const entries = openingSizeEntriesFor(type);
  return entries.find((entry) => entry.value === value);
}

function applyOpeningSizePreset(door) {
  const preset = openingSizePreset(door.doorType, door.openingSize);
  if (!preset) return;
  door.widthFt = preset.widthFt ?? door.widthFt;
  door.widthIn = preset.widthIn ?? 0;
  door.heightFt = preset.heightFt ?? door.heightFt;
  door.heightIn = preset.heightIn ?? 0;
}

function panelLengthOptionsFor(door) {
  const category = doorSizeCategoryFor(door.doorType);
  return category === 'Commercial' ? commercialPanelLengthOptions : residentialPanelLengthOptions;
}

let nextDoorId = 1;

function collectionOptionsFor(type) {
  const values = doorCollectionsByType[type] || doorCollectionsByType.Steel;
  return values.map((value) => ({ label: value, value }));
}

function getDefaultCollection(type) {
  return (doorCollectionsByType[type] || doorCollectionsByType.Steel)[0] || '';
}

function createDoorConfig() {
  const type = doorTypeOptions[0].value;
  const config = {
    id: `door-${nextDoorId++}`,
    doorType: type,
    collection: getDefaultCollection(type),
    widthFt: 9,
    widthIn: 0,
    heightFt: 7,
    heightIn: 0,
    quantity: 1,
    cyclage: cyclageOptions[0].value,
    spring: springOptions[0].value,
    track: trackOptions[0].value,
    trackRadius: trackRadiusOptions[0].value,
    jambMountFlag: true,
    lockEnabled: false,
    lockType: lockOptions[0].value,
    model: modelOptions[0].value,
    color: colorOptions[0].value,
    jambMountStyle: jambMountOptions[0].value,
    windowsRequested: false,
  };
  config.openingSize = openingSizeOptionsFor(type)[0]?.value || 'custom';
  applyOpeningSizePreset(config);
  config.panelLength = panelLengthOptionsFor(config)[0]?.value || '';
  return config;
}

const doorConfigs = ref([createDoorConfig()]);
const windowsEnabled = ref(false);
const builderLoading = ref(false);
const builderSaving = ref(false);
const builderResult = ref(null);
const builderAction = ref('');

const builderActionLabel = computed(() => {
  if (builderAction.value === 'windows') return 'Final price with windows';
  if (builderAction.value === 'price') return 'Door price preview';
  return 'Door estimate';
});

function appendDoor() {
  doorConfigs.value.push(createDoorConfig());
}

function removeDoor(index) {
  if (doorConfigs.value.length <= 1) return;
  doorConfigs.value.splice(index, 1);
}

function toggleDoorWindows(door) {
  door.windowsRequested = !door.windowsRequested;
}

function onDoorTypeChange(door) {
  door.collection = getDefaultCollection(door.doorType);
  door.openingSize = openingSizeOptionsFor(door.doorType)[0]?.value || 'custom';
  applyOpeningSizePreset(door);
  door.panelLength = panelLengthOptionsFor(door)[0]?.value || door.panelLength;
}

function onOpeningSizeChange(door) {
  if (door.openingSize === 'custom') return;
  applyOpeningSizePreset(door);
}

function shouldShowTrackRadius(track) {
  return trackRadiusTriggers.has(track);
}

function summarizeDoor(door) {
  const width = `${door.widthFt}'${door.widthIn ? door.widthIn + '"' : ''}`;
  const height = `${door.heightFt}'${door.heightIn ? door.heightIn + '"' : ''}`;
  return `${door.collection} · ${width} × ${height} · ${door.track} · Qty ${door.quantity}`;
}

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

function doorPayload(door) {
  return {
    door_type: door.doorType,
    opening_size: door.openingSize,
    collection: door.collection,
    model: door.model,
    color: door.color,
    width_ft: door.widthFt,
    width_ft: door.widthIn,
    height_ft: door.heightFt,
    height_ft: door.heightIn,
    quantity: door.quantity,
    cyclage: door.cyclage,
    spring: door.spring,
    track: door.track,
    track_radius: shouldShowTrackRadius(door.track) ? door.trackRadius : null,
    jamb_mount: door.jambMountFlag ? 'Flag bracket' : 'Angle mount',
    jamb_mount_style: door.jambMountStyle,
    panel_length: door.panelLength,
    lock: door.lockEnabled ? door.lockType : 'Omit Lock',
    windows_requested: door.windowsRequested,
  };
}

function builderPayload(mode) {
  return {
    doors: doorConfigs.value.map(doorPayload),
    include_windows: windowsEnabled.value || mode === 'windows',
    variant: mode,
    source: 'proposals-customer-builder',
  };
}

async function submitEstimate(mode) {
  builderLoading.value = true;
  try {
    const response = await api.post('/api/estimate/calculate', builderPayload(mode));
    if (!response) {
      builderResult.value = null;
      builderAction.value = '';
      return;
    }
    builderResult.value = {
      price: response.price ?? response.sell_price ?? null,
      description: response.description ?? response.label ?? response.message ?? 'Garage door estimate',
      doorSummaries: doorConfigs.value.map((door) => summarizeDoor(door)),
      metadata: response,
    };
    builderAction.value = mode;
  } finally {
    builderLoading.value = false;
  }
}

const applyWindows = () => submitEstimate('windows');
const getPrice = () => submitEstimate('price');

async function saveBuilderEstimate() {
  if (!builderResult.value) return;
  builderSaving.value = true;
  try {
    const payload = builderPayload(builderAction.value || 'price');
    await api.post(
      '/api/estimate/save',
      {
        ...payload,
        price: builderResult.value.price,
        description: builderResult.value.description,
      },
      { successMessage: 'Estimate saved' }
    );
    dismissBuilderResult();
  } finally {
    builderSaving.value = false;
  }
}

function dismissBuilderResult() {
  builderResult.value = null;
  builderAction.value = '';
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
.estimate-builder {
  margin-top: 1.5rem;
  padding: 1.25rem;
  border: 1px solid var(--surface-border, #d0d7de);
  border-radius: 0.85rem;
  background: var(--surface-card, #fff);
}
.estimate-builder__header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
  flex-wrap: wrap;
}
.estimate-builder__description {
  margin: 0;
  color: var(--p-text-muted-color);
  font-size: 0.95rem;
}
.door-card {
  margin-top: 1rem;
  border: 1px solid var(--surface-border, #d0d7de);
  border-radius: 0.6rem;
  padding: 1rem;
  background: var(--surface-ground, #fafbfc);
}
.door-card__header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
  margin-bottom: 0.85rem;
}
.door-card__label {
  margin: 0;
  font-weight: 600;
}
.door-card__subtitle {
  margin: 0;
  color: var(--p-text-muted-color);
  font-size: 0.85rem;
}
.door-card__decoration {
  margin-top: 0.75rem;
  padding-top: 0.65rem;
  border-top: 1px dashed var(--surface-border, #d0d7de);
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.door-card__decoration-note {
  margin: 0;
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
}
.builder-grid {
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
}
.jamb-mount-row {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.5rem;
  align-items: center;
}
.dimension-row {
  display: flex;
  gap: 0.5rem;
}
.dimension-stack {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.dimension-sub-label {
  font-size: 0.72rem;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--p-text-muted-color);
}
.dimension-input {
  flex: 1;
}
.lock-row {
  display: grid;
  gap: 0.5rem;
}
.estimate-builder__windows {
  margin-top: 1.25rem;
  display: flex;
  align-items: center;
  gap: 0.75rem;
}
.estimate-builder__windows-note {
  margin: 0;
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}
.builder-actions {
  margin-top: 1rem;
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
}
.portal-parity-helper {
  display: none;
}
.result-panel {
  margin-top: 1rem;
  border: 1px solid var(--surface-border, #d0d7de);
  border-radius: 0.85rem;
  padding: 1rem;
  background: var(--surface-ground, #fafbfc);
}
.result-panel__header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
}
.result-panel__label {
  margin: 0;
  font-weight: 600;
}
.result-panel__description {
  margin: 0;
  color: var(--p-text-muted-color);
  font-size: 0.9rem;
}
.result-panel__amount {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--p-primary-color);
}
.result-panel__list {
  margin: 0.75rem 0 0;
  padding: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  color: var(--p-text-muted-color);
}
.result-panel__actions {
  margin-top: 0.75rem;
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  justify-content: flex-end;
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
