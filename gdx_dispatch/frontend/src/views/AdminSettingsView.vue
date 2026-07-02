<template>
    <section class='admin-settings-view view-card'>
      <header class='view-header'>
        <h2 class='page-title'>Admin Settings</h2>
      </header>

      <Tabs v-model:value='activeTab'>
        <TabList class='settings-tab-list'>
          <Tab value='email' data-testid='admin-tab-email'>Email Settings</Tab>
          <Tab value='tax' data-testid='admin-tab-tax'>Tax Jurisdictions</Tab>
          <Tab value='audit' data-testid='admin-tab-audit'>Audit Log</Tab>
        </TabList>
        <TabPanels>
          <TabPanel value='email'>
            <div class='tab-content'>
              <div v-if='emailLoading' class='spinner-wrap'>
                <ProgressSpinner />
              </div>
              <div v-else class='email-panels'>
                <div class='form-card-grid'>
                  <section class='form-card'>
                    <header class='form-card-header'>
                      <h3>SMTP</h3>
                    </header>
                    <div class='form-grid'>
                      <div class='form-field'>
                        <label>Host</label>
                        <InputText v-model='emailSettings.smtp.host' class='w-full' data-testid='admin-email-smtp-host' />
                      </div>
                      <div class='form-field'>
                        <label>Port</label>
                        <InputNumber
                          v-model='emailSettings.smtp.port'
                          class='w-full'
                          :min='0'
                          :max='65535'
                          :use-input='true'
                          mode='decimal'
                          data-testid='admin-email-smtp-port'
                        />
                      </div>
                      <div class='form-field'>
                        <label>Username</label>
                        <InputText v-model='emailSettings.smtp.username' class='w-full' data-testid='admin-email-smtp-username' />
                      </div>
                      <div class='form-field'>
                        <label>Password</label>
                        <InputText
                          v-model='emailSettings.smtp.password'
                          type='password'
                          class='w-full'
                          data-testid='admin-email-smtp-password'
                        />
                      </div>
                      <div class='form-field'>
                        <label>From name</label>
                        <InputText v-model='emailSettings.smtp.from_name' class='w-full' data-testid='admin-email-smtp-from-name' />
                      </div>
                      <div class='form-field'>
                        <label>From email</label>
                        <InputText v-model='emailSettings.smtp.from_email' class='w-full' data-testid='admin-email-smtp-from-email' />
                      </div>
                    </div>
                  </section>

                  <section class='form-card'>
                    <header class='form-card-header'>
                      <h3>IMAP</h3>
                    </header>
                    <div class='form-grid'>
                      <div class='form-field'>
                        <label>Host</label>
                        <InputText v-model='emailSettings.imap.host' class='w-full' data-testid='admin-email-imap-host' />
                      </div>
                      <div class='form-field'>
                        <label>Username</label>
                        <InputText v-model='emailSettings.imap.username' class='w-full' data-testid='admin-email-imap-username' />
                      </div>
                      <div class='form-field'>
                        <label>Password</label>
                        <InputText
                          v-model='emailSettings.imap.password'
                          type='password'
                          class='w-full'
                          data-testid='admin-email-imap-password'
                        />
                      </div>
                    </div>
                  </section>
                </div>

                <div class='form-actions'>
                  <Button
                    label='Test Email'
                    icon='pi pi-envelope'
                    severity='secondary'
                    data-testid='admin-email-test'
                    @click='showTestDialog = true'
                  />
                  <Button
                    label='Save Settings'
                    icon='pi pi-save'
                    class='primary-action'
                    :loading='emailSaving'
                    @click='saveEmailSettings'
                    data-testid='admin-email-save'
                  />
                </div>
              </div>
            </div>
          </TabPanel>

          <TabPanel value='tax'>
            <div class='tab-content'>
              <div class='tax-actions'>
                <Button
                  label='+ Add'
                  icon='pi pi-plus'
                  class='p-button-sm'
                  data-testid='admin-tax-add'
                  @click='openJurisdictionDialog()'
                />
              </div>
              <div v-if='taxLoading' class='spinner-wrap'>
                <ProgressSpinner />
              </div>
              <DataTable
      responsiveLayout="scroll"
                v-else
                :value='jurisdictions'
                dataKey='id'
                striped-rows
                responsive-layout='scroll'
                class='full-width-table'
                data-testid='admin-tax-table'
              >
                <Column field='name' header='Name' />
                <Column field='rate' header='Rate (%)' style='width:140px'>
                  <template #body='{ data }'>{{ formatRate(data.rate) }}</template>
                </Column>
                <Column field='state' header='State' />
                <Column field='county' header='County' />
                <Column field='city' header='City' />
                <Column header='Active' style='width:150px'>
                  <template #body='{ data }'>
                    <ToggleSwitch
                      :checked='data.active'
                      @change='toggleJurisdictionActive(data)'
                      :data-testid="'admin-tax-active-' + data.id"
                    />
                  </template>
                </Column>
                <Column header='Actions' style='width:220px'>
                  <template #body='{ data }'>
                    <Button
                      text
                      size='small'
                      label='Edit'
                      icon='pi pi-pencil'
                      class='p-button-text'
                      :data-testid="'admin-tax-edit-' + data.id"
                      @click='openJurisdictionDialog(data)'
                    />
                    <Button
                      text
                      size='small'
                      label='Delete'
                      icon='pi pi-trash'
                      severity='danger'
                      class='p-button-text'
                      :loading='deletingJurisdictionId === data.id'
                      :data-testid="'admin-tax-delete-' + data.id"
                      @click='deleteJurisdiction(data)'
                    />
                  </template>
                </Column>
              </DataTable>
            </div>
          </TabPanel>

          <TabPanel value='audit'>
            <div class='tab-content'>
              <div class='filter-row'>
                <Select
                  v-model='auditFilters.action'
                  :options='auditActionOptions'
                  option-label='label'
                  option-value='value'
                  placeholder='Action'
                  class='filter-field'
                  data-testid='admin-audit-action-select'
                />
                <Select
                  v-model='auditFilters.user'
                  :options='auditUserOptions'
                  option-label='label'
                  option-value='value'
                  placeholder='User'
                  class='filter-field'
                  data-testid='admin-audit-user-select'
                />
                <Select
                  v-model='auditFilters.entityType'
                  :options='auditEntityTypeOptions'
                  option-label='label'
                  option-value='value'
                  placeholder='Entity'
                  class='filter-field'
                  data-testid='admin-audit-entity-select'
                />
                <DatePicker
                  v-model='auditDateRange'
                  selectionMode='range'
                  dateFormat='yy-mm-dd'
                  placeholder='Date range'
                  class='filter-field'
                  data-testid='admin-audit-date-range'
                />
                <Button
                  label='Clear filters'
                  severity='secondary'
                  text
                  data-testid='admin-audit-clear'
                  @click='clearAuditFilters'
                />
              </div>

              <DataTable
      responsiveLayout="scroll"
                :value='auditEntries'
                dataKey='id'
                striped-rows
                responsive-layout='scroll'
                :loading='auditLoading'
                empty-message='No audit entries found'
                class='full-width-table'
                data-testid='admin-audit-table'
              >
                <Column field='timestamp' header='Timestamp'>
                  <template #body='{ data }'>{{ formatTimestamp(data.timestamp) }}</template>
                </Column>
                <Column field='user' header='User' />
                <Column field='action' header='Action' />
                <Column field='entity_type' header='Entity Type'>
                  <template #body='{ data }'>{{ humanize(data.entity_type) }}</template>
                </Column>
                <Column field='entity_id' header='Entity ID' />
                <Column field='details' header='Details'>
                  <template #body='{ data }'>{{ truncate(data.details) }}</template>
                </Column>
              </DataTable>

              <Paginator
                :first='auditFirst'
                :rows='auditLimit'
                :totalRecords='auditTotal'
                @page='handleAuditPage'
                data-testid='admin-audit-paginator'
              />
            </div>
          </TabPanel>
        </TabPanels>
      </Tabs>

      <Dialog
        v-model:visible='showTestDialog'
        header='Send test email'
        :style="{ width: '420px' }"
        modal
        data-testid='admin-email-test-dialog'
      >
        <div class='form-field'>
          <label>Recipient email</label>
          <InputText v-model='testEmailForm.to_email' class='w-full' data-testid='admin-email-test-input' />
        </div>
        <template #footer>
          <Button
            label='Cancel'
            severity='secondary'
            data-testid='admin-email-test-cancel'
            @click='showTestDialog = false'
          />
          <Button
            label='Send test'
            icon='pi pi-paper-plane'
            :loading='testingEmail'
            :disabled='!testEmailForm.to_email'
            @click='sendTestEmail'
            data-testid='admin-email-test-send'
          />
        </template>
      </Dialog>

      <Dialog
        v-model:visible='showJurisdictionDialog'
        :header='jurisdictionDialogTitle'
        :style="{ width: '640px' }"
        modal
        data-testid='admin-tax-dialog'
        @hide='closeJurisdictionDialog'
      >
        <div class='form-grid'>
          <div class='form-field'>
            <label>Name</label>
            <InputText v-model='jurisdictionForm.name' class='w-full' data-testid='admin-tax-name' />
          </div>
          <div class='form-field'>
            <label>Rate (%)</label>
            <InputNumber
              v-model='jurisdictionForm.rate'
              class='w-full'
              mode='decimal'
              :min='0'
              :max='100'
              :use-input='true'
              step='0.01'
              data-testid='admin-tax-rate'
            />
          </div>
          <div class='form-field'>
            <label>State</label>
            <InputText v-model='jurisdictionForm.state' class='w-full' data-testid='admin-tax-state' />
          </div>
          <div class='form-field'>
            <label>County</label>
            <InputText v-model='jurisdictionForm.county' class='w-full' data-testid='admin-tax-county' />
          </div>
          <div class='form-field'>
            <label>City</label>
            <InputText v-model='jurisdictionForm.city' class='w-full' data-testid='admin-tax-city' />
          </div>
          <div class='form-field'>
            <label>Active</label>
            <ToggleSwitch
              v-model='jurisdictionForm.active'
              data-testid='admin-tax-active-toggle'
            />
          </div>
        </div>
        <template #footer>
          <Button
            label='Cancel'
            severity='secondary'
            data-testid='admin-tax-cancel'
            @click='closeJurisdictionDialog'
          />
          <Button
            :label="editingJurisdiction ? 'Save changes' : 'Create jurisdiction'"
            icon='pi pi-check'
            :loading='savingJurisdiction'
            @click='saveJurisdiction'
            data-testid='admin-tax-save'
          />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatDateTime as formatTimestamp, formatPercent as fmtPercent } from '../composables/useFormatters';
