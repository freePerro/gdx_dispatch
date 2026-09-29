<template>
    <section class="leads-view view-card">
      <Toolbar>
        <template #start>
          <h2 class="page-title">Inbound Leads</h2>
          <p class="page-subtitle">Web-form and intake captures awaiting first contact. Existing-customer service inquiries live on the <router-link to="/jobs">Jobs board</router-link> as service calls.</p>
        </template>
        <template #end>
          <Button
            label="Export"
            icon="pi pi-download"
            aria-label="Export CSV"
            text
            data-testid="leads-export-btn"
            @click="exportLeads"
          />
          <Button v-if="canWrite" label="+ New Lead" icon="pi pi-plus" @click="openCreate" />
        </template>
      </Toolbar>

      <div class="pipeline-stats">
        <Card v-for="stage in pipelineStages" :key="stage.key" class="pipeline-card">
          <template #title>
            <span class="pipeline-label">{{ stage.label }}</span>
          </template>
          <div class="stat-value">{{ pipelineSummary[stage.key] ?? 0 }}</div>
        </Card>
      </div>

      <div class="filters-bar">
        <Tabs v-model:value="stageFilter" class="stage-tabs">
          <TabList>
            <Tab v-for="tab in stageTabs" :key="tab" :value="tab">
              <span class="tab-label">{{ tabLabel(tab) }}
                <small v-if="tab === 'all'">({{ leads.length }})</small>
                <small v-else-if="pipelineSummary[tab] !== undefined">({{ pipelineSummary[tab] }})</small>
              </span>
            </Tab>
          </TabList>
        </Tabs>
        <div class="due-filter-box">
          <Select
            v-model="dueFilter"
            :options="dueOptions"
            optionLabel="label"
            optionValue="value"
            placeholder="Due status"
            class="due-filter-select"
            data-testid="leads-due-filter"
            @change="loadLeads"
          />
        </div>
      </div>

      <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

      <DataTable
        class="clickable-rows"
        responsiveLayout="scroll"
        v-else
        :value="filteredLeads"
        dataKey="id"
        paginator
        :rows="20"
        :rowsPerPageOptions="[10, 20, 50, 100]"
        striped-rows
        @row-click="openEdit($event.data)"
      >
        <template #empty>
          <EmptyState
            icon="pi pi-users"
            title="No leads yet"
            message="Use the button above to add a new prospect."
            :actionLabel="canWrite ? '+ Add Lead' : ''"
            @action="openCreate"
          />
        </template>
        <Column field="name" header="Name" sortable />
        <Column field="email" header="Email" sortable />
        <Column field="stage" header="Stage" style="width:160px" sortable>
          <template #body="{ data }">
            <Badge :value="stageLabel(data.stage)" :severity="stageSeverity(data.stage)" />
          </template>
        </Column>
        <Column field="follow_up_date" header="Call Back" style="width:170px" sortable>
          <template #body="{ data }">
            <div class="inline-date-cell" @click.stop>
              <DatePicker
                v-if="canWrite"
                :modelValue="parseLocalDateString(data.follow_up_date)"
                dateFormat="yy-mm-dd"
                placeholder="Set date"
                :manualInput="false"
                :disabled="followUpSaving.includes(data.id)"
                :showIcon="false"
                class="inline-follow-up-picker"
                :class="{
                  'date-overdue': isOverdue(data.follow_up_date, data.stage),
                  'date-today': isDueToday(data.follow_up_date),
                }"
                data-testid="inline-follow-up-date"
                @update:modelValue="updateLeadFollowUpDate(data, $event)"
              />
              <span
                v-else
                :class="{
                  'text-overdue': isOverdue(data.follow_up_date, data.stage),
                  'text-today': isDueToday(data.follow_up_date),
                }"
              >
                {{ formatDate(data.follow_up_date) || '—' }}
              </span>
            </div>
          </template>
        </Column>
        <Column field="estimated_value" header="Estimated Value" style="width:140px" sortable>
          <template #body="{ data }">{{ formatCurrency(data.estimated_value) }}</template>
        </Column>
        <Column field="source" header="Source" sortable />
        <Column field="created_at" header="Created" style="width:140px" sortable>
          <template #body="{ data }">{{ formatDate(data.created_at) }}</template>
        </Column>
        <Column header="Actions" style="width:380px">
          <template #body="{ data }">
            <div class="row-actions">
              <Button
                v-if="canWrite && data.email"
                text
                size="small"
                icon="pi pi-envelope"
                aria-label="Reply by email"
                v-tooltip.top="'Reply by email'"
                data-testid="lead-email"
                @click.stop="emailLead(data)"
              />
              <Button
                v-if="canWrite"
                text
                size="small"
                icon="pi pi-wrench"
                aria-label="Create service call"
                v-tooltip.top="'Create service call'"
                data-testid="lead-service-call"
                :loading="serviceCallLeadId === data.id"
                :disabled="serviceCallLeadId === data.id || estimateLeadId === data.id || startEstimateLeadId === data.id"
                @click.stop="createServiceCall(data)"
              />
              <Button
                v-if="canStartEstimate"
                text
                size="small"
                icon="pi pi-file-edit"
                label="Start estimate"
                aria-label="Start estimate"
                v-tooltip.top="'Start estimate'"
                data-testid="lead-start-estimate"
                :loading="startEstimateLeadId === data.id"
                :disabled="serviceCallLeadId === data.id || estimateLeadId === data.id || startEstimateLeadId === data.id"
                @click.stop="startEstimateFromLead(data)"
              />
              <Button
                v-else-if="canWrite"
                text
                size="small"
                icon="pi pi-file-edit"
                aria-label="Create estimate"
                v-tooltip.top="'Create estimate'"
                data-testid="lead-estimate"
                :loading="estimateLeadId === data.id"
                :disabled="serviceCallLeadId === data.id || estimateLeadId === data.id || startEstimateLeadId === data.id"
                @click.stop="createEstimateFromLead(data)"
              />
              <Button
                v-if="canWrite"
                text
                size="small"
                icon="pi pi-arrow-circle-right"
                label="Advance Stage"
                severity="info"
                :disabled="!nextStage(data.stage)"
                :loading="advancingLeadId === data.id"
                @click.stop="advanceStage(data)"
              />
              <Button
                v-if="canWrite && data.stage === 'won'"
                text
                size="small"
                icon="pi pi-user-plus"
                label="Convert to Customer"
                severity="success"
                :loading="convertingLeadId === data.id"
                :disabled="!!data.converted_customer_id"
                @click.stop="convertToCustomer(data)"
              />
              <Button
                v-if="canDelete"
                icon="pi pi-trash"
                size="small"
                severity="danger"
                text
                aria-label="Delete lead"
                v-tooltip.top="'Delete lead'"
                :loading="deletingLeadId === data.id"
                :disabled="deletingLeadId === data.id"
                @click.stop="confirmDeleteLead(data)"
              />
            </div>
          </template>
        </Column>
      </DataTable>

      <section class="landing-section">
        <header class="landing-header">
          <h3>Landing Leads</h3>
          <small>Form submissions that have not been promoted yet.</small>
        </header>
        <div v-if="landingLoading" class="spinner-wrap small"><ProgressSpinner /></div>
        <DataTable
          responsiveLayout="scroll" v-else :value="landingLeads" dataKey="id" paginator :rows="10" striped-rows
          class="landing-table" @row-click="openLanding($event.data)">
          <template #empty>
            <EmptyState
              icon="pi pi-inbox"
              title="No landing leads"
              message="Lead-ready prospects will appear here after a web form submission."
            />
          </template>
          <Column field="name" header="Name" sortable>
            <template #body="{ data }">{{ data.name || '—' }}</template>
          </Column>
          <Column field="email" header="Email" sortable>
            <template #body="{ data }">{{ data.email || '—' }}</template>
          </Column>
          <Column field="phone" header="Phone" sortable>
            <template #body="{ data }">{{ formatPhone(data.phone) || '—' }}</template>
          </Column>
          <Column field="source" header="Source" sortable />
          <!-- Status column (2026-08-04): the dashboard's "N new website
               leads" count is status='new' — without this column the page
               it links to couldn't show WHICH rows are the N. -->
          <Column field="status" header="Status" style="width:110px" sortable>
            <template #body="{ data }">
              <Tag
                :value="data.status || 'new'"
                :severity="data.status === 'contacted' ? 'info' : data.status === 'promoted' || data.status === 'completed' ? 'success' : data.status === 'discarded' ? 'secondary' : 'warn'"
                data-testid="landing-status-tag"
              />
            </template>
          </Column>
          <Column field="created_at" header="Submitted" style="width:140px" sortable>
            <template #body="{ data }">{{ formatDate(data.created_at) }}</template>
          </Column>
          <Column header="Actions" style="width:460px">
            <template #body="{ data }">
              <div class="row-actions">
                <Button
                  v-if="canWrite && (data.status || 'new') === 'new'"
                  label="Contacted"
                  icon="pi pi-phone"
                  size="small"
                  severity="success"
                  outlined
                  data-testid="landing-mark-contacted"
                  :loading="landingContactingId === data.id"
                  :disabled="landingConvertingId === data.id || landingDeletingId === data.id || landingContactingId === data.id"
                  @click.stop="markLandingContacted(data)"
                />
                <Button
                  v-if="canWrite && ['new', 'contacted'].includes(data.status || 'new')"
                  label="Completed"
                  icon="pi pi-check"
                  size="small"
                  severity="success"
                  outlined
                  data-testid="landing-mark-completed"
                  :loading="landingCompletingId === data.id"
                  :disabled="landingConvertingId === data.id || landingDeletingId === data.id || landingCompletingId === data.id"
                  @click.stop="markLandingCompleted(data)"
                />
                <Button
                  v-if="canWrite && data.status === 'completed'"
                  label="Reopen"
                  icon="pi pi-undo"
                  size="small"
                  severity="secondary"
                  outlined
                  data-testid="landing-reopen"
                  :loading="landingCompletingId === data.id"
                  :disabled="landingConvertingId === data.id || landingDeletingId === data.id || landingCompletingId === data.id"
                  @click.stop="reopenLanding(data)"
                />
                <Button
                  v-if="canWrite"
                  label="Convert"
                  icon="pi pi-arrow-right"
                  size="small"
                  :loading="landingConvertingId === data.id"
                  :disabled="landingConvertingId === data.id || landingDeletingId === data.id"
                  @click.stop="convertLandingLead(data)"
                />
                <Button
                  v-if="canDelete"
                  label="Spam"
                  icon="pi pi-times"
                  size="small"
                  severity="danger"
                  outlined
                  :loading="landingDeletingId === data.id"
                  :disabled="landingConvertingId === data.id || landingDeletingId === data.id"
                  @click.stop="confirmDeleteLanding(data, 'spam')"
                />
                <Button
                  v-if="canDelete"
                  icon="pi pi-trash"
                  size="small"
                  severity="secondary"
                  text
                  aria-label="Delete"
                  v-tooltip.top="'Delete (not spam)'"
                  :loading="landingDeletingId === data.id"
                  :disabled="landingConvertingId === data.id || landingDeletingId === data.id"
                  @click.stop="confirmDeleteLanding(data, 'manual')"
                />
              </div>
            </template>
          </Column>
        </DataTable>
      </section>

      <Dialog
        v-model:visible="showDialog"
        :header="editingLead ? `Edit ${editingLead.name}` : 'New Lead'"
        modal
        :style="{ width: '640px', maxWidth: '95vw' }"
      >
        <div class="form-grid">
          <div class="form-field">
            <label>Name *</label>
            <InputText v-model="form.name" class="w-full" />
          </div>
          <div class="form-field">
            <label>Email</label>
            <InputText v-model="form.email" class="w-full" />
          </div>
          <div class="form-field">
            <label>Phone</label>
            <PhoneInput v-model="form.phone" class="w-full" />
          </div>
          <div class="form-field">
            <label>Source</label>
            <InputText v-model="form.source" class="w-full" />
          </div>
          <div class="form-field">
            <label>Stage</label>
            <Select v-model="form.stage" :options="stageOptions" optionLabel="label" optionValue="value" class="w-full" />
          </div>
          <div class="form-field">
            <label>Call-back Date</label>
            <InputText v-model="form.follow_up_date" type="date" class="w-full" data-testid="lead-follow-up-date-input" />
          </div>
          <div class="form-field">
            <label>Estimated Value</label>
            <InputText v-model="form.estimated_value" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Address</label>
            <InputText v-model="form.address" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Assigned To</label>
            <InputText v-model="form.assigned_to" class="w-full" />
          </div>
          <div class="form-field full-width">
            <label>Notes</label>
            <Textarea v-model="form.notes" rows="3" class="w-full" />
          </div>

          <!-- Custom intake fields -->
          <div v-if="editingLead && customFieldDefs.length" class="form-field full-width custom-fields-block" data-testid="lead-custom-fields-block">
            <h4 class="custom-fields-title">Intake Answers</h4>
            <div class="custom-fields-grid">
              <div
                v-for="f in customFieldDefs"
                :key="f.id || f.field_key"
                class="form-field"
                :data-testid="`custom-field-${f.field_key}`"
              >
                <label>{{ f.label }}</label>
                <Select
                  v-if="f.field_type === 'select'"
                  v-model="customFieldValues[f.field_key]"
                  :options="f.options || []"
                  placeholder="Choose..."
                  class="w-full"
                />
                <InputNumber
                  v-else-if="f.field_type === 'number'"
                  v-model="customFieldValues[f.field_key]"
                  class="w-full"
                />
                <div v-else-if="f.field_type === 'boolean'" class="bool-field">
                  <Checkbox
                    v-model="customFieldValues[f.field_key]"
                    :binary="true"
                  />
                  <span>Yes</span>
                </div>
                <InputText
                  v-else-if="f.field_type === 'date'"
                  v-model="customFieldValues[f.field_key]"
                  type="date"
                  class="w-full"
                />
                <InputText
                  v-else
                  v-model="customFieldValues[f.field_key]"
                  class="w-full"
                />
              </div>
            </div>
          </div>

          <!-- Every estimate made for this lead, and which one counts as won -->
          <div v-if="editingLead && canSeeLeadEstimates" class="form-field full-width">
            <LeadEstimatesPanel
              :lead-id="editingLead.id"
              :can-write="canWrite"
              @selected="onLeadEstimateSelected"
            />
          </div>
        </div>
        <template #footer>
          <Button
            v-if="editingLead && canStartEstimate"
            label="Start Estimate"
            icon="pi pi-file-edit"
            severity="info"
            data-testid="dialog-start-estimate"
            :loading="startEstimateLeadId === editingLead.id"
            @click="startEstimateFromLead(editingLead)"
          />
          <Button label="Cancel" severity="secondary" @click="showDialog = false" />
          <Button :label="editingLead ? 'Save Lead' : 'Create Lead'" icon="pi pi-check" :loading="saving" :disabled="customFieldsLoading || (editingLead && followUpSaving.includes(editingLead.id))" @click="saveLead" />
        </template>
      </Dialog>

      <Dialog
        v-model:visible="showLandingDialog"
        :header="selectedLanding ? `Submission — ${selectedLanding.name || selectedLanding.email || 'Anonymous'}` : 'Submission'"
        modal
        :style="{ width: '560px' }"
      >
        <div v-if="selectedLanding" class="landing-detail">
          <!-- What the submission actually asked for: reply, book the repair,
               or start a quote — the three exits that used to not exist. -->
          <div v-if="canWrite" class="ld-actions">
            <Button
              v-if="selectedLanding.email"
              label="Reply by email"
              icon="pi pi-envelope"
              size="small"
              outlined
              data-testid="landing-email"
              @click="emailLanding(selectedLanding)"
            />
            <Button
              label="Service call"
              icon="pi pi-wrench"
              size="small"
              outlined
              data-testid="landing-service-call"
              :loading="landingActionId === selectedLanding.id"
              :disabled="landingActionId === selectedLanding.id"
              @click="landingServiceCall(selectedLanding)"
            />
            <Button
              label="Estimate"
              icon="pi pi-file-edit"
              size="small"
              outlined
              data-testid="landing-estimate"
              :loading="landingActionId === selectedLanding.id"
              :disabled="landingActionId === selectedLanding.id"
              @click="landingEstimate(selectedLanding)"
            />
          </div>
          <div class="ld-row"><span class="ld-label">Name</span><span>{{ selectedLanding.name || '—' }}</span></div>
          <div class="ld-row">
            <span class="ld-label">Email</span>
            <span><a v-if="selectedLanding.email" :href="`mailto:${selectedLanding.email}`">{{ selectedLanding.email }}</a><template v-else>—</template></span>
          </div>
          <div class="ld-row">
            <span class="ld-label">Phone</span>
            <span><a v-if="selectedLanding.phone" :href="`tel:${selectedLanding.phone}`">{{ formatPhone(selectedLanding.phone) }}</a><template v-else>—</template></span>
          </div>
          <div class="ld-row"><span class="ld-label">Source</span><span>{{ selectedLanding.source || '—' }}</span></div>
          <div class="ld-row"><span class="ld-label">Status</span><span>{{ selectedLanding.status || 'new' }}</span></div>
          <div class="ld-message">
            <span class="ld-label">Message</span>
            <p class="ld-message-body" data-testid="landing-message">{{ selectedLanding.message || 'No message provided.' }}</p>
          </div>
          <div v-if="selectedLanding.referrer" class="ld-row">
            <span class="ld-label">Referrer</span><span class="ld-muted">{{ selectedLanding.referrer }}</span>
          </div>
          <div v-if="hasUtm(selectedLanding)" class="ld-row">
            <span class="ld-label">Campaign</span>
            <span class="ld-muted">{{ [selectedLanding.utm_campaign, selectedLanding.utm_source, selectedLanding.utm_medium].filter(Boolean).join(' · ') }}</span>
          </div>
          <div class="ld-row">
            <span class="ld-label">Submitted</span><span class="ld-muted">{{ formatDateTime(selectedLanding.created_at) }}</span>
          </div>
          <div v-if="selectedLanding.contacted_at" class="ld-row">
            <span class="ld-label">Contacted</span><span class="ld-muted">{{ formatDateTime(selectedLanding.contacted_at) }}</span>
          </div>
        </div>
        <template #footer>
          <Button label="Close" severity="secondary" text @click="showLandingDialog = false" />
          <Button
            v-if="canWrite && selectedLanding && ['new', 'contacted'].includes(selectedLanding.status || 'new')"
            label="Completed"
            icon="pi pi-check"
            severity="success"
            outlined
            data-testid="landing-dialog-completed"
            :loading="landingCompletingId === selectedLanding.id"
            @click="completeFromDialog(selectedLanding)"
          />
          <Button
            v-if="canWrite && selectedLanding && selectedLanding.status === 'completed'"
            label="Reopen"
            icon="pi pi-undo"
            severity="secondary"
            outlined
            data-testid="landing-dialog-reopen"
            :loading="landingCompletingId === selectedLanding.id"
            @click="reopenFromDialog(selectedLanding)"
          />
          <Button
            v-if="canWrite && selectedLanding"
            label="Convert to Lead"
            icon="pi pi-arrow-right"
            :loading="landingConvertingId === selectedLanding.id"
            @click="convertFromDialog(selectedLanding)"
          />
          <Button
            v-if="canDelete && selectedLanding"
            label="Spam"
            icon="pi pi-times"
            severity="danger"
            outlined
            @click="deleteFromDialog(selectedLanding, 'spam')"
          />
          <Button
            v-if="canDelete && selectedLanding"
            label="Delete"
            icon="pi pi-trash"
            severity="danger"
            text
            @click="deleteFromDialog(selectedLanding, 'manual')"
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { leadStageSeverity } from '../utils/statusSeverity';
import { computed, onMounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { useApiWithToast } from '../composables/useApiWithToast';
import { useToast } from 'primevue/usetoast';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
import { useListPrefs } from '../composables/useListPrefs';
import { useTableExport } from '../composables/useTableExport';
import { formatMoney as formatCurrency, formatDate, formatDateTime, formatPhone, localDateString, parseLocalDateString } from '../composables/useFormatters';
import { useAuthStore } from '../stores/auth';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import InputNumber from 'primevue/inputnumber';
import Checkbox from 'primevue/checkbox';
import DatePicker from 'primevue/datepicker';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Textarea from 'primevue/textarea';
import Badge from 'primevue/badge';
import ProgressSpinner from 'primevue/progressspinner';
import Tabs from 'primevue/tabs';
import TabList from 'primevue/tablist';
import Tab from 'primevue/tab';
import Toolbar from 'primevue/toolbar';
import EmptyState from '../components/EmptyState.vue';
import PhoneInput from '../components/PhoneInput.vue';
import LeadEstimatesPanel from '../components/LeadEstimatesPanel.vue';

const api = useApiWithToast();
const toast = useToast();
const { confirmDestructive } = useDestructiveConfirm();
const auth = useAuthStore();
const router = useRouter();
const route = useRoute();

// Mirror the backend require_permission gates on leads.py so we don't
// render controls a role will only get a 403 from. Hide (not disable):
// a permission a role will never have shouldn't advertise itself
// (Smashing/NN-group: hide when the user can't act on it).
const canWrite = computed(() => auth.hasPermission('leads.write'));
const canDelete = computed(() => auth.hasPermission('leads.delete'));
const canStartEstimate = computed(() => auth.hasPermission('leads.write') && auth.hasPermission('estimates.write'));
// GET /api/leads/{id}/estimates needs both (accounting has leads.read only).
const canSeeLeadEstimates = computed(() => auth.hasPermission('leads.read') && auth.hasPermission('estimates.read_all'));

const leads = ref([]);
const landingLeads = ref([]);
const pipelineSummary = ref({ New: 0, Contacted: 0, Qualified: 0, Quoted: 0, Won: 0, Lost: 0 });
const loading = ref(true);
const landingLoading = ref(true);
const stageFilter = ref('All');
const dueFilter = ref('all');
const showDialog = ref(false);
const editingLead = ref(null);
const saving = ref(false);
const convertingLeadId = ref(null);
const advancingLeadId = ref(null);
const landingConvertingId = ref(null);
const landingContactingId = ref(null);
const landingCompletingId = ref(null);
const landingDeletingId = ref(null);
const deletingLeadId = ref(null);
const showLandingDialog = ref(false);
const selectedLanding = ref(null);
const serviceCallLeadId = ref(null);
const estimateLeadId = ref(null);
const startEstimateLeadId = ref(null);
const landingActionId = ref(null);

const customFieldDefs = ref([]);
const customFieldValues = ref({});
const customFieldsLoading = ref(false);
// What the server holds, so Save sends only the answers someone changed.
const customFieldOriginal = ref({});
let customFieldsRequest = 0;

const dueOptions = [
  { value: 'all', label: 'All Due Dates' },
  { value: 'overdue', label: 'Overdue' },
  { value: 'today', label: 'Due Today' },
  { value: 'upcoming', label: 'Upcoming' },
];

const stageOptions = [
  { value: 'New', label: 'New' },
  { value: 'Contacted', label: 'Contacted' },
  { value: 'Qualified', label: 'Qualified' },
  { value: 'Quoted', label: 'Quoted' },
  { value: 'Won', label: 'Won' },
  { value: 'Lost', label: 'Lost' },
];

const stageOrder = ['New', 'Contacted', 'Qualified', 'Quoted', 'Won'];
const stageTabs = ['All', ...stageOptions.map((s) => s.value)];

// Persist the chosen stage tab across reloads (JobsView/BillingView
// pattern). A stale/renamed stage falls back to 'All' so the list never
// silently filters to empty.
useListPrefs(
  'leads',
  { stageFilter },
  {
    stageFilter: { default: 'All', valid: (v) => stageTabs.includes(v) },
  },
);

const form = ref(emptyForm());

const pipelineStages = [
  { key: 'New', label: 'New' },
  { key: 'Contacted', label: 'Contacted' },
  { key: 'Qualified', label: 'Qualified' },
  { key: 'Quoted', label: 'Quoted' },
  { key: 'Won', label: 'Won' },
  { key: 'Lost', label: 'Lost' },
];

// The local calendar day, never toISOString()'s UTC one — after ~7pm Central
// that is tomorrow, and every lead due today would read as overdue.
function isOverdue(dateStr, stage) {
  if (!dateStr) return false;
  const s = String(stage || '').toLowerCase();
  if (['won', 'lost'].includes(s)) return false;
  return dateStr < localDateString(new Date());
}

function isDueToday(dateStr) {
  if (!dateStr) return false;
  return dateStr === localDateString(new Date());
}

const filteredLeads = computed(() => {
  let list = leads.value;
  if (stageFilter.value !== 'All') {
    list = list.filter((lead) => lead.stage === stageFilter.value);
  }
  return list.slice().sort((a, b) => {
    const isAOverdue = isOverdue(a.follow_up_date, a.stage);
    const isBOverdue = isOverdue(b.follow_up_date, b.stage);
    if (isAOverdue && !isBOverdue) return -1;
    if (!isAOverdue && isBOverdue) return 1;
    if (a.follow_up_date && b.follow_up_date) {
      return a.follow_up_date.localeCompare(b.follow_up_date);
    }
    if (a.follow_up_date && !b.follow_up_date) return -1;
    if (!a.follow_up_date && b.follow_up_date) return 1;
    return 0;
  });
});

// CSV export — dumps the CURRENTLY FILTERED rows (stage tab applied),
// matching the visible table columns.
const { exportCsv } = useTableExport();
function exportLeads() {
  exportCsv(
    filteredLeads.value,
    [
      { field: 'name', header: 'Name' },
      { field: 'email', header: 'Email' },
      { field: 'stage', header: 'Stage' },
      { field: 'follow_up_date', header: 'Call Back' },
      { field: 'estimated_value', header: 'Estimated Value' },
      { field: 'source', header: 'Source' },
      { field: 'created_at', header: 'Created' },
    ],
    'leads',
  );
}

function tabLabel(value) {
  if (value === 'All') return 'All';
  return stageOptions.find((option) => option.value === value)?.label || value;
}

function stageLabel(value) {
  return stageOptions.find((option) => option.value === value)?.label || value || '—';
}

function stageSeverity(stage) {
  return leadStageSeverity(stage);
}

function capitalize(s) {
  if (!s) return "";
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}

function hasUtm(ll) {
  return Boolean(ll && (ll.utm_campaign || ll.utm_source || ll.utm_medium));
}

function emptyForm() {
  return {
    name: '',
    email: '',
    phone: '',
    address: '',
    stage: 'new',
    estimated_value: '',
    source: '',
    assigned_to: '',
    notes: '',
    follow_up_date: '',
  };
}

// Latest request wins: flipping the due filter fast must never leave one
// filter's rows under another's label.
let leadsRequest = 0;
// The request number of the load whose rows are on screen now.
let appliedRequest = 0;

// quiet: refresh the rows in place, without swapping the table for a spinner.
// Resolves true when its rows were applied, false when a newer load won.
async function loadLeads({ quiet = false } = {}) {
  const req = ++leadsRequest;
  if (!quiet) loading.value = true;
  try {
    const params = new URLSearchParams();
    if (dueFilter.value && dueFilter.value !== 'all') {
      params.append('follow_up', dueFilter.value);
    }
    const queryStr = params.toString() ? `?${params.toString()}` : '';
    const data = await api.get(`/api/leads${queryStr}`);
    if (req !== leadsRequest) return false;
    const list = Array.isArray(data) ? data : data?.items || [];
    leads.value = list.map((l) => ({ ...l, stage: capitalize(l.stage) || 'New' }));
    appliedRequest = req;
    return true;
  } finally {
    // Whoever is newest clears the spinner — a quiet load that superseded a
    // normal one must too, or nothing ever does.
    if (req === leadsRequest) loading.value = false;
  }
}

async function loadPipelineSummary() {
  try {
    const summary = await api.get('/api/leads/pipeline-summary');
    const capitalized = {};
    for (const [key, val] of Object.entries(summary || {})) {
      capitalized[capitalize(key)] = val;
    }
    pipelineSummary.value = { ...pipelineSummary.value, ...capitalized };
  } catch {
    pipelineSummary.value = { New: 0, Contacted: 0, Qualified: 0, Quoted: 0, Won: 0, Lost: 0 };
  }
}

async function loadLandingLeads() {
  landingLoading.value = true;
  try {
    const data = await api.get('/api/landing-leads');
    landingLeads.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    landingLoading.value = false;
  }
}

async function refreshLeads() {
  await Promise.all([loadLeads(), loadPipelineSummary()]);
}

// The answers in memory must belong to the dialog as it was last opened:
// they are cleared on every open, only the newest request's response is kept
// (reopening the same lead included), and Save waits while it loads —
// otherwise one lead's answers save onto another, or a late reply eats an edit.
async function loadLeadCustomFields(leadId) {
  const req = ++customFieldsRequest;
  customFieldDefs.value = [];
  customFieldValues.value = {};
  customFieldOriginal.value = {};
  customFieldsLoading.value = true;
  try {
    const defs = await api.get(`/api/leads/${leadId}/custom-fields`);
    if (req !== customFieldsRequest) return;
    customFieldDefs.value = Array.isArray(defs) ? defs : [];
    // The server stores every answer as text ("true", "3"); a binary
    // Checkbox and an InputNumber need the real types back.
    const vals = {};
    for (const f of customFieldDefs.value) {
      if (f.field_type === 'boolean') vals[f.field_key] = f.value === 'true' || f.value === true;
      else if (f.field_type === 'number') vals[f.field_key] = f.value == null || f.value === '' ? null : Number(f.value);
      else vals[f.field_key] = f.value ?? null;
    }
    customFieldValues.value = vals;
    customFieldOriginal.value = { ...vals };
  } catch {
    // useApi has already said why; the dialog just shows no answers.
  } finally {
    if (req === customFieldsRequest) customFieldsLoading.value = false;
  }
}

function openCreate() {
  editingLead.value = null;
  form.value = emptyForm();
  customFieldDefs.value = [];
  customFieldValues.value = {};
  customFieldOriginal.value = {};
  customFieldsRequest++;
  customFieldsLoading.value = false;
  showDialog.value = true;
}

function openEdit(lead) {
  editingLead.value = lead;
  form.value = {
    ...lead,
    estimated_value: lead.estimated_value ?? '',
    follow_up_date: lead.follow_up_date || '',
  };
  formOriginal.value = leadPayload(form.value);
  loadLeadCustomFields(lead.id);
  showDialog.value = true;
}

// The pick changed on the server; keep the row (and the open dialog) honest.
function onLeadEstimateSelected(updated) {
  if (!updated?.id) return;
  const row = leads.value.find((l) => l.id === updated.id);
  if (row) row.selected_estimate_id = updated.selected_estimate_id;
  if (editingLead.value?.id === updated.id) editingLead.value.selected_estimate_id = updated.selected_estimate_id;
}

function openLanding(landingLead) {
  selectedLanding.value = landingLead;
  showLandingDialog.value = true;
}

async function convertFromDialog(landingLead) {
  await convertLandingLead(landingLead);
  showLandingDialog.value = false;
}

function deleteFromDialog(landingLead, reason) {
  // Close the detail view first so the confirm popup isn't stacked over a
  // row that's about to disappear; confirmDeleteLanding owns the confirm.
  showLandingDialog.value = false;
  confirmDeleteLanding(landingLead, reason);
}

// "$2,500", "1,500" and "2500.50" are amounts. Blank — including a box
// backspaced down to "$" — is undefined (the API cannot blank it). Anything
// else ("about 2k", "1e999", "0x10") is NaN and refused before sending: only
// plain digits go out, never a value JSON would turn into null.
// One leading "$" is allowed; commas only as thousands separators; nothing
// is stripped from inside the number ("2,50", "2 50", "25$00" are typos).
function parseEstimatedValue(raw) {
  const s = raw === null || raw === undefined ? '' : String(raw).trim();
  if (s === '' || s === '$') return undefined;
  const m = /^\$?\s*((\d+|\d{1,3}(,\d{3})+)(\.\d{1,2})?)$/.exec(s);
  return m ? Number(m[1].replace(/,/g, '')) : NaN;
}

function leadPayload(f) {
  return {
    name: f.name,
    email: f.email,
    phone: f.phone,
    address: f.address,
    stage: f.stage,
    estimated_value: parseEstimatedValue(f.estimated_value),
    source: f.source,
    assigned_to: f.assigned_to,
    notes: f.notes,
    follow_up_date: f.follow_up_date || null,
  };
}

// The edit form as it was opened. Save sends only the fields changed since:
// a value merely copied into the form — possibly stale, e.g. a call-back
// date picked in the table while the dialog was opening — is never written
// back over what the server holds.
const formOriginal = ref(null);

async function saveLead() {
  if (!form.value.name.trim()) return;
  // An inline call-back save for this lead is still in flight (Save is
  // disabled too): a dialog PATCH now could land on either side of it.
  if (editingLead.value && followUpSaving.value.includes(editingLead.value.id)) return;
  const payload = leadPayload(form.value);
  if (Number.isNaN(payload.estimated_value)) {
    // NaN would go out as null, which the API ignores — a "save" that isn't.
    toast.add({ severity: 'warn', summary: 'Estimated value is not a number', detail: 'Enter an amount like 2500.', life: 5000 });
    return;
  }
  saving.value = true;

  try {
    if (editingLead.value) {
      // Only the answers someone changed: an untouched, unanswered yes/no is
      // null on the server and must not be saved as "No".
      // A text box typed into and emptied again reads "" — still unanswered.
      const changed = {};
      for (const [k, raw] of Object.entries(customFieldValues.value)) {
        const v = raw === '' ? null : raw;
        if (v !== customFieldOriginal.value[k]) changed[k] = v;
      }
      const hasAnswers = Object.keys(changed).length > 0;
      const edits = {};
      for (const [k, v] of Object.entries(payload)) {
        if (JSON.stringify(v) !== JSON.stringify(formOriginal.value?.[k])) edits[k] = v;
      }
      // The API cannot blank Estimated Value (it ignores null there), and an
      // undefined never reaches the wire. Say so rather than report a save.
      if ('estimated_value' in edits && edits.estimated_value === undefined) {
        delete edits.estimated_value;
        // Blanking a 0 changes nothing; blanking an amount is refused, loudly,
        // and the dialog stays open if there is nothing else to save.
        if (formOriginal.value?.estimated_value) {
          toast.add({
            severity: 'warn',
            summary: 'Estimated value not cleared',
            detail: 'It cannot be left blank once set — enter 0 instead.',
            life: 5000,
          });
          if (Object.keys(edits).length === 0 && !hasAnswers) return;
        }
      }
      // Up to two writes, not atomic. "Lead updated" rides on the last one;
      // if only the first lands, the table shows it and the dialog stays open
      // (useApi has toasted the failure) so the answers can be saved again.
      if (Object.keys(edits).length > 0) {
        await api.patch(`/api/leads/${editingLead.value.id}`, edits, hasAnswers ? {} : { successMessage: 'Lead updated' });
        // Landed: a retry after a failed answers save must not resend these.
        formOriginal.value = { ...formOriginal.value, ...edits };
      }
      if (hasAnswers) {
        try {
          await api.put(`/api/leads/${editingLead.value.id}/custom-fields`, { values: changed }, { successMessage: 'Lead updated' });
          customFieldOriginal.value = { ...customFieldValues.value };
        } catch {
          await refreshLeads();
          return;
        }
      }
    } else {
      await api.post('/api/leads', payload, { successMessage: 'Lead created' });
    }
    showDialog.value = false;
    await refreshLeads();
  } finally {
    saving.value = false;
  }
}

// The inline picker is pick-only (manualInput=false): a typed date would be
// reported, and saved, at every keystroke that happens to parse. Typing a
// date stays available in the edit dialog, which saves once.
// One save per lead at a time — its picker is disabled, and so is the edit
// dialog's Save for that lead, from the pick until its re-read has settled. Afterwards, landed or
// not, the rows are re-read from the server: that read is requested after the
// PATCH settled and loadLeads is latest-wins, so the table shows what the
// server holds, under the right due filter, whatever else was in flight.
// What is on screen is trusted only if it was read after the PATCH settled
// (appliedRequest > settledAt); otherwise the PATCH's own outcome is shown.
// An open edit dialog sends only what the user changed, so a date it merely
// copied can never be written back.
const followUpSaving = ref([]);

async function updateLeadFollowUpDate(lead, dateVal) {
  const dateStr = localDateString(dateVal);
  // The picker reports every click, including the day already selected;
  // re-picking it changes nothing, so it writes nothing (no PATCH, no audit row).
  if (dateStr === (lead.follow_up_date || null)) return;
  if (followUpSaving.value.includes(lead.id)) return;
  followUpSaving.value = [...followUpSaving.value, lead.id];
  const previous = lead.follow_up_date || null;
  lead.follow_up_date = dateStr; // shown at once; settled below
  let landed = false;
  try {
    await api.patch(`/api/leads/${lead.id}`, { follow_up_date: dateStr }, { successMessage: 'Call-back date saved' });
    landed = true;
  } catch {
    // useApi has already said why.
  }
  // Loads numbered above this went out after the PATCH settled, so their
  // rows already include it; anything at or below may predate it.
  const settledAt = leadsRequest;
  try {
    await loadLeads({ quiet: true });
  } catch {
    // The re-read failed too (network down); decided below like a stale one.
  }
  // Rows read after this PATCH settled are the server's word — leave them.
  // Otherwise they may predate it (this re-read failed, or a newer load is
  // still pending): the PATCH's own outcome is the best word until then.
  if (appliedRequest <= settledAt) {
    const outcome = landed ? dateStr : previous;
    lead.follow_up_date = outcome;
    const shown = leads.value.find((l) => l.id === lead.id);
    if (shown) shown.follow_up_date = outcome;
  }
  // An edit dialog open on this lead with its date untouched (still what it
  // opened with) shows the settled date. Form and snapshot move together, so
  // the date is still not sent unless the user changes it.
  const shownRow = leads.value.find((l) => l.id === lead.id);
  // Not in the table (e.g. filtered out): the PATCH outcome, not the stale row.
  const settled = shownRow ? shownRow.follow_up_date || null : landed ? dateStr : previous;
  if (showDialog.value && editingLead.value?.id === lead.id
      && (form.value.follow_up_date || null) === formOriginal.value?.follow_up_date) {
    form.value.follow_up_date = settled || '';
    formOriginal.value = { ...formOriginal.value, follow_up_date: settled };
  }
  // Only now: until the re-read settles, a re-pick or a dialog save on this
  // lead could interleave with it.
  followUpSaving.value = followUpSaving.value.filter((id) => id !== lead.id);
}

function nextStage(current) {
  const index = stageOrder.indexOf(current);
  if (index === -1 || index === stageOrder.length - 1) return null;
  return stageOrder[index + 1];
}

async function advanceStage(lead) {
  const target = nextStage(lead.stage);
  if (!target) return;
  advancingLeadId.value = lead.id;
  try {
    await api.post(`/api/leads/${lead.id}/advance-stage`, { stage: target }, { successMessage: `${lead.name} moved to ${stageLabel(target)}` });
    await refreshLeads();
  } finally {
    advancingLeadId.value = null;
  }
}

async function convertToCustomer(lead) {
  convertingLeadId.value = lead.id;
  try {
    await api.post(`/api/leads/${lead.id}/convert-to-customer`, null, { successMessage: `${lead.name} converted to customer` });
    await refreshLeads();
  } finally {
    convertingLeadId.value = null;
  }
}

async function convertLandingLead(landingLead) {
  landingConvertingId.value = landingLead.id;
  try {
    await api.post(`/api/landing-leads/${landingLead.id}/convert-to-lead`, null, { successMessage: `${landingLead.name} promoted to lead` });
    await loadLandingLeads();
    await refreshLeads();
  } finally {
    landingConvertingId.value = null;
  }
}

// ── Lead → the actual work (2026-08-08) ──────────────────────────────
// A web form is usually a service call or an estimate request, and often
// needs an email reply. These are the three exits.

// Opens the real inbox composer (NOT mailto:) so the reply lives in the
// customer's communication history. lead_id/landing_lead_id ride along so
// the inbox can record the contact after a SUCCESSFUL send — a delivery
// fact, never a button-click assertion. Safe to always pass: the server's
// record-contact only ever moves new → contacted.
function emailLead(lead) {
  router.push({
    path: '/inbox',
    query: {
      to: lead.email,
      subject: 'Your service request',
      lead_id: lead.id,
    },
  });
}

function emailLanding(ll) {
  showLandingDialog.value = false;
  router.push({
    path: '/inbox',
    query: {
      to: ll.email,
      subject: 'Your website inquiry',
      landing_lead_id: ll.id,
    },
  });
}

// Idempotent on the backend: reuses the lead's converted customer or an
// existing customer matched by email/phone before creating one. stage says
// what the conversion is for (service call = won, estimate = quoted).
// The linked-vs-created distinction is SAID out loud: a dedupe match is a
// merge decision, and the operator must know it happened.
async function ensureCustomer(lead, stage) {
  const conv = await api.post(`/api/leads/${lead.id}/convert-to-customer`, { stage });
  if (!conv?.converted || !conv?.customer_id) {
    toast.add({
      severity: 'error',
      summary: 'Customer conversion failed',
      detail: conv?.reason || 'Could not create a customer for this lead',
      life: 6000,
    });
    throw new Error(conv?.reason || 'convert failed');
  }
  if (conv.existing) {
    toast.add({
      severity: 'info',
      summary: 'Linked to existing customer',
      detail: `Using the existing record for ${conv.customer_name || lead.name} — no duplicate created.`,
      life: 5000,
    });
  }
  return conv.customer_id;
}

async function createServiceCall(lead) {
  serviceCallLeadId.value = lead.id;
  try {
    const customerId = await ensureCustomer(lead, 'won');
    // No scheduled_at: the job derives "Service Call" status and lands in
    // Ready to Schedule, so a dispatcher reviews it like any call-in.
    const job = await api.post(
      '/api/jobs',
      {
        title: `Service call — ${lead.name}`,
        description: lead.notes || '',
        customer_id: customerId,
      },
      { successMessage: 'Service call created — ready to schedule' },
    );
    if (job?.id) router.push(`/jobs/${job.id}`);
  } finally {
    serviceCallLeadId.value = null;
  }
}

async function createEstimateFromLead(lead) {
  estimateLeadId.value = lead.id;
  try {
    const customerId = await ensureCustomer(lead, 'quoted');
    // Route through /estimates/new (customer pre-selected) instead of
    // POSTing a bare estimate: line items go in through the real create
    // path, so no zero-line $0.00 estimate rows (the EST-000014 trap).
    // lead_id: the estimate belongs to this lead; accepting it wins the lead.
    router.push({ path: '/estimates/new', query: { customer_id: customerId, lead_id: lead.id } });
  } finally {
    estimateLeadId.value = null;
  }
}

async function startEstimateFromLead(lead) {
  startEstimateLeadId.value = lead.id;
  try {
    const res = await api.post(`/api/leads/${lead.id}/start-estimate`);
    if (res?.customer?.status === 'matched') {
      toast.add({
        severity: 'info',
        summary: 'Linked to existing customer',
        detail: `Using existing customer ${res.customer.name || ''}`,
        life: 5000,
      });
    }
    if (res?.estimate?.id) {
      showDialog.value = false;
      router.push(`/estimates/${res.estimate.id}`);
    }
  } catch {
    // useApi has already toasted the failure.
  } finally {
    startEstimateLeadId.value = null;
  }
}

// Landing-lead versions chain the promotion first, so the pipeline record
// exists and the audit trail reads submission → lead → customer → work.
async function landingServiceCall(ll) {
  landingActionId.value = ll.id;
  try {
    const lead = await api.post(`/api/landing-leads/${ll.id}/convert-to-lead`);
    showLandingDialog.value = false;
    await createServiceCall({ ...lead, stage: capitalize(lead.stage) });
  } finally {
    landingActionId.value = null;
  }
}

async function landingEstimate(ll) {
  landingActionId.value = ll.id;
  try {
    const lead = await api.post(`/api/landing-leads/${ll.id}/convert-to-lead`);
    showLandingDialog.value = false;
    await createEstimateFromLead({ ...lead, stage: capitalize(lead.stage) });
  } finally {
    landingActionId.value = null;
  }
}

// "I called them, they're not (yet) a pipeline lead" — the exit that used to
// not exist. Before this, the only ways out of status='new' were convert or
// delete, so a called-but-declined submission nagged the dashboard forever
// (or forced a fake Lead / a deleted contact record into the books).
async function markLandingContacted(landingLead) {
  landingContactingId.value = landingLead.id;
  try {
    await api.patch(
      `/api/landing-leads/${landingLead.id}/status`,
      { status: 'contacted' },
      { successMessage: `${landingLead.name || 'Lead'} marked contacted` },
    );
    await loadLandingLeads();
  } finally {
    landingContactingId.value = null;
  }
}

// "Handled, and it's done" — answered their question, booked them directly,
// or it duplicates a lead already worked. Unlike Contacted this is terminal;
// unlike Convert nothing enters the pipeline; unlike Spam/Delete the row
// stays visible as a real, honestly-handled inquiry.
async function markLandingCompleted(landingLead) {
  landingCompletingId.value = landingLead.id;
  try {
    await api.patch(
      `/api/landing-leads/${landingLead.id}/status`,
      { status: 'completed' },
      { successMessage: `${landingLead.name || 'Lead'} marked completed` },
    );
    await loadLandingLeads();
  } finally {
    landingCompletingId.value = null;
  }
}

async function completeFromDialog(landingLead) {
  await markLandingCompleted(landingLead);
  showLandingDialog.value = false;
}

// Undo for a misclicked Completed (it sits one button over from Contacted,
// and confirm dialogs are broken — issue #215). Returns the row to its last
// honest state: 'contacted' if an outreach actually happened, else 'new'.
async function reopenLanding(landingLead) {
  landingCompletingId.value = landingLead.id;
  try {
    await api.patch(
      `/api/landing-leads/${landingLead.id}/status`,
      { status: landingLead.contacted_at ? 'contacted' : 'new' },
      { successMessage: `${landingLead.name || 'Lead'} reopened` },
    );
    await loadLandingLeads();
  } finally {
    landingCompletingId.value = null;
  }
}

async function reopenFromDialog(landingLead) {
  await reopenLanding(landingLead);
  showLandingDialog.value = false;
}

function confirmDeleteLead(lead) {
  const who = lead.name || lead.email || 'this lead';
  confirmDestructive({
    header: 'Delete Lead',
    message: `Delete "${who}" from the sales pipeline?\n\nIt will be hidden from the leads list. This is a soft delete — the row stays in the database for audit.`,
    icon: 'pi pi-trash',
    acceptClass: 'p-button-danger',
    acceptLabel: 'Delete',
    rejectLabel: 'Cancel',
    accept: () => doDeleteLead(lead),
  });
}

async function doDeleteLead(lead) {
  deletingLeadId.value = lead.id;
  try {
    await api.del(`/api/leads/${lead.id}`, { successMessage: `${lead.name || 'Lead'} deleted` });
    // Optimistic local removal so the UI updates before the refresh round-trip.
    leads.value = leads.value.filter((r) => r.id !== lead.id);
    await refreshLeads();
  } finally {
    deletingLeadId.value = null;
  }
}

function confirmDeleteLanding(landingLead, reason) {
  const who = landingLead.name || landingLead.email || 'this submission';
  const headline = reason === 'spam'
    ? `Mark "${who}" as spam?`
    : `Delete "${who}"?`;
  const body = reason === 'spam'
    ? 'It will be hidden from the leads list and flagged in the audit log as spam.'
    : 'It will be hidden from the leads list. This is a soft delete — the row stays in the database for audit.';
  confirmDestructive({
    header: reason === 'spam' ? 'Mark as Spam' : 'Delete Landing Lead',
    message: `${headline}\n\n${body}`,
    icon: reason === 'spam' ? 'pi pi-times-circle' : 'pi pi-trash',
    acceptClass: 'p-button-danger',
    acceptLabel: reason === 'spam' ? 'Mark as Spam' : 'Delete',
    rejectLabel: 'Cancel',
    accept: () => doDeleteLanding(landingLead, reason),
  });
}

async function doDeleteLanding(landingLead, reason) {
  landingDeletingId.value = landingLead.id;
  try {
    const successMessage = reason === 'spam'
      ? `${landingLead.name || 'Submission'} marked as spam`
      : `${landingLead.name || 'Submission'} deleted`;
    await api.del(
      `/api/landing-leads/${landingLead.id}?reason=${encodeURIComponent(reason)}`,
      { successMessage },
    );
    // Optimistic: remove locally so the UI updates before refetch.
    landingLeads.value = landingLeads.value.filter((r) => r.id !== landingLead.id);
    await loadLandingLeads();
  } finally {
    landingDeletingId.value = null;
  }
}

onMounted(async () => {
  if (route.query.follow_up) {
    const q = String(route.query.follow_up).toLowerCase();
    if (q === 'due' || q === 'overdue') {
      dueFilter.value = 'overdue';
    } else if (['today', 'upcoming'].includes(q)) {
      dueFilter.value = q;
    }
  }
  await Promise.all([refreshLeads(), loadLandingLeads()]);
  // The estimate's Customer Request panel links here with ?id=<lead>.
  if (route.query.id) {
    const id = String(route.query.id);
    let lead = leads.value.find((l) => l.id === id);
    if (!lead) {
      // Past the first page, or outside the due filter: fetch it directly.
      const one = await api.get(`/api/leads/${encodeURIComponent(id)}`).catch(() => null);
      if (one?.id) lead = { ...one, stage: capitalize(one.stage) || 'New' };
    }
    if (lead) openEdit(lead);
  }
});
</script>

<style scoped>
.page-subtitle {
  margin: 0.25rem 0 0;
  color: var(--p-text-muted-color);
  font-size: 0.85rem;
  max-width: 56rem;
}
.page-subtitle a { color: var(--p-primary-color); text-decoration: none; }
.page-subtitle a:hover { text-decoration: underline; }

.leads-view .pipeline-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 1rem;
  margin: 1rem 0 1.5rem;
}

