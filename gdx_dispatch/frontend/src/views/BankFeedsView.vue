<template>
  <section class="bank-feeds-view view-card">
    <Toolbar>
      <template #start>
        <h2 class="page-title">Bank Feeds</h2>
      </template>
      <template #end>
        <Button
          v-if="canManage"
          label="Sync Now"
          icon="pi pi-sync"
          :loading="actionLoading === 'sync'"
          data-testid="bank-feeds-sync-btn"
          @click="syncNow()"
        />
      </template>
    </Toolbar>

    <Tabs v-model:value="activeTab" class="bank-feeds-tabview">
      <TabList>
        <Tab value="banks" data-testid="bank-feeds-tab-banks">Banks</Tab>
        <Tab value="accounts" data-testid="bank-feeds-tab-accounts">Accounts</Tab>
        <Tab value="transactions" data-testid="bank-feeds-tab-transactions">Transactions</Tab>
        <Tab value="statements" data-testid="bank-feeds-tab-statements">Statements</Tab>
        <Tab value="reconcile" data-testid="bank-feeds-tab-reconcile">Reconcile</Tab>
        <Tab value="settings" data-testid="bank-feeds-tab-settings">Settings</Tab>
      </TabList>
      <TabPanels>
        <!-- ── Banks ─────────────────────────────────────────────── -->
        <TabPanel value="banks">
          <div v-if="loading" class="spinner-wrap"><ProgressSpinner /></div>
          <template v-else>
            <DataTable :value="institutions" striped-rows responsiveLayout="scroll" data-testid="bank-feeds-banks-table">
              <template #empty>
                <EmptyState
                  icon="pi pi-building-columns"
                  title="No banks connected"
                  :message="canManage
                    ? 'Add your bank to start syncing accounts, transactions, and statements.'
                    : 'No banks are configured yet. Ask an admin to add one.'"
                />
              </template>
              <Column field="label" header="Bank" :style="{ minWidth: '160px' }" />
              <Column header="Status" :style="{ minWidth: '150px' }">
                <template #body="{ data }">
                  <Tag :value="stateLabel(data)" :severity="stateSeverity(data)" />
                </template>
              </Column>
              <Column header="Accounts" :style="{ width: '110px' }">
                <template #body="{ data }">{{ data.account_count }}</template>
              </Column>
              <Column header="Last Sync" :style="{ minWidth: '170px' }">
                <template #body="{ data }">
                  {{ data.last_synced_at ? formatDateTime(data.last_synced_at) : '—' }}
                </template>
              </Column>
              <Column header="Statements" :style="{ width: '130px' }">
                <template #body="{ data }">
                  <span v-if="data.documents_available === true">Available</span>
                  <span v-else-if="data.documents_available === false" class="muted">Unavailable</span>
                  <span v-else class="muted">—</span>
                </template>
              </Column>
              <Column v-if="canManage" header="" :style="{ minWidth: '280px' }">
                <template #body="{ data }">
                  <div class="row-actions">
                    <Button
                      v-if="!data.connected"
                      label="Connect"
                      size="small"
                      icon="pi pi-link"
                      :disabled="!data.configured"
                      :loading="actionLoading === `connect-${data.id}`"
                      :data-testid="`bank-connect-${data.id}`"
                      @click="connectBank(data)"
                    />
                    <Button
                      v-else-if="data.auth_state !== 'healthy'"
                      label="Reconnect"
                      size="small"
                      icon="pi pi-refresh"
                      severity="warn"
                      :loading="actionLoading === `connect-${data.id}`"
                      @click="connectBank(data)"
                    />
                    <Button
                      v-if="data.connected"
                      label="Disconnect"
                      size="small"
                      icon="pi pi-unlink"
                      class="p-button-outlined"
                      @click="disconnectBank(data)"
                    />
                    <Button
                      label="Edit"
                      size="small"
                      class="p-button-text"
                      icon="pi pi-pencil"
                      @click="openEditDialog(data)"
                    />
                  </div>
                </template>
              </Column>
            </DataTable>
            <div v-if="canManage" class="add-bank-row">
              <Button
                label="Add bank"
                icon="pi pi-plus"
                class="p-button-outlined"
                data-testid="bank-feeds-add-bank-btn"
                @click="openAddDialog"
              />
            </div>
          </template>
        </TabPanel>

        <!-- ── Accounts ──────────────────────────────────────────── -->
        <TabPanel value="accounts">
          <DataTable :value="accounts" striped-rows responsiveLayout="scroll" data-testid="bank-feeds-accounts-table">
            <template #empty>
              <EmptyState
                icon="pi pi-wallet"
                title="No accounts yet"
                message="Accounts appear here after the first sync of a connected bank."
              />
            </template>
            <Column field="institution_label" header="Bank" :style="{ minWidth: '140px' }" />
            <Column header="Account" :style="{ minWidth: '180px' }">
              <template #body="{ data }">
                {{ data.name || 'Account' }}
                <span v-if="data.account_number_masked" class="muted"> {{ data.account_number_masked }}</span>
              </template>
            </Column>
            <Column field="account_type" header="Type" :style="{ width: '130px' }" />
            <Column header="Balance" :style="{ width: '140px' }">
              <template #body="{ data }">
                {{ data.balance != null ? formatMoney(data.balance) : '—' }}
              </template>
            </Column>
            <Column header="Last Sync" :style="{ minWidth: '170px' }">
              <template #body="{ data }">
                {{ data.last_synced_at ? formatDateTime(data.last_synced_at) : '—' }}
              </template>
            </Column>
            <Column header="Sync" :style="{ width: '100px' }">
              <template #body="{ data }">
                <InputSwitch
                  :modelValue="data.sync_enabled"
                  :disabled="!canManage"
                  @update:modelValue="(v) => toggleAccount(data, v)"
                />
              </template>
            </Column>
            <Column header="" :style="{ width: '110px' }">
              <template #body="{ data }">
                <Tag v-if="data.is_inactive" value="inactive" severity="secondary" />
              </template>
            </Column>
          </DataTable>
        </TabPanel>

        <!-- ── Transactions ──────────────────────────────────────── -->
        <TabPanel value="transactions">
          <div class="filters-row">
            <Select
              v-model="txnFilters.institution_id"
              :options="institutionOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="All banks"
              showClear
              class="filter-input"
              @change="reloadTransactions"
            />
            <Select
              v-model="txnFilters.account_id"
              :options="accountOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="All accounts"
              showClear
              class="filter-input"
              @change="reloadTransactions"
            />
            <DatePicker v-model="txnFilters.date_from" placeholder="From" dateFormat="yy-mm-dd" showIcon class="filter-input" @update:modelValue="reloadTransactions" />
            <DatePicker v-model="txnFilters.date_to" placeholder="To" dateFormat="yy-mm-dd" showIcon class="filter-input" @update:modelValue="reloadTransactions" />
            <InputText v-model="txnFilters.q" placeholder="Search payee / memo" class="filter-input" @keyup.enter="reloadTransactions" />
            <label class="pending-toggle">
              <Checkbox v-model="txnFilters.include_pending" binary @change="reloadTransactions" />
              <span>Include pending</span>
            </label>
          </div>
          <DataTable
            :value="transactions"
            lazy
            paginator
            :rows="txnPaging.limit"
            :totalRecords="txnPaging.total"
            :loading="txnLoading"
            striped-rows
            responsiveLayout="scroll"
            data-testid="bank-feeds-transactions-table"
            @page="onTxnPage"
          >
            <template #empty>
              <EmptyState
                icon="pi pi-list"
                title="No transactions"
                message="Synced bank transactions will appear here."
              />
            </template>
            <Column header="Date" :style="{ width: '120px' }">
              <template #body="{ data }">
                {{ data.posted_date || '—' }}
              </template>
            </Column>
            <Column field="payee" header="Payee" :style="{ minWidth: '180px' }" />
            <Column field="memo" header="Memo" :style="{ minWidth: '220px' }" />
            <Column field="institution_label" header="Bank" :style="{ width: '130px' }" />
            <Column header="Amount" :style="{ width: '130px' }">
              <template #body="{ data }">
                <span :class="amountClass(data.amount_cents)">{{ formatCents(data.amount_cents) }}</span>
              </template>
            </Column>
            <Column header="" :style="{ width: '110px' }">
              <template #body="{ data }">
                <Tag v-if="data.pending" value="pending" severity="warn" />
              </template>
            </Column>
          </DataTable>
        </TabPanel>

        <!-- ── Statements ────────────────────────────────────────── -->
        <TabPanel value="statements">
          <!-- Imported statements (manual PDF upload — evidence layer) -->
          <div class="imports-header">
            <h3 class="imports-title">Imported statements</h3>
            <FileUpload
              v-if="canImportStatements"
              mode="basic"
              custom-upload
              multiple
              accept="application/pdf"
              choose-label="Import statement PDFs"
              choose-icon="pi pi-upload"
              :auto="true"
              :disabled="importing"
              data-testid="bank-stmt-import-upload"
              @uploader="importStatements"
            />
          </div>
          <DataTable
            :value="statementImports"
            :loading="importsLoading"
            striped-rows
            responsiveLayout="scroll"
            data-testid="bank-stmt-imports-table"
          >
            <template #empty>
              <EmptyState
                icon="pi pi-file-import"
                title="No imported statements"
                :message="canImportStatements
                  ? 'Upload statement PDFs from your bank — every import is arithmetic-checked against the statement\'s own balances before any transaction is accepted.'
                  : 'Imported bank statements will appear here.'"
              />
            </template>
            <Column header="Period" :style="{ minWidth: '170px' }">
              <template #body="{ data }">{{ data.period_start }} → {{ data.period_end }}</template>
            </Column>
            <Column header="Account" :style="{ minWidth: '160px' }">
              <template #body="{ data }">{{ accountLabel(data.bank_account_id) }}</template>
            </Column>
            <Column header="Tie-out" :style="{ width: '110px' }">
              <template #body="{ data }">
                <Tag
                  :value="data.tie_out_status"
                  :severity="data.tie_out_status === 'passed' ? 'success' : 'danger'"
                  :data-testid="`bank-stmt-tieout-${data.id}`"
                />
              </template>
            </Column>
            <Column header="Lines" :style="{ width: '110px' }">
              <template #body="{ data }">
                {{ data.lines_added }}<span v-if="data.lines_deduped" class="muted"> (+{{ data.lines_deduped }} dup)</span>
              </template>
            </Column>
            <Column header="Ending balance" :style="{ width: '140px' }">
              <template #body="{ data }">{{ formatCents(data.ending_balance_cents) }}</template>
            </Column>
            <Column header="" :style="{ minWidth: '190px' }">
              <template #body="{ data }">
                <div class="row-actions">
                  <Button
                    label="Details"
                    size="small"
                    class="p-button-text"
                    icon="pi pi-search"
                    :data-testid="`bank-stmt-details-${data.id}`"
                    @click="openImportDetails(data)"
                  />
                  <Button
                    v-if="canImportStatements && !data.voided_at"
                    label="Void"
                    size="small"
                    severity="danger"
                    class="p-button-text"
                    icon="pi pi-times"
                    @click="askVoidImport(data)"
                  />
                  <Tag v-if="data.voided_at" value="voided" severity="secondary" />
                </div>
              </template>
            </Column>
          </DataTable>

          <h3 class="imports-title bank-docs-title">Bank-delivered statements</h3>
          <div class="filters-row">
            <Select
              v-model="docFilters.document_type"
              :options="docTypeOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="All types"
              showClear
              class="filter-input"
              @change="reloadDocuments"
            />
            <Select
              v-model="docFilters.institution_id"
              :options="institutionOptions"
              optionLabel="label"
              optionValue="value"
              placeholder="All banks"
              showClear
              class="filter-input"
              @change="reloadDocuments"
            />
          </div>
          <DataTable
            :value="documents"
            lazy
            paginator
            :rows="docPaging.limit"
            :totalRecords="docPaging.total"
            :loading="docLoading"
            striped-rows
            responsiveLayout="scroll"
            data-testid="bank-feeds-statements-table"
            @page="onDocPage"
          >
            <template #empty>
              <EmptyState
                icon="pi pi-file-pdf"
                :title="statementsUnavailable ? 'Statements unavailable' : 'No statements yet'"
                :message="statementsUnavailable
                  ? 'Your bank has not made documents available for this connection — check eStatement enrollment in your bank\'s own app.'
                  : 'Statement PDFs are archived here after each sync.'"
              />
            </template>
            <Column header="Date" :style="{ width: '120px' }">
              <template #body="{ data }">{{ data.document_date || '—' }}</template>
            </Column>
            <Column field="title" header="Title" :style="{ minWidth: '220px' }" />
            <Column header="Type" :style="{ width: '120px' }">
              <template #body="{ data }">
                <Tag :value="data.document_type" :severity="data.document_type === 'statement' ? 'info' : 'secondary'" />
              </template>
            </Column>
            <Column field="institution_label" header="Bank" :style="{ width: '140px' }" />
            <Column header="" :style="{ width: '130px' }">
              <template #body="{ data }">
                <Button
                  v-if="data.fetched"
                  label="Download"
                  size="small"
                  icon="pi pi-download"
                  class="p-button-text"
                  :data-testid="`bank-doc-download-${data.id}`"
                  @click="downloadDocument(data)"
                />
                <Tag v-else value="queued" severity="secondary" />
              </template>
            </Column>
          </DataTable>
        </TabPanel>

        <!-- ── Reconcile (PR 3): matcher + the four verification reports ── -->
        <TabPanel value="reconcile">
          <div class="filters-row">
            <Select
              v-model="reconAccountId"
              :options="statementAccounts"
              optionLabel="name"
              optionValue="id"
              placeholder="Account"
              class="filter-input"
              data-testid="recon-account-select"
              @change="loadReconciliation"
            />
            <DatePicker v-model="reconFrom" dateFormat="yy-mm-dd" placeholder="From" class="filter-input" @update:modelValue="loadReconciliation" />
            <DatePicker v-model="reconTo" dateFormat="yy-mm-dd" placeholder="To" class="filter-input" @update:modelValue="loadReconciliation" />
            <Button
              v-if="canReconcile"
              label="Suggest matches"
              icon="pi pi-sparkles"
              :loading="actionLoading === 'suggest'"
              :disabled="!reconAccountId"
              data-testid="recon-suggest-btn"
              @click="runSuggest"
            />
          </div>

          <EmptyState
            v-if="!reconAccountId"
            icon="pi pi-check-square"
            title="Pick an account"
            message="Reconciliation compares imported statement evidence against recorded payments and expenses."
          />
          <template v-else>
            <div class="recon-summary">
              <div class="recon-card" data-testid="recon-card-deposits">
                <span class="recon-count">{{ reports.unmatched_deposits?.length ?? '—' }}</span>
                <span>unmatched deposits</span>
                <span class="muted">{{ formatCents(reports.unmatched_deposits_total_cents) }}</span>
              </div>
              <div class="recon-card" data-testid="recon-card-payments">
                <span class="recon-count">{{ reports.unmatched_payments?.length ?? '—' }}</span>
                <span>payments not on statement</span>
                <span class="muted">{{ formatCents(reports.unmatched_payments_total_cents) }}</span>
              </div>
              <div class="recon-card">
                <span class="recon-count">{{ reports.date_drift?.length ?? '—' }}</span>
                <span>date drift</span>
              </div>
              <div class="recon-card">
                <span class="recon-count">{{ reports.unmatched_debits?.length ?? '—' }}</span>
                <span>unmatched debits</span>
                <span class="muted">{{ formatCents(reports.unmatched_debits_total_cents) }}</span>
              </div>
            </div>

            <template v-if="(reports.broken_matches || []).length">
              <h3 class="imports-title broken-title">
                ⚠ Broken matches <span class="muted">({{ reports.broken_matches.length }})</span>
              </h3>
              <p class="muted recon-note">
                These confirmed matches reference payments or records that were voided or
                deleted afterwards — the statement line still shows as settled by dead money.
                Unconfirm to put the line back in the worklist.
              </p>
              <DataTable :value="reports.broken_matches" striped-rows size="small" data-testid="recon-broken-table">
                <Column header="Statement side" :style="{ minWidth: '240px' }">
                  <template #body="{ data }">
                    <div v-for="l in data.lines" :key="l.id">
                      {{ l.txn_date }} · {{ formatCents(l.amount_cents) }} · {{ l.description }}
                    </div>
                  </template>
                </Column>
                <Column header="What died" :style="{ minWidth: '200px' }">
                  <template #body="{ data }">
                    <div v-for="d in data.dead_externals" :key="d.source_id">
                      <Tag :value="d.reason" severity="danger" />
                    </div>
                  </template>
                </Column>
                <Column header="" :style="{ width: '140px' }">
                  <template #body="{ data }">
                    <Button
                      v-if="canReconcile"
                      label="Unconfirm"
                      size="small"
                      severity="warn"
                      :data-testid="`recon-unconfirm-${data.match_id}`"
                      @click="actOnMatch(data.match_id, 'unconfirm')"
                    />
                  </template>
                </Column>
              </DataTable>
            </template>

            <h3 class="imports-title">Suggestions <span class="muted">({{ suggestions.length }})</span></h3>
            <DataTable :value="suggestions" striped-rows size="small" data-testid="recon-suggestions-table">
              <template #empty><span class="muted">No open suggestions — run "Suggest matches".</span></template>
              <Column header="Rule" :style="{ width: '90px' }">
                <template #body="{ data }">
                  <Tag :value="data.classification || data.rule" severity="info" />
                </template>
              </Column>
              <Column header="Statement side" :style="{ minWidth: '240px' }">
                <template #body="{ data }">
                  <div v-for="l in data.lines" :key="l.id">
                    {{ l.txn_date }} · {{ formatCents(l.amount_cents) }} · {{ l.description }}
                  </div>
                </template>
              </Column>
              <Column header="Books side" :style="{ minWidth: '200px' }">
                <template #body="{ data }">
                  <span v-if="!data.externals.length" class="muted">classification only</span>
                  <div v-else>{{ data.externals.length }} record(s) · {{ data.note || '' }}</div>
                </template>
              </Column>
              <Column header="" :style="{ minWidth: '190px' }">
                <template #body="{ data }">
                  <div v-if="canReconcile" class="row-actions">
                    <Button label="Confirm" size="small" icon="pi pi-check"
                            :data-testid="`recon-confirm-${data.id}`"
                            @click="actOnMatch(data.id, 'confirm')" />
                    <Button label="Reject" size="small" severity="danger" class="p-button-text"
                            @click="actOnMatch(data.id, 'reject')" />
                  </div>
                </template>
              </Column>
            </DataTable>

            <h3 class="imports-title">Unmatched deposits</h3>
            <DataTable :value="reports.unmatched_deposits || []" striped-rows size="small" data-testid="recon-deposits-table">
              <template #empty><span class="muted">Every deposit in range is confirmed-matched. 🎉</span></template>
              <Column field="txn_date" header="Date" :style="{ width: '110px' }" />
              <Column header="Amount" :style="{ width: '120px' }">
                <template #body="{ data }">{{ formatCents(data.amount_cents) }}</template>
              </Column>
              <Column field="description" header="Description" :style="{ minWidth: '220px' }" />
              <Column header="" :style="{ width: '210px' }">
                <template #body="{ data }">
                  <Tag v-if="data.has_suggestion" value="suggestion above" severity="info" />
                  <Button v-else-if="canReconcile" label="Match…" size="small" class="p-button-text"
                          icon="pi pi-link" @click="openManualMatch(data)" />
                </template>
              </Column>
            </DataTable>

            <h3 class="imports-title">Payments never seen by the bank</h3>
            <DataTable :value="reports.unmatched_payments || []" striped-rows size="small" data-testid="recon-payments-table">
              <template #empty><span class="muted">Every recorded payment in range is confirmed against the statement.</span></template>
              <Column field="payment_date" header="Recorded" :style="{ width: '110px' }" />
              <Column header="Amount" :style="{ width: '120px' }">
                <template #body="{ data }">{{ formatCents(data.amount_cents) }}</template>
              </Column>
              <Column field="method" header="Method" :style="{ width: '100px' }" />
              <Column field="invoice_number" header="Invoice" :style="{ width: '130px' }" />
              <Column field="customer_name" header="Customer" :style="{ minWidth: '160px' }" />
              <Column field="reference" header="Reference" :style="{ minWidth: '120px' }" />
            </DataTable>
            <p class="muted recon-note">
              Card payments settle through the processor as batched payouts — a card
              payment listed here may be inside a payout, not missing.
            </p>

            <h3 class="imports-title">Date drift <span class="muted">(recorded vs bank)</span></h3>
            <DataTable :value="reports.date_drift || []" striped-rows size="small">
              <template #empty><span class="muted">No confirmed match has a date discrepancy.</span></template>
              <Column field="payment_date" header="Recorded" :style="{ width: '110px' }" />
              <Column field="bank_date" header="Bank" :style="{ width: '110px' }" />
              <Column header="Drift" :style="{ width: '90px' }">
                <template #body="{ data }">{{ data.drift_days > 0 ? '+' : '' }}{{ data.drift_days }}d</template>
              </Column>
              <Column header="Amount" :style="{ minWidth: '120px' }">
                <template #body="{ data }">{{ formatCents(data.amount_cents) }}</template>
              </Column>
            </DataTable>

            <h3 class="imports-title">Unmatched debits</h3>
            <DataTable :value="reports.unmatched_debits || []" striped-rows size="small" data-testid="recon-debits-table">
              <template #empty><span class="muted">Every debit in range is confirmed-matched.</span></template>
              <Column field="txn_date" header="Date" :style="{ width: '110px' }" />
              <Column header="Amount" :style="{ width: '120px' }">
                <template #body="{ data }">{{ formatCents(-data.amount_cents) }}</template>
              </Column>
              <Column field="description" header="Description" :style="{ minWidth: '220px' }" />
              <Column field="check_number" header="Check #" :style="{ width: '90px' }" />
              <Column header="" :style="{ width: '210px' }">
                <template #body="{ data }">
                  <Tag v-if="data.has_suggestion" value="suggestion above" severity="info" />
                  <Button v-else-if="canReconcile" label="Match…" size="small" class="p-button-text"
                          icon="pi pi-link" @click="openManualMatch(data)" />
                </template>
              </Column>
            </DataTable>
          </template>
        </TabPanel>

        <!-- ── Settings ──────────────────────────────────────────── -->
        <TabPanel value="settings">
          <div class="settings-panel">
            <h3>Sync schedule</h3>
            <div class="settings-row">
              <label>Frequency</label>
              <Select
                v-model="scheduleForm.frequency"
                :options="frequencyOptions"
                optionLabel="label"
                optionValue="value"
                :disabled="!canManage"
                class="filter-input"
                data-testid="bank-feeds-frequency-select"
              />
            </div>
            <div class="settings-row">
              <label>Backfill history (days)</label>
              <InputNumber
                v-model="scheduleForm.backfill_days"
                :min="1"
                :max="3650"
                :disabled="!canManage"
                data-testid="bank-feeds-backfill-days"
              />
            </div>
            <p class="muted">
              Changing the backfill window only affects future full re-syncs — history already synced is kept.
            </p>
            <div class="settings-row" v-if="schedule.last_run_at">
              <label>Last run</label>
              <span>
                {{ formatDateTime(schedule.last_run_at) }}
                <Tag
                  v-if="schedule.last_run_status"
                  :value="schedule.last_run_status"
                  :severity="schedule.last_run_status === 'ok' ? 'success' : (schedule.last_run_status === 'partial' ? 'warn' : 'danger')"
                />
              </span>
            </div>
            <Button
              v-if="canManage"
              label="Save schedule"
              icon="pi pi-save"
              :loading="actionLoading === 'schedule'"
              data-testid="bank-feeds-save-schedule"
              @click="saveSchedule"
            />
          </div>
        </TabPanel>
      </TabPanels>
    </Tabs>

    <!-- ── Add / Edit bank dialog ─────────────────────────────────── -->
    <Dialog
      v-model:visible="showBankDialog"
      :header="editingInstitutionId ? 'Edit bank' : 'Add bank'"
      modal
      :style="{ width: '460px' }"
    >
      <div class="dialog-form">
        <label>Banno hostname
          <InputText
            v-model="bankForm.fi_host"
            placeholder="digital.yourbank.com"
            data-testid="bank-form-host"
          />
        </label>
        <label>Display name
          <InputText v-model="bankForm.display_label" placeholder="My Bank" data-testid="bank-form-label" />
        </label>
        <label>Client ID
          <InputText v-model="bankForm.client_id" autocomplete="off" data-testid="bank-form-client-id" />
        </label>
        <label>Client secret
          <Password
            v-model="bankForm.client_secret"
            :feedback="false"
            toggleMask
            autocomplete="new-password"
            :placeholder="editingSecretSet ? '•••••• (already set — leave blank to keep)' : ''"
            data-testid="bank-form-client-secret"
          />
        </label>
        <p class="muted">
          Each bank provisions these credentials as an “external application” in its Banno back office.
        </p>
      </div>
      <template #footer>
        <Button label="Cancel" class="p-button-text" @click="showBankDialog = false" />
        <Button
          :label="editingInstitutionId ? 'Save' : 'Add bank'"
          :loading="actionLoading === 'save-bank'"
          data-testid="bank-form-save"
          @click="saveBank"
        />
      </template>
    </Dialog>

    <!-- ── Imported statement details ─────────────────────────────── -->
    <Dialog
      v-model:visible="showImportDetails"
      :header="importDetails ? `Statement ${importDetails.period_start} → ${importDetails.period_end}` : 'Statement'"
      :style="{ width: 'min(860px, 95vw)' }"
      modal
      data-testid="bank-stmt-details-dialog"
      @hide="closeImportDetails"
    >
      <template v-if="importDetails">
        <div class="stmt-summary-grid">
          <div><span class="muted">Beginning</span> {{ formatCents(importDetails.beginning_balance_cents) }}</div>
          <div><span class="muted">Deposits</span> {{ formatCents(importDetails.deposits_total_cents) }} ({{ importDetails.deposits_count ?? '—' }})</div>
          <div><span class="muted">Debits</span> {{ formatCents(importDetails.debits_total_cents) }} ({{ importDetails.debits_count ?? '—' }})</div>
          <div><span class="muted">Ending</span> {{ formatCents(importDetails.ending_balance_cents) }}</div>
        </div>

        <h4>Tie-out checks</h4>
        <DataTable :value="importDetails.tie_out_report?.checks || []" size="small" striped-rows>
          <Column field="name" header="Check" :style="{ minWidth: '170px' }" />
          <Column header="Result" :style="{ width: '90px' }">
            <template #body="{ data }">
              <Tag :value="data.ok ? 'ok' : 'FAIL'" :severity="data.ok ? 'success' : 'danger'" />
            </template>
          </Column>
          <Column header="Detail" :style="{ minWidth: '260px' }">
            <template #body="{ data }">
              <span v-if="data.ok" class="muted">—</span>
              <span v-else>expected {{ data.expected }}, got {{ formatCheckActual(data.actual) }}</span>
            </template>
          </Column>
        </DataTable>

        <div v-if="(importDetails.tie_out_report?.continuity_warnings || []).length" class="continuity-warnings">
          <Tag severity="warn" value="continuity" />
          <span>{{ importDetails.tie_out_report.continuity_warnings.length }} continuity warning(s) against adjacent statements — see report.</span>
        </div>

        <h4>Check &amp; deposit images <span v-if="canViewScans" class="muted">({{ importImages.length }})</span></h4>
        <p v-if="!canViewScans" class="muted">Check images are restricted to accounting staff.</p>
        <p v-else-if="!importImages.length" class="muted">This statement's PDF has no images page.</p>
        <div v-else class="stmt-image-grid">
          <figure v-for="img in importImages" :key="img.id" class="stmt-image-card">
            <img v-if="img.url" :src="img.url" :alt="imageCaption(img)" loading="lazy" />
            <div v-else class="stmt-image-loading"><ProgressSpinner style="width:24px;height:24px" /></div>
            <figcaption>
              <Tag
                :value="img.caption_check_no === '0' ? 'deposit' : `check ${img.caption_check_no || '?'}`"
                :severity="img.caption_check_no === '0' ? 'info' : 'secondary'"
              />
              <span v-if="img.caption_amount_cents != null">{{ formatCents(img.caption_amount_cents) }}</span>
              <span v-if="img.caption_date" class="muted">{{ img.caption_date }}</span>
              <Tag v-if="!img.line_id" value="unpaired" severity="warn" />
            </figcaption>
          </figure>
        </div>
      </template>
    </Dialog>

    <!-- ── Manual match dialog ────────────────────────────────────── -->
    <Dialog
      v-model:visible="showManualMatch"
      :header="manualLine ? `Match ${manualLine.txn_date} · ${formatCents(Math.abs(manualLine.amount_cents))}` : 'Match'"
      :style="{ width: 'min(680px, 95vw)' }"
      modal
      data-testid="recon-manual-dialog"
    >
      <template v-if="manualLine">
        <p class="muted">{{ manualLine.description }}</p>
        <h4 v-if="manualCandidates.payments.length">Payments</h4>
        <div v-for="p in manualCandidates.payments" :key="p.id" class="manual-candidate">
          <Checkbox v-model="manualSelected" :inputId="p.id" :value="`payments:${p.id}`" />
          <label :for="p.id">
            {{ p.payment_date }} · {{ formatCents(p.amount_cents) }} · {{ p.method }}
            · {{ p.invoice_number || '—' }} · {{ p.customer_name || '—' }}
            <span v-if="p.reference" class="muted">ref {{ p.reference }}</span>
          </label>
        </div>
        <h4 v-if="manualCandidates.expenses.length">Expenses</h4>
        <div v-for="e in manualCandidates.expenses" :key="e.id" class="manual-candidate">
          <Checkbox v-model="manualSelected" :inputId="e.id" :value="`expenses:${e.id}`" />
          <label :for="e.id">{{ e.date }} · {{ formatCents(e.amount_cents) }} · {{ e.vendor }} · {{ e.category }}</label>
        </div>
        <h4 v-if="manualCandidates.vendor_invoices.length">Vendor bills</h4>
        <div v-for="v in manualCandidates.vendor_invoices" :key="v.id" class="manual-candidate">
          <Checkbox v-model="manualSelected" :inputId="v.id" :value="`vendor_invoices:${v.id}`" />
          <label :for="v.id">{{ v.invoice_date }} · {{ formatCents(v.total_cents) }} · {{ v.vendor_key }} #{{ v.invoice_number }}</label>
        </div>

        <div class="settings-row manual-classify-row">
          <label>Or classify as</label>
          <Select
            v-model="manualClassification"
            :options="classificationOptions"
            optionLabel="label"
            optionValue="value"
            showClear
            placeholder="—"
            class="filter-input"
          />
        </div>
        <InputText v-model="manualNote" placeholder="Note (optional)" class="manual-note" />
      </template>
      <template #footer>
        <Button label="Cancel" class="p-button-text" @click="showManualMatch = false" />
        <Button
          label="Create confirmed match"
          :disabled="!manualSelected.length && !manualClassification"
          :loading="actionLoading === 'manual'"
          data-testid="recon-manual-save"
          @click="saveManualMatch"
        />
      </template>
    </Dialog>

    <!-- ── Void confirm (local dialog — deliberate, not useDestructiveConfirm) ── -->
    <Dialog
      v-model:visible="showVoidConfirm"
      header="Void this import?"
      :style="{ width: 'min(460px, 95vw)' }"
      modal
      data-testid="bank-stmt-void-dialog"
    >
      <p v-if="voidTarget">
        Voiding removes this statement's transactions from the evidence table
        (lines also vouched for by an overlapping import are kept). The same
        PDF can be imported again afterwards.
      </p>
      <template #footer>
        <Button label="Cancel" class="p-button-text" @click="showVoidConfirm = false" />
        <Button
          label="Void import"
          severity="danger"
          :loading="actionLoading === 'void'"
          data-testid="bank-stmt-void-confirm"
          @click="voidImport"
        />
      </template>
    </Dialog>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import { useToast } from 'primevue/usetoast';
