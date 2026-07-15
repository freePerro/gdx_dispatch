<template>
  <section class="accounting-settings-view view-card">
    <header class="view-header">
      <h2 class="page-title">Accounting Settings</h2>
      <p class="page-subtitle">
        Chart of accounts, posting maps, and accounting policy — review with your CPA
        before enabling ledger posting. Locked fields follow QuickBooks parity: once the
        ledger is live they can't change.
      </p>
    </header>

    <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>

    <div v-else class="settings-stack">
      <!-- Master switch -->
      <section class="form-card" data-testid="gl-master-switch-card">
        <header class="form-card-header">
          <h3>Ledger Posting</h3>
          <Tag
            :severity="settings.ledger_posting_enabled ? 'success' : 'secondary'"
            :value="settings.ledger_posting_enabled ? 'ENABLED' : 'OFF'"
            data-testid="gl-posting-status"
          />
        </header>
        <p class="hint">
          The master switch. While off, nothing posts to the ledger and every setting
          stays editable. Enabling is a one-way door once entries exist.
        </p>
        <div class="switch-row">
          <Button
            v-if="!settings.ledger_posting_enabled"
            label="Enable posting…"
            severity="danger"
            data-testid="gl-enable-posting-btn"
            @click="enableDialogOpen = true"
          />
          <Button
            v-else-if="!meta.entries_exist"
            label="Disable posting (ledger still empty)"
            severity="secondary"
            outlined
            data-testid="gl-disable-posting-btn"
            @click="disablePosting"
          />
          <span v-else class="hint" data-testid="gl-one-way-door-note">
            Journal entries exist — posting can no longer be disabled.
          </span>
        </div>
      </section>

      <!-- Policy -->
      <section class="form-card" data-testid="gl-policy-card">
        <header class="form-card-header"><h3>Policy</h3></header>
        <div class="form-grid">
          <div class="form-field">
            <label>Reporting basis</label>
            <Select
              v-model="settings.reporting_basis"
              :options="bases"
              class="w-full"
              data-testid="gl-reporting-basis"
              @change="save({ reporting_basis: settings.reporting_basis })"
            />
          </div>
          <div class="form-field">
            <label>Tax basis <CpaStamp k="tax_basis" /></label>
            <Select
              v-model="settings.tax_basis"
              :options="bases"
              class="w-full"
              data-testid="gl-tax-basis"
              @change="save({ tax_basis: settings.tax_basis })"
            />
          </div>
          <div class="form-field">
            <label>
              Inventory treatment <CpaStamp k="inventory_treatment" />
              <LockNote field="inventory_treatment" />
            </label>
            <Select
              v-model="settings.inventory_treatment"
              :options="inventoryTreatments"
              :disabled="isLocked('inventory_treatment')"
              class="w-full"
              data-testid="gl-inventory-treatment"
              @change="save({ inventory_treatment: settings.inventory_treatment })"
            />
          </div>
          <div class="form-field">
            <label>Cutover month <LockNote field="cutover_month" /></label>
            <DatePicker
              v-model="cutoverMonth"
              view="month"
              date-format="yy-mm"
              :disabled="isLocked('cutover_month')"
              show-icon
              class="w-full"
              data-testid="gl-cutover-month"
              @update:model-value="saveCutover"
            />
          </div>
          <div class="form-field">
            <label>Entity type <CpaStamp k="entity_type" /></label>
            <InputText
              v-model="settings.entity_type"
              class="w-full"
              placeholder="e.g. S-Corp, LLC, Sole Prop"
              data-testid="gl-entity-type"
              @blur="saveEntityType"
            />
          </div>
          <div class="form-field">
            <label>Opening bank balance attestation</label>
            <div class="attest-row">
              <template v-if="settings.opening_bank_attested_at">
                <Tag severity="success" value="Attested" />
                <span class="hint">{{ formatDateTime(settings.opening_bank_attested_at) }}</span>
              </template>
              <Button
                v-else
                label="Attest current bank balances are entered"
                size="small"
                outlined
                data-testid="gl-attest-btn"
                @click="save({ attest_opening_bank: true })"
              />
            </div>
          </div>
        </div>
      </section>

      <!-- Payment method map -->
      <section class="form-card" data-testid="gl-payment-map-card">
        <header class="form-card-header">
          <h3>Payment method → account role <CpaStamp k="payment_method_role_map" /></h3>
          <LockNote field="payment_method_role_map" />
        </header>
        <p class="hint">
          Where each payment method lands when recorded: Undeposited Funds until a bank
          deposit clears it, or straight into the Operating Bank.
        </p>
        <div class="map-grid">
          <div v-for="(role, method) in settings.payment_method_role_map" :key="method" class="map-row">
            <span class="map-key">{{ method }}</span>
            <Select
              :model-value="role"
              :options="paymentRoles"
              :disabled="isLocked('payment_method_role_map')"
              class="map-value"
              :data-testid="`gl-paymap-${method}`"
              @update:model-value="(v) => saveMap('payment_method_role_map', method, v)"
            />
          </div>
        </div>
      </section>

      <!-- Credit reason map -->
      <section class="form-card" data-testid="gl-credit-map-card">
        <header class="form-card-header">
          <h3>Credit / refund reason → contra-revenue <CpaStamp k="credit_reason_role_map" /></h3>
        </header>
        <p class="hint">
          Discounts (4900) vs Refunds &amp; Allowances (4910) — split so goodwill and
          warranty credits don't misstate the discounts line.
        </p>
        <div class="map-grid">
          <div v-for="(role, reason) in settings.credit_reason_role_map" :key="reason" class="map-row">
            <span class="map-key">{{ reason }}</span>
            <Select
              :model-value="role"
              :options="creditRoles"
              class="map-value"
              :data-testid="`gl-creditmap-${reason}`"
              @update:model-value="(v) => saveMap('credit_reason_role_map', reason, v)"
            />
          </div>
        </div>
      </section>

      <!-- Expense category map -->
      <section class="form-card" data-testid="gl-expense-map-card">
        <header class="form-card-header"><h3>Expense category → account</h3></header>
        <div class="map-grid">
          <div v-for="(acctId, category) in settings.expense_category_account_map" :key="category" class="map-row">
            <span class="map-key">{{ category }}</span>
            <Select
              :model-value="acctId"
              :options="expenseAccountOptions"
              option-label="label"
              option-value="value"
              class="map-value"
              :data-testid="`gl-expmap-${category}`"
              @update:model-value="(v) => saveMap('expense_category_account_map', category, v)"
            />
          </div>
        </div>
      </section>

      <!-- Chart of accounts -->
      <section class="form-card" data-testid="gl-coa-card">
        <header class="form-card-header">
          <h3>Chart of Accounts</h3>
          <Button label="Add account" size="small" icon="pi pi-plus" data-testid="gl-add-account-btn" @click="openAdd" />
        </header>
        <p class="hint">
          System accounts (badged) own a posting role — rename or renumber them freely,
          but they can never be deactivated or deleted. Accounts are deactivated, never
          deleted, once used.
        </p>
        <DataTable :value="accounts" data-key="id" size="small" striped-rows>
          <Column field="code" header="Code" sortable style="width: 6rem" />
          <Column field="name" header="Name" sortable />
          <Column field="type" header="Type" sortable style="width: 8rem" />
          <Column header="Role" style="width: 12rem">
            <template #body="{ data }">
              <Tag v-if="data.role" severity="info" :value="data.role" />
            </template>
          </Column>
          <Column header="System" style="width: 6rem">
            <template #body="{ data }">
              <Tag v-if="data.is_system" severity="warning" value="system" />
            </template>
          </Column>
          <Column header="Active" style="width: 6rem">
            <template #body="{ data }">
              <Tag :severity="data.active ? 'success' : 'secondary'" :value="data.active ? 'active' : 'inactive'" />
            </template>
          </Column>
          <Column style="width: 5rem">
            <template #body="{ data }">
              <Button
                icon="pi pi-pencil"
                text
                size="small"
                :data-testid="`gl-edit-account-${data.code}`"
                @click="openEdit(data)"
              />
            </template>
          </Column>
        </DataTable>
      </section>
    </div>

    <!-- Enable-posting one-way-door confirm -->
    <Dialog
      v-model:visible="enableDialogOpen"
      header="Enable ledger posting"
      modal
      :style="{ width: '30rem' }"
    >
      <p>
        From this point every invoice, payment, credit, and expense posts immutable
        journal entries. Locked policy fields (inventory treatment, cutover month,
        payment map) can no longer change. <strong>This is a one-way door once
        entries exist.</strong>
      </p>
      <p class="hint">Type <code>{{ meta.enable_confirm_phrase }}</code> to confirm:</p>
      <InputText v-model="enableConfirmText" class="w-full" data-testid="gl-enable-confirm-input" />
      <template #footer>
        <Button label="Cancel" text @click="enableDialogOpen = false" />
        <Button
          label="Enable posting"
          severity="danger"
          :disabled="enableConfirmText !== meta.enable_confirm_phrase"
          data-testid="gl-enable-confirm-btn"
          @click="enablePosting"
        />
      </template>
    </Dialog>

    <!-- Account add/edit -->
    <Dialog
      v-model:visible="accountDialogOpen"
      :header="editingAccount ? `Edit ${editingAccount.code}` : 'Add account'"
      modal
      :style="{ width: '26rem' }"
    >
      <div class="form-grid">
        <div class="form-field">
          <label>Code</label>
          <InputText v-model="accountForm.code" class="w-full" data-testid="gl-account-code" />
        </div>
        <div class="form-field">
          <label>Name</label>
          <InputText v-model="accountForm.name" class="w-full" data-testid="gl-account-name" />
        </div>
        <div v-if="!editingAccount" class="form-field">
          <label>Type</label>
          <Select v-model="accountForm.type" :options="accountTypes" class="w-full" data-testid="gl-account-type" />
        </div>
        <div v-if="editingAccount && !editingAccount.is_system" class="form-field">
          <label>Active</label>
          <ToggleSwitch v-model="accountForm.active" data-testid="gl-account-active" />
        </div>
        <p v-if="editingAccount && editingAccount.is_system" class="hint">
          System account ({{ editingAccount.role }}) — rename/renumber only; it can’t be
          deactivated because the posting engine resolves it by role.
        </p>
      </div>
      <template #footer>
        <Button label="Cancel" text @click="accountDialogOpen = false" />
        <Button label="Save" data-testid="gl-account-save" @click="saveAccount" />
      </template>
    </Dialog>
  </section>