.pipeline-card {
  text-align: center;
}

.pipeline-label {
  font-size: 0.85rem;
  color: var(--p-text-muted-color, #6b7280);
}

.stat-value {
  font-size: 2rem;
  font-weight: 600;
  margin-top: 0.4rem;
}

.filters-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 1rem;
  margin-bottom: 1rem;
}

.stage-tabs {
  flex: 1;
  min-width: 280px;
}

.due-filter-box {
  min-width: 170px;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem 0;
}

.row-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.inline-date-cell {
  display: flex;
  align-items: center;
}

.inline-follow-up-picker :deep(input) {
  padding: 0.25rem 0.5rem;
  font-size: 0.85rem;
  height: 2rem;
}

.date-overdue :deep(input),
.text-overdue {
  color: var(--p-red-500, #ef4444);
  font-weight: 600;
}

.date-today :deep(input),
.text-today {
  color: var(--p-amber-600, #d97706);
  font-weight: 600;
}

.custom-fields-block {
  margin-top: 0.5rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--p-content-border-color);
}

.custom-fields-title {
  font-size: 0.95rem;
  font-weight: 600;
  margin: 0 0 0.75rem 0;
  color: var(--p-text-color);
}

.custom-fields-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 0.75rem;
}

.bool-field {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding-top: 0.25rem;
}

.landing-section {
  margin-top: 2rem;
}

.landing-header {
  display: flex;
  flex-direction: column;
  margin-bottom: 1rem;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
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

.clickable-row .p-datatable-tbody > tr {
  cursor: pointer;
}

.tab-label {
  display: flex;
  gap: 0.4rem;
  align-items: center;
}

.spinner-wrap.small {
  padding: 1rem 0;
}

.landing-table :deep(.p-datatable-tbody > tr) {
  cursor: pointer;
}

.landing-detail {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.ld-actions {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--p-content-border-color);
}

.ld-row {
  display: flex;
  gap: 0.75rem;
  align-items: baseline;
}

.ld-label {
  flex: 0 0 6.5rem;
  font-weight: 600;
  color: var(--p-text-muted-color);
  font-size: 0.85rem;
}

.ld-muted {
  color: var(--p-text-muted-color);
  word-break: break-word;
}

.ld-message {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin: 0.4rem 0;
}

.ld-message-body {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--p-content-hover-background);
  border-radius: 6px;
  padding: 0.75rem;
  font-size: 0.95rem;
  line-height: 1.45;
}
</style>