import { useAuthStore } from '../stores/auth';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatDateTime } from '../composables/useFormatters';
import EmptyState from '../components/EmptyState.vue';
import Button from 'primevue/button';
import Checkbox from 'primevue/checkbox';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import DatePicker from 'primevue/datepicker';
import Dialog from 'primevue/dialog';
import FileUpload from 'primevue/fileupload';
import InputNumber from 'primevue/inputnumber';
import InputSwitch from 'primevue/inputswitch';
import InputText from 'primevue/inputtext';
import Password from 'primevue/password';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Tab from 'primevue/tab';
import TabList from 'primevue/tablist';
import TabPanel from 'primevue/tabpanel';
import TabPanels from 'primevue/tabpanels';
import Tabs from 'primevue/tabs';
import Tag from 'primevue/tag';
import Toolbar from 'primevue/toolbar';

const api = useApiWithToast();
const auth = useAuthStore();
// useApi keeps its toast internal — `api.toast` does not exist (a silent
// no-op the PR-2 audit caught live). Use PrimeVue's useToast directly.
const toast = useToast();

const canManage = computed(() => auth.hasPermission('bank_feeds.manage'));
// Statement import/void is the accounting role's job (Doug 2026-08-01) —
// its own key, because bank_feeds.manage also carries Banno credentials.
const canImportStatements = computed(() => auth.hasPermission('bank_feeds.statements'));
// Check/deposit-ticket scans show full account numbers + signatures:
// higher office only (accounting.write), per Doug.
const canViewScans = computed(() => auth.hasPermission('accounting.write'));