</template>

<script setup>
import { computed, defineComponent, h, onMounted, reactive, ref } from 'vue';
import Button from 'primevue/button';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import ToggleSwitch from 'primevue/toggleswitch';
import { useApi } from '../composables/useApi';
import { formatDateTime } from '../composables/useFormatters';

const { get, patch, post } = useApi();

const loading = ref(true);
const settings = reactive({});
const accounts = ref([]);
const meta = reactive({ locked_fields: {}, enable_confirm_phrase: '', entries_exist: false });
const roles = ref([]);
const accountTypes = ref([]);

const bases = ['accrual', 'cash'];
const inventoryTreatments = ['expense', 'capitalize'];
// The two roles that make sense as payment destinations / contra-revenue.
const paymentRoles = ['UNDEPOSITED', 'OPERATING_BANK'];
const creditRoles = ['DISCOUNTS', 'REFUNDS'];

const enableDialogOpen = ref(false);
const enableConfirmText = ref('');
const accountDialogOpen = ref(false);
const editingAccount = ref(null);
const accountForm = reactive({ code: '', name: '', type: 'expense', active: true });
const cutoverMonth = ref(null);

const expenseAccountOptions = computed(() =>
  accounts.value
    .filter((a) => a.active && (a.type === 'expense'))
    .map((a) => ({ label: `${a.code} ${a.name}`, value: a.id })),
);