import Button from 'primevue/button';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import Select from 'primevue/select';
import ToggleSwitch from 'primevue/toggleswitch';
import DatePicker from 'primevue/datepicker';
import Tag from 'primevue/tag';
import Paginator from 'primevue/paginator';
import ProgressSpinner from 'primevue/progressspinner';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';

const { confirmDestructive } = useDestructiveConfirm();
const api = useApiWithToast();
const activeTab = ref('email');

const emailSettings = reactive({
  smtp: {
    host: '',
    port: null,
    username: '',
    password: '',
    from_name: '',
    from_email: '',
  },
  imap: {
    host: '',
    username: '',
    password: '',
  },
});
const emailLoading = ref(false);
const emailSaving = ref(false);
const showTestDialog = ref(false);
const testingEmail = ref(false);
const testEmailForm = reactive({ to_email: '' });

const jurisdictions = ref([]);
const taxLoading = ref(false);
const showJurisdictionDialog = ref(false);
const editingJurisdiction = ref(null);
const jurisdictionForm = reactive({
  id: null,
  name: '',
  rate: null,
  state: '',
  county: '',
  city: '',
  active: true,
});
const savingJurisdiction = ref(false);
const deletingJurisdictionId = ref(null);
const jurisdictionDialogTitle = computed(() =>
  editingJurisdiction.value ? 'Edit Jurisdiction' : 'Add Jurisdiction'
);