const activeTab = ref('banks');
const loading = ref(true);
const actionLoading = ref('');

const institutions = ref([]);
const schedule = ref({});
const accounts = ref([]);

// ── status + accounts ──────────────────────────────────────────────

const loadStatus = async () => {
  loading.value = true;
  try {
    const [status, accts] = await Promise.all([
      api.get('/api/bank-feeds/status'),
      api.get('/api/bank-feeds/accounts').catch(() => ({ accounts: [] })),
    ]);
    institutions.value = status.institutions || [];
    schedule.value = status.schedule || {};
    scheduleForm.frequency = schedule.value.frequency || 'manual';
    scheduleForm.backfill_days = schedule.value.backfill_days || 365;
    accounts.value = accts.accounts || [];
  } finally {
    loading.value = false;
  }
};

const stateLabel = (inst) => {
  if (!inst.configured) return 'not configured';
  if (!inst.connected) return 'not connected';
  if (inst.auth_state === 'healthy') return 'connected';
  return inst.auth_state.replace('_', ' ');
};

const stateSeverity = (inst) => {
  if (!inst.configured || !inst.connected) return 'secondary';
  if (inst.auth_state === 'healthy') return 'success';
  return 'warn';
};

const statementsUnavailable = computed(
  () => institutions.value.length > 0
    && institutions.value.every((i) => i.documents_available === false)
);