function isLocked(field) {
  return field in (meta.locked_fields || {});
}

// Small inline helpers keep the template readable.
const LockNote = defineComponent({
  props: { field: { type: String, required: true } },
  setup(props) {
    return () =>
      isLocked(props.field)
        ? h('span', { class: 'lock-note', 'data-testid': `gl-lock-${props.field}` },
            ` 🔒 ${meta.locked_fields[props.field]}`)
        : null;
  },
});

const CpaStamp = defineComponent({
  props: { k: { type: String, required: true } },
  setup(props) {
    return () => {
      const stamp = (settings.cpa_review || {})[props.k];
      return stamp
        ? h('span', { class: 'cpa-stamp', title: `CPA reviewed ${formatDateTime(stamp.reviewed_at)}` }, ' ✓CPA')
        : null;
    };
  },
});

function applyPayload(data) {
  Object.assign(settings, data.settings);
  accounts.value = data.accounts;
  meta.locked_fields = data.locked_fields;
  meta.enable_confirm_phrase = data.enable_confirm_phrase;
  meta.entries_exist = data.entries_exist;
  roles.value = data.roles;
  accountTypes.value = data.account_types;
  cutoverMonth.value = settings.cutover_month ? new Date(`${settings.cutover_month}T00:00:00`) : null;
}