const auditEntries = ref([]);
const auditLoading = ref(false);
const auditLimit = 50;
const auditPage = ref(1);
const auditTotal = ref(0);
const auditFilters = reactive({ action: null, user: null, entityType: null });
const auditDateRange = ref([]);
const auditActionOptions = ref([{ label: 'All actions', value: null }]);
const auditUserOptions = ref([{ label: 'All users', value: null }]);
const auditEntityTypeOptions = ref([{ label: 'All entities', value: null }]);
const auditFirst = computed(() => (auditPage.value - 1) * auditLimit);

const humanize = (value) => {
  if (!value) return 'Unknown';
  return value
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ');
};

const formatRate = (value) => fmtPercent(value, { whole: true, digits: 2 });

const truncate = (value) => {
  if (!value) return '—';
  return value.length > 80 ? `${value.slice(0, 80)}…` : value;
};

const formatFilterDate = (date) => {
  if (!date) return null;
  const iso = date.toISOString();
  return iso.split('T')[0];
};

const buildOptions = (values, labeler = (value) => value) => {
  const unique = Array.from(new Set(values.filter(Boolean)));
  return unique.map((value) => ({ label: labeler(value), value }));
};

const loadEmailSettings = async () => {
  emailLoading.value = true;
  try {
    const response = await api.get('/api/admin/settings/email');
    if (response?.smtp) {
      Object.assign(emailSettings.smtp, response.smtp);
    }
    if (response?.imap) {
      Object.assign(emailSettings.imap, response.imap);
    }
  } catch (err) {
    console.error('load_email_settings_failed', err?.message || err);
  } finally {
    emailLoading.value = false;
  }
};