// ── OAuth popup connect flow (QB pattern + close-poll fallback) ────

let popupWatch = null;

const connectBank = async (inst) => {
  actionLoading.value = `connect-${inst.id}`;
  try {
    const response = await api.post('/api/bank-feeds/connect', { institution_id: inst.id });
    if (response?.redirect_url) {
      const popup = window.open(response.redirect_url, '_blank', 'width=600,height=700');
      if (!popup || popup.closed || typeof popup.closed === 'undefined') {
        toast.add({
          severity: 'warn',
          summary: 'Popup blocked',
          detail: 'Redirecting in this window instead...',
          life: 3000,
        });
        window.location.href = response.redirect_url;
        return;
      }
      // Fallback for postMessage origin mismatches: when the popup closes
      // without a message, re-poll status once so the UI can't strand.
      if (popupWatch) clearInterval(popupWatch);
      popupWatch = setInterval(() => {
        if (popup.closed) {
          clearInterval(popupWatch);
          popupWatch = null;
          loadStatus();
        }
      }, 1000);
    }
  } finally {
    actionLoading.value = '';
  }
};

function onOAuthMessage(event) {
  if (event.origin !== window.location.origin) return;
  const data = event.data;
  if (!data || data.type !== 'bank_feeds_oauth_result') return;
  actionLoading.value = '';
  if (data.status === 'connected') {
    loadStatus();
  }
}