async function load() {
  loading.value = true;
  try {
    let data = await get('/api/accounting/settings');
    if (data && data.seeded === false) {
      // First visit: materialize the starter CoA + defaults (write-gated;
      // a read-only viewer gets a 403 toast and an empty page, correctly).
      data = await post('/api/accounting/settings/initialize', {}, {
        successMessage: 'Chart of accounts initialized',
      });
    }
    applyPayload(data);
  } finally {
    loading.value = false;
  }
}

async function save(fields) {
  applyPayload(
    await patch('/api/accounting/settings', fields, { successMessage: 'Accounting settings saved' }),
  );
}

async function saveMap(mapField, key, value) {
  const next = { ...(settings[mapField] || {}), [key]: value };
  await save({ [mapField]: next });
}

async function saveEntityType() {
  const value = (settings.entity_type || '').trim();
  // An emptied field must actually CLEAR (a bare undefined would silently
  // no-op while toasting success).
  await save(value ? { entity_type: value } : { clear_entity_type: true });
}

async function saveCutover(value) {
  if (!value) {
    await save({ clear_cutover_month: true });
    return;
  }
  const first = new Date(value.getFullYear(), value.getMonth(), 1);
  const iso = `${first.getFullYear()}-${String(first.getMonth() + 1).padStart(2, '0')}-01`;
  await save({ cutover_month: iso });
}

async function enablePosting() {
  applyPayload(
    await post(
      '/api/accounting/settings/enable-posting',
      { confirm: enableConfirmText.value },
      { successMessage: 'Ledger posting enabled' },
    ),
  );
  enableDialogOpen.value = false;
  enableConfirmText.value = '';
}

async function disablePosting() {
  applyPayload(
    await post('/api/accounting/settings/disable-posting', {}, { successMessage: 'Ledger posting disabled' }),
  );
}

function openAdd() {
  editingAccount.value = null;
  Object.assign(accountForm, { code: '', name: '', type: 'expense', active: true });
  accountDialogOpen.value = true;
}

function openEdit(account) {
  editingAccount.value = account;
  Object.assign(accountForm, {
    code: account.code,
    name: account.name,
    type: account.type,
    active: account.active,
  });
  accountDialogOpen.value = true;
}

async function saveAccount() {
  if (editingAccount.value) {
    const body = { code: accountForm.code, name: accountForm.name };
    if (!editingAccount.value.is_system) body.active = accountForm.active;
    await patch(`/api/accounting/accounts/${editingAccount.value.id}`, body, {
      successMessage: 'Account updated',
    });
  } else {
    await post(
      '/api/accounting/accounts',
      { code: accountForm.code, name: accountForm.name, type: accountForm.type },
      { successMessage: 'Account added' },
    );
  }
  accountDialogOpen.value = false;
  await load();
}

onMounted(load);

// The vitest spec stubs DataTable (Column body slots don't render there), so
// the account-edit flow is driven through this handle.
defineExpose({ openEdit });
</script>

<style scoped>
.settings-stack {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.page-subtitle {
  color: var(--p-text-muted-color);
  margin: 0.25rem 0 0;
  font-size: 0.9rem;
}
.form-card {
  border: 1px solid var(--p-content-border-color);
  border-radius: 8px;
  padding: 1rem;
}
.form-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  margin-bottom: 0.5rem;
}
.form-card-header h3 { margin: 0; font-size: 1rem; }
.form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr));
  gap: 0.75rem 1rem;
}
.form-field { display: flex; flex-direction: column; gap: 0.25rem; }
.hint { color: var(--p-text-muted-color); font-size: 0.85rem; }
.switch-row { display: flex; align-items: center; gap: 0.75rem; }
.attest-row { display: flex; align-items: center; gap: 0.5rem; }
.map-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(18rem, 1fr)); gap: 0.5rem 1.25rem; }
.map-row { display: flex; align-items: center; gap: 0.6rem; }
.map-key { min-width: 8rem; font-weight: 600; text-transform: capitalize; }
.map-value { flex: 1; }
.lock-note { color: var(--p-orange-500, #f97316); font-size: 0.8rem; }
.cpa-stamp { color: var(--p-green-500, #22c55e); font-size: 0.8rem; }
.spinner-wrap { display: flex; justify-content: center; padding: 3rem 0; }
.w-full { width: 100%; }
</style>