const saveEmailSettings = async () => {
  emailSaving.value = true;
  try {
    await api.patch('/api/admin/settings/email', emailSettings, { successMessage: 'Email settings updated' });
  } finally {
    emailSaving.value = false;
  }
};

const sendTestEmail = async () => {
  testingEmail.value = true;
  try {
    await api.post(
      '/api/admin/settings/email/test',
      { to_email: testEmailForm.to_email },
      { successMessage: 'Test email sent' }
    );
    testEmailForm.to_email = '';
    showTestDialog.value = false;
  } finally {
    testingEmail.value = false;
  }
};

const resetJurisdictionForm = () => {
  Object.assign(jurisdictionForm, {
    id: null,
    name: '',
    rate: null,
    state: '',
    county: '',
    city: '',
    active: true,
  });
  editingJurisdiction.value = null;
};

const openJurisdictionDialog = (jurisdiction = null) => {
  if (jurisdiction) {
    editingJurisdiction.value = jurisdiction;
    Object.assign(jurisdictionForm, {
      id: jurisdiction.id,
      name: jurisdiction.name ?? '',
      rate: jurisdiction.rate ?? null,
      state: jurisdiction.state ?? '',
      county: jurisdiction.county ?? '',
      city: jurisdiction.city ?? '',
      active: jurisdiction.active ?? true,
    });
  } else {
    resetJurisdictionForm();
  }
  showJurisdictionDialog.value = true;
};

const closeJurisdictionDialog = () => {
  showJurisdictionDialog.value = false;
  resetJurisdictionForm();
};

const loadTaxJurisdictions = async () => {
  taxLoading.value = true;
  try {
    const response = await api.get('/api/admin/tax-jurisdictions');
    jurisdictions.value = Array.isArray(response)
      ? response
      : response?.items || response?.data || [];
  } catch (err) {
    console.error('load_tax_jurisdictions_failed', err?.message || err);
    jurisdictions.value = [];
  } finally {
    taxLoading.value = false;
  }
};

const saveJurisdiction = async () => {
  savingJurisdiction.value = true;
  try {
    const payload = {
      name: jurisdictionForm.name,
      rate: jurisdictionForm.rate,
      state: jurisdictionForm.state,
      county: jurisdictionForm.county,
      city: jurisdictionForm.city,
      active: jurisdictionForm.active,
    };
    if (jurisdictionForm.id) {
      await api.patch(
        `/api/admin/tax-jurisdictions/${jurisdictionForm.id}`,
        payload,
        { successMessage: 'Jurisdiction updated' }
      );
    } else {
      await api.post('/api/admin/tax-jurisdictions', payload, { successMessage: 'Jurisdiction added' });
    }
    await loadTaxJurisdictions();
    closeJurisdictionDialog();
  } finally {
    savingJurisdiction.value = false;
  }
};

const toggleJurisdictionActive = async (jurisdiction) => {
  const targetState = !jurisdiction.active;
  try {
    await api.patch(
      `/api/admin/tax-jurisdictions/${jurisdiction.id}`,
      { active: targetState },
      { successMessage: 'Jurisdiction status updated' }
    );
    jurisdiction.active = targetState;
  } catch {
    // error handled by useApiWithToast
  }
};

const deleteJurisdiction = (jurisdiction) => {
  confirmDestructive({
    message: `Delete jurisdiction "${jurisdiction.name || jurisdiction.id}"? This cannot be undone.`,
    header: 'Confirm Delete',
    accept: async () => {
      deletingJurisdictionId.value = jurisdiction.id;
      try {
        await api.del(`/api/admin/tax-jurisdictions/${jurisdiction.id}`, { successMessage: 'Jurisdiction removed' });
        await loadTaxJurisdictions();
      } finally {
        deletingJurisdictionId.value = null;
      }
    },
  });
};