const disconnectBank = async (inst) => {
  await api.post(
    '/api/bank-feeds/disconnect',
    { institution_id: inst.id },
    { successMessage: 'Bank disconnected — synced data is kept.' },
  );
  await loadStatus();
};

const syncNow = async () => {
  actionLoading.value = 'sync';
  try {
    await api.post('/api/bank-feeds/sync', {}, { successMessage: 'Sync queued' });
  } finally {
    actionLoading.value = '';
  }
};

// ── add / edit bank ────────────────────────────────────────────────

const showBankDialog = ref(false);
const editingInstitutionId = ref(null);
const editingSecretSet = ref(false);
const bankForm = reactive({ fi_host: '', display_label: '', client_id: '', client_secret: '' });

const openAddDialog = () => {
  editingInstitutionId.value = null;
  editingSecretSet.value = false;
  Object.assign(bankForm, { fi_host: '', display_label: '', client_id: '', client_secret: '' });
  showBankDialog.value = true;
};

const openEditDialog = async (inst) => {
  editingInstitutionId.value = inst.id;
  const detail = await api.get('/api/bank-feeds/institutions');
  const row = (detail.institutions || []).find((i) => i.id === inst.id) || {};
  editingSecretSet.value = !!row.secret_set;
  Object.assign(bankForm, {
    fi_host: row.fi_host || '',
    display_label: row.display_label || '',
    client_id: row.client_id || '',
    client_secret: '',
  });
  showBankDialog.value = true;
};

const saveBank = async () => {
  actionLoading.value = 'save-bank';
  try {
    const payload = {
      fi_host: bankForm.fi_host,
      display_label: bankForm.display_label,
      client_id: bankForm.client_id,
    };
    if (bankForm.client_secret) payload.client_secret = bankForm.client_secret;
    if (editingInstitutionId.value) {
      await api.patch(`/api/bank-feeds/institutions/${editingInstitutionId.value}`, payload, {
        successMessage: 'Bank updated',
      });
    } else {
      await api.post('/api/bank-feeds/institutions', { ...payload, client_secret: bankForm.client_secret || '' }, {
        successMessage: 'Bank added',
      });
    }
    showBankDialog.value = false;
    await loadStatus();
  } finally {
    actionLoading.value = '';
  }
};

// ── accounts toggle ────────────────────────────────────────────────

const toggleAccount = async (account, value) => {
  await api.patch(`/api/bank-feeds/accounts/${account.id}`, { sync_enabled: value });
  account.sync_enabled = value;
};

// ── transactions ───────────────────────────────────────────────────

const transactions = ref([]);
const txnLoading = ref(false);
const txnFilters = reactive({
  institution_id: null,
  account_id: null,
  date_from: null,
  date_to: null,
  q: '',
  include_pending: true,
});
const txnPaging = reactive({ limit: 50, offset: 0, total: 0 });