const updateAuditOptions = (entries) => {
  auditActionOptions.value = [
    { label: 'All actions', value: null },
    ...buildOptions(entries.map((item) => item.action), humanize),
  ];
  auditUserOptions.value = [
    { label: 'All users', value: null },
    ...buildOptions(entries.map((item) => item.user)),
  ];
  auditEntityTypeOptions.value = [
    { label: 'All entities', value: null },
    ...buildOptions(entries.map((item) => item.entity_type), humanize),
  ];
};

const loadAuditLog = async () => {
  auditLoading.value = true;
  try {
    const params = new URLSearchParams();
    params.append('page', auditPage.value.toString());
    params.append('limit', auditLimit.toString());
    if (auditFilters.action) params.append('action', auditFilters.action);
    if (auditFilters.user) params.append('user', auditFilters.user);
    if (auditFilters.entityType) params.append('entity_type', auditFilters.entityType);
    const [start, end] = auditDateRange.value;
    if (start) params.append('start_date', formatFilterDate(start));
    if (end) params.append('end_date', formatFilterDate(end));
    const query = params.toString();
    const endpoint = query ? `/api/admin/audit-log?${query}` : '/api/admin/audit-log';
    const response = await api.get(endpoint);
    const items = Array.isArray(response)
      ? response
      : response?.items || response?.data || [];
    auditEntries.value = items;
    auditTotal.value = response?.total ?? response?.meta?.total ?? items.length;
    updateAuditOptions(items);
  } catch (err) {
    console.error('load_audit_log_failed', err?.message || err);
    auditEntries.value = [];
  } finally {
    auditLoading.value = false;
  }
};

const clearAuditFilters = () => {
  const dateRangeLength = auditDateRange.value?.length ?? 0;
  const hadFilters = auditFilters.action || auditFilters.user || auditFilters.entityType || dateRangeLength;
  auditFilters.action = null;
  auditFilters.user = null;
  auditFilters.entityType = null;
  auditDateRange.value = [];
  auditPage.value = 1;
  if (!hadFilters) {
    loadAuditLog();
  }
};

const handleAuditPage = (event) => {
  auditPage.value = event.page + 1;
  loadAuditLog();
};

watch(
  () => [auditFilters.action, auditFilters.user, auditFilters.entityType],
  () => {
    auditPage.value = 1;
    loadAuditLog();
  }
);

watch(
  auditDateRange,
  () => {
    auditPage.value = 1;
    loadAuditLog();
  },
  { deep: true }
);

onMounted(() => {
  loadEmailSettings();
  loadTaxJurisdictions();
  loadAuditLog();
});
</script>

<style scoped>
.admin-settings-view .view-header {
  margin-bottom: 1rem;
}

.settings-tab-list {
  margin-bottom: 1rem;
}

.tab-content {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.email-panels {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.form-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 1rem;
}

.form-card {
  background: var(--p-content-background);
  color: var(--p-text-color);
  border: 1px solid var(--p-content-border-color);
  border-radius: 0.5rem;
  padding: 1rem;
}

.form-card-header h3 {
  color: var(--p-text-color);
  margin: 0 0 0.25rem 0;
  font-size: 0.95rem;
  font-weight: 600;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.form-field label {
  color: var(--p-text-muted-color);
  font-size: 0.8rem;
  font-weight: 500;
}

/* Inputs sit on top of .form-card; ensure they share the surrounding
   theme rather than the white-on-dark inputs the screenshot showed. */
.form-card :deep(.p-inputtext),
.form-card :deep(.p-password input),
.form-card :deep(.p-select),
.form-card :deep(textarea) {
  background: var(--p-form-field-background);
  color: var(--p-text-color);
  border-color: var(--p-form-field-border-color);
}

.form-card-header {
  margin-bottom: 0.5rem;
}

.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 1rem;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
}

/* Removed: .primary-action override — let PrimeVue Button defaults paint
   the primary color. Hardcoded #2563eb conflicted with theme tokens. */

.tax-actions {
  display: flex;
  justify-content: flex-end;
}

.full-width-table {
  width: 100%;
}

.filter-row {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
}

.filter-field {
  min-width: 180px;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem 0;
}
</style>