const toDateParam = (d) => {
  if (!d) return null;
  if (typeof d === 'string') return d.slice(0, 10);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

const loadTransactions = async () => {
  txnLoading.value = true;
  try {
    const params = new URLSearchParams();
    if (txnFilters.institution_id) params.set('institution_id', txnFilters.institution_id);
    if (txnFilters.account_id) params.set('account_id', txnFilters.account_id);
    const from = toDateParam(txnFilters.date_from);
    const to = toDateParam(txnFilters.date_to);
    if (from) params.set('date_from', from);
    if (to) params.set('date_to', to);
    if (txnFilters.q) params.set('q', txnFilters.q);
    params.set('include_pending', String(txnFilters.include_pending));
    params.set('limit', String(txnPaging.limit));
    params.set('offset', String(txnPaging.offset));
    const data = await api.get(`/api/bank-feeds/transactions?${params.toString()}`);
    transactions.value = data.items || [];
    txnPaging.total = data.total || 0;
  } finally {
    txnLoading.value = false;
  }
};

const reloadTransactions = () => {
  txnPaging.offset = 0;
  loadTransactions();
};

const onTxnPage = (event) => {
  txnPaging.offset = event.first;
  loadTransactions();
};

const institutionOptions = computed(() =>
  institutions.value.map((i) => ({ label: i.label, value: i.id }))
);
const accountOptions = computed(() =>
  accounts.value.map((a) => ({
    label: `${a.name || 'Account'} ${a.account_number_masked || ''}`.trim(),
    value: a.id,
  }))
);

const formatCents = (cents) => {
  if (cents == null) return '—';
  const value = cents / 100;
  return value.toLocaleString('en-US', { style: 'currency', currency: 'USD' });
};
const formatMoney = (v) => Number(v).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
const amountClass = (cents) => (cents != null && cents < 0 ? 'amount-out' : 'amount-in');

// ── documents ──────────────────────────────────────────────────────

const documents = ref([]);
const docLoading = ref(false);
const docFilters = reactive({ document_type: 'statement', institution_id: null });
const docPaging = reactive({ limit: 50, offset: 0, total: 0 });
const docTypeOptions = [
  { label: 'Statements', value: 'statement' },
  { label: 'Notices', value: 'notice' },
  { label: 'Tax documents', value: 'tax' },
];

const loadDocuments = async () => {
  docLoading.value = true;
  try {
    const params = new URLSearchParams();
    if (docFilters.document_type) params.set('document_type', docFilters.document_type);
    if (docFilters.institution_id) params.set('institution_id', docFilters.institution_id);
    params.set('limit', String(docPaging.limit));
    params.set('offset', String(docPaging.offset));
    const data = await api.get(`/api/bank-feeds/documents?${params.toString()}`);
    documents.value = data.items || [];
    docPaging.total = data.total || 0;
  } finally {
    docLoading.value = false;
  }
};

const reloadDocuments = () => {
  docPaging.offset = 0;
  loadDocuments();
};

const onDocPage = (event) => {
  docPaging.offset = event.first;
  loadDocuments();
};

function deriveTenantId() {
  if (auth.tenantSlug) return auth.tenantSlug;
  const parts = window.location.hostname.split('.');
  const slug = parts.length >= 3 ? parts[0] : null;
  return slug && slug !== 'gdx' ? slug : '';
}

const downloadDocument = async (doc) => {
  const headers = new Headers();
  const tenantId = deriveTenantId();
  if (tenantId) headers.set('x-tenant-id', tenantId);
  if (auth.accessToken) headers.set('Authorization', `Bearer ${auth.accessToken}`);
  const response = await fetch(`/api/bank-feeds/documents/${doc.id}/download`, {
    headers,
    credentials: 'include',
  });
  if (!response.ok) {
    api.toast?.add?.({ severity: 'error', summary: 'Download failed', life: 4000 });
    return;
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = doc.title || `statement-${doc.document_date || 'document'}.pdf`;
  link.click();
  URL.revokeObjectURL(url);
};

// ── imported statements (evidence layer) ───────────────────────────

const statementImports = ref([]);
const statementAccounts = ref([]);
const importsLoading = ref(false);
const importing = ref(false);

const loadStatementImports = async () => {
  importsLoading.value = true;
  try {
    const [imports, accts] = await Promise.all([
      api.get('/api/bank-feeds/statements/imports?include_voided=true'),
      api.get('/api/bank-feeds/statements/accounts'),
    ]);
    statementImports.value = imports.items || [];
    statementAccounts.value = accts.items || [];
  } catch {
    // Module dark or older API — the section just shows its empty state.
    statementImports.value = [];
    statementAccounts.value = [];
  } finally {
    importsLoading.value = false;
  }
};

const accountLabel = (accountId) => {
  const account = statementAccounts.value.find((a) => a.id === accountId);
  return account ? `${account.name} ····${account.last4}` : '—';
};

const formatCheckActual = (actual) => {
  if (actual == null) return '—';
  return typeof actual === 'object' ? JSON.stringify(actual) : String(actual);
};

const importStatements = async (event) => {
  const files = event.files || [];
  if (!files.length) return;
  importing.value = true;
  try {
    const fd = new FormData();
    files.forEach((f) => fd.append('files', f));
    const result = await api.post('/api/bank-feeds/statements/import', fd);
    const outcomes = (result?.results || []).map((r) => `${r.filename}: ${r.status}`);
    const failed = (result?.results || []).some((r) => r.status !== 'imported');
    // duplicate_file / parse_error return HTTP 200 with NO table row — this
    // toast is their only user-visible surface, so it must actually fire.
    toast.add({
      severity: failed ? 'warn' : 'success',
      summary: failed ? 'Import finished with issues' : 'Statements imported',
      detail: outcomes.join(' · '),
      life: failed ? 8000 : 4000,
    });
    await loadStatementImports();
  } finally {
    importing.value = false;
  }
};

// Details dialog (+ authenticated image blobs — the scans carry full
// account numbers, so they are never plain <img src> URLs).

const showImportDetails = ref(false);
const importDetails = ref(null);
const importImages = ref([]);

const authedFetchBlobUrl = async (path) => {
  const headers = new Headers();
  const tenantId = deriveTenantId();
  if (tenantId) headers.set('x-tenant-id', tenantId);
  if (auth.accessToken) headers.set('Authorization', `Bearer ${auth.accessToken}`);
  const response = await fetch(path, { headers, credentials: 'include' });
  if (!response.ok) return null;
  return URL.createObjectURL(await response.blob());
};

const openImportDetails = async (row) => {
  importDetails.value = null;
  importImages.value = [];
  showImportDetails.value = true;
  const [detail, gallery] = await Promise.all([
    api.get(`/api/bank-feeds/statements/imports/${row.id}`),
    canViewScans.value
      ? api.get(`/api/bank-feeds/statements/imports/${row.id}/images`)
      : Promise.resolve({ items: [] }),
  ]);
  importDetails.value = detail;
  const images = (gallery.items || []).map((img) => ({ ...img, url: null }));
  importImages.value = images;
  for (const img of images) {
    const url = await authedFetchBlobUrl(`/api/bank-feeds/statements/images/${img.id}/file`);
    // Dialog closed mid-fetch: the array was detached by closeImportDetails —
    // revoke instead of leaking the blob URL, and stop fetching.
    if (importImages.value !== images) {
      if (url) URL.revokeObjectURL(url);
      break;
    }
    img.url = url;
  }
};

const closeImportDetails = () => {
  importImages.value.forEach((img) => { if (img.url) URL.revokeObjectURL(img.url); });
  importImages.value = [];
  importDetails.value = null;
};

const imageCaption = (img) => {
  const kind = img.caption_check_no === '0' ? 'Deposit ticket' : `Check ${img.caption_check_no || ''}`;
  return `${kind} ${img.caption_date || ''}`.trim();
};

// ── reconciliation (PR 3) ──────────────────────────────────────────

const canReconcile = computed(() => auth.hasPermission('accounting.write'));
const reconAccountId = ref(null);
const reconFrom = ref(null);
const reconTo = ref(null);
const reports = ref({});
const suggestions = ref([]);

const isoDate = (d) => {
  if (!d) return null;
  const dt = d instanceof Date ? d : new Date(d);
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`;
};

const defaultReconRange = () => {
  const mine = statementImports.value.filter(
    (i) => i.bank_account_id === reconAccountId.value && !i.voided_at);
  if (!mine.length) return;
  reconFrom.value = new Date(`${mine.reduce((a, i) => (i.period_start < a ? i.period_start : a), mine[0].period_start)}T12:00:00`);
  reconTo.value = new Date(`${mine.reduce((a, i) => (i.period_end > a ? i.period_end : a), mine[0].period_end)}T12:00:00`);
};

const loadReconciliation = async () => {
  if (!reconAccountId.value) return;
  if (!reconFrom.value || !reconTo.value) defaultReconRange();
  if (!reconFrom.value || !reconTo.value) return;
  const range = `date_from=${isoDate(reconFrom.value)}&date_to=${isoDate(reconTo.value)}`;
  const [reportData, matchData] = await Promise.all([
    api.get(`/api/bank-feeds/statements/reconciliation?account_id=${reconAccountId.value}&${range}`),
    // Same range as the report cards beside it — a suggestions list that
    // ignores the date pickers reads as a different period's data.
    api.get(`/api/bank-feeds/statements/matches?account_id=${reconAccountId.value}&status=suggested&${range}`),
  ]);
  reports.value = reportData;
  suggestions.value = matchData.items || [];
};

const runSuggest = async () => {
  actionLoading.value = 'suggest';
  try {
    const stats = await api.post('/api/bank-feeds/statements/reconcile/suggest', {
      account_id: reconAccountId.value,
      date_from: isoDate(reconFrom.value),
      date_to: isoDate(reconTo.value),
    });
    const s = stats?.stats || {};
    toast.add({
      severity: 'success',
      summary: 'Matching complete',
      detail: `${s.classified ?? 0} classified · ${(s.r2_payments ?? 0) + (s.r2_expenses ?? 0) + (s.r2_vendor_invoices ?? 0)} exact · ${s.r3_sweeps ?? 0} deposit sweeps · ${s.unmatched ?? 0} unmatched`,
      life: 6000,
    });
    await loadReconciliation();
  } finally {
    actionLoading.value = '';
  }
};

const actOnMatch = async (matchId, action) => {
  const messages = {
    confirm: 'Match confirmed',
    reject: 'Suggestion rejected',
    unconfirm: 'Match unconfirmed — line returned to the worklist',
  };
  await api.post(`/api/bank-feeds/statements/matches/${matchId}/${action}`, {},
    { successMessage: messages[action] || 'Done' });
  await loadReconciliation();
};

// Manual match dialog
const showManualMatch = ref(false);
const manualLine = ref(null);
const manualCandidates = ref({ payments: [], expenses: [], vendor_invoices: [] });
const manualSelected = ref([]);
const manualClassification = ref(null);
const manualNote = ref('');
const classificationOptions = [
  { label: 'Transfer between accounts', value: 'transfer' },
  { label: 'Bank fee', value: 'bank_fee' },
  { label: 'Interest', value: 'interest' },
  { label: 'Owner draw / contribution', value: 'owner' },
  { label: 'Processor payout', value: 'processor' },
  { label: 'Other (non-books)', value: 'other' },
];

const openManualMatch = async (line) => {
  manualLine.value = line;
  manualSelected.value = [];
  manualClassification.value = null;
  manualNote.value = '';
  manualCandidates.value = { payments: [], expenses: [], vendor_invoices: [] };
  showManualMatch.value = true;
  manualCandidates.value = await api.get(
    `/api/bank-feeds/statements/reconcile/candidates?account_id=${reconAccountId.value}&line_id=${line.id}`);
};

const saveManualMatch = async () => {
  actionLoading.value = 'manual';
  try {
    await api.post('/api/bank-feeds/statements/matches', {
      account_id: reconAccountId.value,
      line_ids: [manualLine.value.id],
      externals: manualSelected.value.map((key) => {
        const [source_table, source_id] = key.split(':');
        return { source_table, source_id };
      }),
      classification: manualClassification.value,
      note: manualNote.value || null,
    }, { successMessage: 'Match created' });
    showManualMatch.value = false;
    await loadReconciliation();
  } finally {
    actionLoading.value = '';
  }
};

// Void (local confirm dialog on purpose — see issue #215:
// useDestructiveConfirm can auto-accept without rendering).

const showVoidConfirm = ref(false);
const voidTarget = ref(null);

const askVoidImport = (row) => {
  voidTarget.value = row;
  showVoidConfirm.value = true;
};

const voidImport = async () => {
  if (!voidTarget.value) return;
  actionLoading.value = 'void';
  try {
    await api.post(
      `/api/bank-feeds/statements/imports/${voidTarget.value.id}/void`,
      {},
      { successMessage: 'Import voided' },
    );
    showVoidConfirm.value = false;
    voidTarget.value = null;
    await loadStatementImports();
  } finally {
    actionLoading.value = '';
  }
};

// ── schedule ───────────────────────────────────────────────────────

const scheduleForm = reactive({ frequency: 'manual', backfill_days: 365 });
const frequencyOptions = [
  { label: 'Manual only', value: 'manual' },
  { label: 'Hourly', value: 'hourly' },
  { label: 'Every 4 hours', value: 'every_4h' },
  { label: 'Daily', value: 'daily' },
  { label: 'Weekly', value: 'weekly' },
];

const saveSchedule = async () => {
  actionLoading.value = 'schedule';
  try {
    const data = await api.put('/api/bank-feeds/schedule', {
      frequency: scheduleForm.frequency,
      backfill_days: scheduleForm.backfill_days,
    }, { successMessage: 'Schedule saved' });
    schedule.value = data;
  } finally {
    actionLoading.value = '';
  }
};

// ── lifecycle ──────────────────────────────────────────────────────

onMounted(() => {
  window.addEventListener('message', onOAuthMessage);
  loadStatus();
  loadTransactions();
  loadDocuments();
  loadStatementImports();
});

onBeforeUnmount(() => {
  window.removeEventListener('message', onOAuthMessage);
  if (popupWatch) clearInterval(popupWatch);
});
</script>

<style scoped>
.imports-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin-bottom: 0.5rem;
}
.imports-title {
  margin: 0;
}
.bank-docs-title {
  margin-top: 1.75rem;
  margin-bottom: 0.5rem;
}
.stmt-summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 0.5rem 1rem;
  margin-bottom: 1rem;
}
.stmt-summary-grid .muted {
  display: block;
  font-size: 0.85em;
}
.continuity-warnings {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-top: 0.75rem;
}
.stmt-image-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 0.75rem;
}
.stmt-image-card {
  margin: 0;
  border: 1px solid var(--surface-border, var(--p-content-border-color, #ddd));
  border-radius: 6px;
  padding: 0.5rem;
  background: var(--surface-card, var(--p-content-background, transparent));
}
.stmt-image-card img {
  width: 100%;
  height: auto;
  border-radius: 4px;
  /* Scans are white paper — keep the surface white in BOTH themes, and pin
     dark text with it so alt text can't go white-on-white in dark mode. */
  background: #fff;
  color: #334155;
}
.stmt-image-card figcaption {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin-top: 0.4rem;
  font-size: 0.9em;
}
.stmt-image-loading {
  display: flex;
  justify-content: center;
  padding: 1.25rem 0;
}
.recon-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 0.75rem;
  margin: 0.75rem 0 1rem;
}
.recon-card {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  padding: 0.75rem 1rem;
  border: 1px solid var(--surface-border, var(--p-content-border-color, #ddd));
  border-radius: 8px;
}
.recon-count {
  font-size: 1.5rem;
  font-weight: 700;
}
.recon-note {
  margin: 0.4rem 0 0;
  font-size: 0.9em;
}
.manual-candidate {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.25rem 0;
}
.manual-classify-row {
  margin-top: 0.75rem;
}
.manual-note {
  width: 100%;
  margin-top: 0.5rem;
}
.broken-title {
  color: var(--p-red-500, #dc2626);
}

.bank-feeds-tabview {
  margin-top: 1rem;
}
.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem;
}
.row-actions {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}
.add-bank-row {
  margin-top: 1rem;
}
.filters-row {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 1rem;
}
.filter-input {
  min-width: 170px;
}
.pending-toggle {
  display: inline-flex;
  gap: 0.4rem;
  align-items: center;
}
.amount-out {
  color: var(--p-red-600, #dc2626);
  font-variant-numeric: tabular-nums;
}
.amount-in {
  color: var(--p-green-700, #15803d);
  font-variant-numeric: tabular-nums;
}
.muted {
  color: var(--p-text-muted-color, #64748b);
}
.settings-panel {
  max-width: 480px;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.settings-row {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.dialog-form {
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}
.dialog-form label {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-weight: 500;
}
</style>
