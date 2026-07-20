<template>
    <section class="settings-view view-card">
      <Tabs v-model:value="activeTab" data-testid="settings-tabs">
        <TabList>
          <Tab value="branding">Branding</Tab>
          <Tab value="modules">Modules</Tab>
          <Tab value="users">Users</Tab>
          <Tab value="integrations">Integrations</Tab>
          <Tab value="tax">Tax</Tab>
          <Tab value="billing">Billing Terms</Tab>
          <Tab value="estimates">Feature Settings</Tab>
          <Tab value="margin-tiers">Margin Tiers</Tab>
        </TabList>
        <TabPanels>
        <!-- Branding Tab -->
        <TabPanel value="branding">
          <div class="branding-grid">
            <div class="form-field">
              <label for="company-name">Company Name</label>
              <InputText id="company-name" v-model="branding.companyName" data-testid="company-name" />
            </div>

            <div class="form-field">
              <label for="logo-upload">Logo</label>
              <input id="logo-upload" data-testid="logo-upload" type="file" accept="image/*" @change="onLogoSelect" />
              <small class="hint">PNG or JPG, max 2MB</small>
            </div>

            <div class="form-field">
              <label for="primary-color">Primary Color</label>
              <div class="color-row">
                <input
                  id="primary-color"
                  type="color"
                  :value="normalizeHex(branding.primaryColor)"
                  data-testid="primary-color"
                  @input="branding.primaryColor = $event.target.value"
                />
                <InputText
                  id="primary-color-hex"
                  name="primary-color-hex"
                  :model-value="normalizeHex(branding.primaryColor)"
                  class="color-hex-input"
                  data-testid="primary-color-hex"
                  @update:model-value="branding.primaryColor = $event"
                />
              </div>
            </div>

            <div class="form-field">
              <label for="secondary-color">Secondary Color</label>
              <div class="color-row">
                <input
                  id="secondary-color"
                  type="color"
                  :value="normalizeHex(branding.accentColor)"
                  data-testid="secondary-color"
                  @input="branding.accentColor = $event.target.value"
                />
                <InputText
                  id="secondary-color-hex"
                  name="secondary-color-hex"
                  :model-value="normalizeHex(branding.accentColor)"
                  class="color-hex-input"
                  data-testid="secondary-color-hex"
                  @update:model-value="branding.accentColor = $event"
                />
              </div>
            </div>
          </div>

          <Card class="branding-preview" data-testid="branding-preview">
            <template #title>Preview</template>
            <template #content>
              <div
                class="preview-chip"
                :style="{ backgroundColor: normalizeHex(branding.primaryColor), color: normalizeHex(branding.accentColor) }"
              >
                {{ branding.companyName || "Company Name" }}
              </div>
            </template>
          </Card>

          <div class="form-actions">
            <Button label="Save Branding" icon="pi pi-save" data-testid="save-branding" :loading="brandingSaving" @click="saveBranding" />
          </div>
          <p v-if="saveState" :class="saveState === 'Saved' ? 'inline-success' : 'inline-info'" data-testid="branding-save-state">{{ saveState }}</p>
        </TabPanel>

        <!-- Modules Tab -->
        <TabPanel value="modules">
          <div v-if="modulesLoading" class="spinner-wrap"><ProgressSpinner /></div>
          <div v-else data-testid="modules-list" class="modules-groups">
            <section v-for="group in modulesByTier" :key="group.tier" class="module-tier-group">
              <h4 class="tier-title">
                {{ group.label }}
                <Badge :value="group.items.length" severity="info" />
              </h4>
              <div class="module-list">
                <div v-for="mod in group.items" :key="mod.key" class="module-row" :class="{ 'module-row--dirty': isModuleDirty(mod.key) }">
                  <div class="module-info">
                    <span class="module-name">{{ mod.name }}</span>
                    <small v-if="mod.description" class="module-desc">{{ mod.description }}</small>
                    <small v-if="isModuleDirty(mod.key)" class="module-pending" data-testid="module-pending-flag">Pending — click Save to apply</small>
                  </div>
                  <div class="module-actions">
                    <Tag v-if="mod.locked" value="Upgrade" severity="warn" data-testid="module-locked-tag" />
                    <ToggleSwitch
                      v-else
                      :input-id="'module-toggle-input-' + mod.key"
                      :name="'module-toggle-' + mod.key"
                      :aria-label="'Toggle ' + (mod.label || mod.key)"
                      :model-value="pendingEnabled(mod.key)"
                      :disabled="modulesBusy"
                      :style="modulesBusy ? { opacity: 0.5, cursor: 'not-allowed' } : {}"
                      :data-testid="'module-toggle-' + mod.key"
                      @update:model-value="setPendingModule(mod, $event)"
                    />
                  </div>
                </div>
              </div>
            </section>
            <p v-if="modules.length === 0" class="muted">No modules available.</p>
          </div>
          <div v-if="modules.length > 0" class="modules-actions">
            <p v-if="dirtyModuleKeys.length > 0" class="modules-dirty-hint" data-testid="modules-dirty-hint">
              {{ dirtyModuleKeys.length }} pending change{{ dirtyModuleKeys.length === 1 ? '' : 's' }}
            </p>
            <Button
              label="Revert"
              severity="secondary"
              text
              :disabled="modulesBusy || dirtyModuleKeys.length === 0"
              data-testid="modules-revert-btn"
              @click="revertModuleChanges"
            />
            <Button
              label="Save"
              :loading="modulesBusy"
              :disabled="modulesBusy || dirtyModuleKeys.length === 0"
              data-testid="modules-save-btn"
              @click="saveModuleChanges"
            />
          </div>
        </TabPanel>

        <!-- Users Tab -->
        <TabPanel value="users">
          <Toolbar>
            <template #start>
              <InputText id="users-search" name="users-search" v-model="userSearch" placeholder="Search users" data-testid="users-search" />
            </template>
            <template #end>
              <Button label="Invite User" icon="pi pi-user-plus" data-testid="invite-user-btn" @click="showInviteDialog = true" />
            </template>
          </Toolbar>

          <div v-if="usersLoading" class="spinner-wrap"><ProgressSpinner /></div>

          <DataTable v-else :value="filteredUsers" data-testid="users-table" striped-rows>
            <Column field="name" header="Name">
              <template #body="{ data }">
                <span class="user-name">{{ data.name || data.username || data.email }}</span>
              </template>
            </Column>
            <Column field="email" header="Email" />
            <Column field="role" header="Role">
              <template #body="{ data }">
                <Tag
                  :value="data.role"
                  :severity="roleSeverity(data.role)"
                  :data-testid="'user-role-' + data.id"
                />
              </template>
            </Column>
            <Column field="is_active" header="Status">
              <template #body="{ data }">
                <Tag :value="data.is_active !== false ? 'Active' : 'Inactive'" :severity="data.is_active !== false ? 'success' : 'danger'" />
              </template>
            </Column>
            <Column field="last_login_at" header="Last Login">
              <template #body="{ data }">{{ (data.last_login_at || data.last_login) ? (data.last_login_at || data.last_login).split('T')[0] : 'Never' }}</template>
            </Column>
          </DataTable>

          <!-- Invite User Dialog -->
          <Dialog v-model:visible="showInviteDialog" header="Invite User" data-testid="invite-user-dialog" :style="{ width: '28rem' }">
            <form class="dialog-form" @submit.prevent="submitInvite">
              <div class="form-field">
                <label for="invite-email">Email *</label>
                <InputText id="invite-email" v-model="inviteForm.email" type="email" data-testid="invite-email-input" />
              </div>
              <div class="form-field">
                <label for="invite-role">Role *</label>
                <Select
                  id="invite-role"
                  v-model="inviteForm.role"
                  :options="roleOptions"
                  data-testid="invite-role-dropdown"
                />
              </div>
              <div v-if="inviteError" class="inline-error" data-testid="invite-error">{{ inviteError }}</div>
              <div class="form-actions">
                <Button type="button" label="Cancel" text @click="showInviteDialog = false" />
                <Button type="submit" label="Send Invite" :loading="inviteSaving" data-testid="invite-submit-btn" />
              </div>
            </form>
          </Dialog>
        </TabPanel>

        <!-- Integrations Tab -->
        <TabPanel value="integrations">
          <div class="integrations-grid">
            <div class="integration-shell" data-testid="integration-qbo">
              <div class="integration-shell-header">
                <div>
                  <h3>QuickBooks Online</h3>
                  <p class="muted">Sync customers, invoices, and payments with your QBO company.</p>
                </div>
                <Tag
                  :value="integrations.quickbooks.needsReconnect
                    ? 'Reconnect required'
                    : (integrations.quickbooks.connected ? 'Connected' : 'Not Connected')"
                  :severity="integrations.quickbooks.needsReconnect
                    ? 'danger'
                    : (integrations.quickbooks.connected ? 'success' : 'warning')"
                  data-testid="qbo-status-tag"
                />
              </div>
              <small v-if="integrations.quickbooks.lastSync" class="muted">Last sync: {{ integrations.quickbooks.lastSync }}</small>
              <p
                v-if="integrations.quickbooks.needsReconnect"
                class="status-row status-error"
                data-testid="qbo-reconnect-banner"
              >
                QuickBooks refresh token rejected. Sync is paused until you reconnect.
              </p>
              <div class="integration-actions">
                <Button
                  :label="integrations.quickbooks.needsReconnect
                    ? 'Reconnect QuickBooks'
                    : (integrations.quickbooks.connected ? 'Reconnect' : 'Connect')"
                  :severity="integrations.quickbooks.needsReconnect ? 'danger' : (integrations.quickbooks.connected ? 'secondary' : 'primary')"
                  data-testid="qbo-connect-btn"
                  @click="connectIntegration('quickbooks')"
                />
                <Button
                  v-if="integrations.quickbooks.connected && !integrations.quickbooks.needsReconnect"
                  label="Sync Now" icon="pi pi-sync" severity="info" size="small"
                  @click="syncNow('quickbooks')"
                />
                <Button v-if="integrations.quickbooks.connected" label="Disconnect" severity="danger" size="small" text @click="disconnectIntegration('quickbooks')" />
              </div>
              <div class="status-row" style="margin-top:0.5rem; gap:0.5rem; align-items:center;">
                <ToggleSwitch
                  v-model="integrations.catalogSyncEnabled"
                  :disabled="integrations.catalogSyncSaving"
                  inputId="qbo-catalog-sync"
                  data-testid="qbo-catalog-sync-toggle"
                  @update:modelValue="toggleCatalogSync"
                />
                <label for="qbo-catalog-sync">Catalog sync (pull/push)</label>
                <small class="muted">Off by default — blocks QB from repopulating the catalog.</small>
              </div>
            </div>

            <div class="integration-shell" data-testid="integration-stripe">
              <div class="integration-shell-header">
                <div>
                  <h3>Stripe Payments</h3>
                  <p class="muted">Accept card and ACH payments from your customers.</p>
                </div>
                <Tag :value="integrations.stripe.connected ? 'Connected' : 'Not Connected'" :severity="integrations.stripe.connected ? 'success' : 'warning'" />
              </div>
              <small v-if="integrations.stripe.mode" class="muted">Mode: {{ integrations.stripe.mode }}</small>
              <div class="integration-actions">
                <Button
                  :label="integrations.stripe.connected ? 'Reconnect' : 'Connect'"
                  :severity="integrations.stripe.connected ? 'secondary' : 'primary'"
                  data-testid="stripe-connect-btn"
                  @click="connectIntegration('stripe')"
                />
                <Button v-if="integrations.stripe.connected" label="Disconnect" severity="danger" size="small" text @click="disconnectIntegration('stripe')" />
              </div>
            </div>

            <div class="integration-shell" data-testid="integration-sms">
              <div class="integration-shell-header">
                <div>
                  <h3>SMS Provider</h3>
                  <p class="muted">Send appointment reminders and follow-ups via text message.</p>
                </div>
                <Tag :value="integrations.sms.connected ? 'Connected' : 'Not Connected'" :severity="integrations.sms.connected ? 'success' : 'warning'" />
              </div>
              <small v-if="integrations.sms.provider" class="muted">Provider: {{ integrations.sms.provider }}</small>
              <div class="integration-actions">
                <Button
                  :label="integrations.sms.connected ? 'Reconnect' : 'Connect'"
                  :severity="integrations.sms.connected ? 'secondary' : 'primary'"
                  data-testid="sms-connect-btn"
                  @click="connectIntegration('sms')"
                />
              </div>
            </div>

            <GoogleMapsIntegrationCard />

            <AIAssistantIntegrationCard />

            <PhoneComIntegrationCard />

            <OutlookIntegrationCard />

            <div class="integration-shell" data-testid="integration-email">
              <div class="integration-shell-header">
                <div>
                  <h3>Email Configuration</h3>
                  <p class="muted">SMTP credentials GDX uses to send estimates, invoices, and notifications.</p>
                </div>
                <Tag v-if="emailConfig.is_verified" severity="success" value="Verified" />
              </div>
              <form class="settings-form" @submit.prevent="saveEmailConfig" autocomplete="on">
                <div class="form-row">
                  <div class="form-field">
                    <label for="email-provider" class="form-label-text">Provider</label>
                    <select
                      id="email-provider"
                      name="email-provider"
                      class="native-select"
                      data-testid="email-provider"
                      :value="emailConfig.provider"
                      @change="($event) => { emailConfig.provider = $event.target.value; onProviderChange(); }"
                    >
                      <option v-for="opt in emailProviders" :key="opt.value" :value="opt.value">{{ opt.label }}</option>
                    </select>
                  </div>
                  <div class="form-field">
                    <label for="email-username">Username (email)</label>
                    <InputText id="email-username" name="email-username" autocomplete="username" v-model="emailConfig.username" data-testid="email-username" class="w-full" />
                  </div>
                </div>
                <div class="form-row">
                  <div class="form-field">
                    <label for="email-password">Password / App Password</label>
                    <Password input-id="email-password" input-props="{ name: 'email-password', autocomplete: 'current-password' }" v-model="emailConfig.password" toggleMask :feedback="false" data-testid="email-password" class="w-full" />
                  </div>
                  <div class="form-field">
                    <label for="email-host">SMTP Host</label>
                    <InputText id="email-host" name="email-host" v-model="emailConfig.smtp_host" data-testid="email-host" class="w-full" />
                  </div>
                </div>
                <div class="form-row">
                  <div class="form-field">
                    <label for="email-port">SMTP Port</label>
                    <InputText id="email-port" name="email-port" v-model="emailConfig.smtp_port" data-testid="email-port" class="w-full" />
                  </div>
                  <div class="form-field">
                    <label for="email-from">From Email</label>
                    <InputText id="email-from" name="email-from" autocomplete="email" v-model="emailConfig.from_email" data-testid="email-from" class="w-full" />
                  </div>
                </div>
                <div class="form-row">
                  <div class="form-field">
                    <label for="email-from-name">From Name</label>
                    <InputText id="email-from-name" name="email-from-name" v-model="emailConfig.from_name" placeholder="Example Garage Doors" data-testid="email-from-name" class="w-full" />
                  </div>
                </div>
                <div class="integration-actions">
                  <Button type="submit" label="Save Email Settings" icon="pi pi-save" data-testid="email-save" />
                  <Button type="button" label="Send Test Email" icon="pi pi-send" severity="secondary" @click="testEmailConfig" data-testid="email-test" />
                </div>
              </form>
            </div>

            <div class="integration-shell" data-testid="integration-outlook-personal">
              <div class="integration-shell-header">
                <div>
                  <h3><i class="pi pi-microsoft" style="margin-right:0.5rem"></i>Your Outlook Mailbox</h3>
                  <p class="muted">Connect your own Microsoft 365 mailbox so emails to/from your customers show up in GDX.</p>
                </div>
              </div>
              <OutlookConnectButton />
            </div>
          </div>
        </TabPanel>


        <!-- Tax Tab — 2026-04-29. Single-rate today. Future phases (jurisdiction
             lookup, customer exemptions UI, provider plugins) layer in here. -->
        <TabPanel value="tax">
          <Card>
            <template #title>Sales Tax</template>
            <template #content>
              <p class="muted" style="margin-top:0">
                Set the default rate the system applies to estimates and
                invoices. Customer-level exemptions and jurisdiction overrides
                are tracked in the Tax module — exposed here as future tabs.
              </p>
              <div class="form-grid">
                <div class="form-field" style="max-width:300px">
                  <label for="tax-rate">Default Rate (%)</label>
                  <InputNumber
                    id="tax-rate"
                    v-model="taxConfig.default_rate_pct"
                    :min="0" :max="100"
                    :min-fraction-digits="2" :max-fraction-digits="4"
                    suffix="%"
                    data-testid="tax-rate-input"
                  />
                  <small class="muted">Stored as a fraction (e.g. 7.38% = 0.0738).</small>
                </div>
                <div class="form-field" style="max-width:400px">
                  <label for="tax-name">Rate Name</label>
                  <InputText
                    id="tax-name"
                    v-model="taxConfig.name"
                    placeholder="e.g. MN Combined Sales Tax"
                    data-testid="tax-name-input"
                  />
                </div>
                <div class="form-field" style="max-width:600px">
                  <label class="flex align-items-center gap-2">
                    <ToggleSwitch v-model="taxConfig.tax_labor" inputId="tax-labor" data-testid="tax-labor-toggle" />
                    <span>Tax labor lines</span>
                  </label>
                  <small class="muted">
                    Most US states do NOT tax service labor. Leave off unless your jurisdiction
                    requires it (parts of WV, HI, NM, SD, etc.). When off, estimate lines with
                    category "Labor" are excluded from the taxable subtotal.
                  </small>
                </div>
                <div class="form-field" style="max-width:600px">
                  <label for="tax-description">Notes</label>
                  <Textarea
                    id="tax-description"
                    v-model="taxConfig.description"
                    rows="2"
                    placeholder="Reference (e.g. MN Dept of Revenue rate sheet, effective date, jurisdictions covered)"
                    data-testid="tax-description-input"
                  />
                </div>
                <div>
                  <Button label="Save Tax Settings" icon="pi pi-save" @click="saveTaxConfig" :loading="taxSaving" data-testid="tax-save" />
                  <span v-if="taxConfig.configured_at" class="muted" style="margin-left:1rem">
                    Last updated {{ formatDate(taxConfig.configured_at) }}
                  </span>
                </div>
              </div>
            </template>
          </Card>
        </TabPanel>


        <!-- Estimates Tab — 2026-04-30. Per-tenant feature toggles for the
             estimate editor. -->
        <TabPanel value="estimates">
          <Card data-testid="security-card" style="margin-bottom:1rem">
            <template #title>Security</template>
            <template #content>
              <div style="display:flex; flex-direction:column; gap:0.6rem;">
                <strong>Auto-logout on inactivity</strong>
                <div class="muted">
                  Sign out after a period of no activity (mouse, keyboard, touch). Applies to
                  every user — each picks up a change on their next sign-in or page reload.
                  Set to 0 to disable.
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <InputNumber v-model="idleTimeoutMin" :min="0" :max="480" suffix=" min"
                    :useGrouping="false" style="width: 10rem" data-testid="idle-timeout-min" />
                  <Button label="Save" icon="pi pi-save" :loading="idleTimeoutSaving" @click="saveIdleTimeout" data-testid="idle-timeout-save" />
                  <span class="muted">{{ idleTimeoutMin > 0 ? `logs out after ${idleTimeoutMin} min idle` : 'disabled' }}</span>
                </div>
              </div>
            </template>
          </Card>

          <Card data-testid="debug-logging-card" style="margin-bottom:1rem">
            <template #title>Debug logging</template>
            <template #content>
              <div style="display:flex; align-items:center; gap:0.75rem;">
                <ToggleSwitch v-model="debugLoggingEnabled" inputId="debug-logging" data-testid="debug-logging-toggle" />
                <div>
                  <strong>Record handled server errors</strong>
                  <div class="muted">
                    When on, errors that are normally handled silently — like the
                    support / <code>cc_support_tickets</code> integration being unavailable —
                    are also written to the
                    <RouterLink to="/server-errors">Server Errors</RouterLink> log (grouped by
                    type) so you can confirm whether an integration is failing. Leave off for
                    normal operation.
                  </div>
                </div>
              </div>
              <div style="margin-top:0.85rem;">
                <Button label="Save" icon="pi pi-save" :loading="debugLoggingSaving" @click="saveDebugLogging" data-testid="debug-logging-save" />
              </div>
            </template>
          </Card>

          <Card>
            <template #title>Estimates</template>
            <template #content>
              <p class="muted" style="margin-top:0">
                Toggle estimate editor capabilities for this tenant.
              </p>
              <div style="display:flex; flex-direction:column; gap:0.75rem;">
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="estimatesFeatures.estimates_allow_line_margin_override" data-testid="est-allow-line-margin" />
                  <div>
                    <strong>Per-line margin override</strong>
                    <div class="muted">
                      Show an editable margin % on each estimate line. Typing a margin
                      re-prices that line from cost. When off, lines use the tier margin only.
                    </div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="estimatesFeatures.estimates_hide_line_prices" data-testid="est-hide-line-prices" />
                  <div>
                    <strong>Hide line-item prices</strong>
                    <div class="muted">
                      Default for new estimates: the customer PDF/email and install sheet
                      show line items and quantities plus the subtotal, tax and total,
                      hiding only the per-line Unit Price / Line Total. Any estimate can
                      override this individually.
                    </div>
                  </div>
                </div>
                <Divider />
                <div style="display:flex; flex-direction:column; gap:0.4rem;">
                  <strong>Default Terms</strong>
                  <div class="muted">
                    Text rendered in the "Terms" section of every estimate PDF for this
                    tenant. Per-estimate notes are entered on each estimate and render
                    underneath.
                  </div>
                  <Textarea
                    v-model="estimatesFeatures.estimates_default_terms"
                    rows="4"
                    placeholder="e.g. Estimate valid for 30 days. Deposit of 50% required to schedule. Payment due on completion."
                    class="w-full"
                    data-testid="est-default-terms"
                  />
                </div>
                <Divider />
                <div style="display:flex; flex-direction:column; gap:0.4rem;" v-pre>
                  <strong>Estimate Email — Subject</strong>
                  <div class="muted">
                    Subject line when emailing an estimate. Placeholders:
                    <code>&#123;&#123;job_title&#125;&#125;</code>,
                    <code>&#123;&#123;customer_name&#125;&#125;</code>,
                    <code>&#123;&#123;estimate_number&#125;&#125;</code>,
                    <code>&#123;&#123;company_name&#125;&#125;</code>,
                    <code>&#123;&#123;total&#125;&#125;</code>.
                    Leave blank for the default <em>&#123;&#123;job_title&#125;&#125;</em>.
                  </div>
                </div>
                <InputText
                  v-model="estimatesFeatures.estimate_email_subject_template"
                  :placeholder="emailSubjectPlaceholder"
                  class="w-full"
                  data-testid="est-email-subject-template"
                />
                <div style="display:flex; flex-direction:column; gap:0.4rem;">
                  <strong>Estimate Email — Body</strong>
                  <div class="muted">
                    Pre-built message body that loads when you click "Email Customer".
                    Same placeholders as the subject. The PDF and any attachments are
                    added automatically — don't restate the line items here.
                  </div>
                  <Textarea
                    v-model="estimatesFeatures.estimate_email_body_template"
                    rows="6"
                    :placeholder="emailBodyPlaceholder"
                    class="w-full"
                    data-testid="est-email-body-template"
                  />
                </div>
                <Divider />
                <div style="display:flex; flex-direction:column; gap:0.4rem;">
                  <strong>Deposit %</strong>
                  <div class="muted">
                    Percentage shown as the down-payment line on every estimate
                    PDF for this tenant ("X% Down: $Y"). Set to 0 to hide.
                  </div>
                  <InputNumber
                    v-model="estimatesFeatures.estimate_deposit_pct"
                    :min="0" :max="100" suffix="%"
                    style="width: 8rem"
                    data-testid="est-deposit-pct"
                  />
                </div>
                <div>
                  <Button label="Save Estimate Settings" icon="pi pi-save" @click="saveEstimatesFeatures" :loading="estimatesFeaturesSaving" data-testid="estimates-features-save" />
                </div>
              </div>
            </template>
          </Card>

          <!-- Catalog card (was its own tab; consolidated into Feature Settings 2026-04-30). -->
          <Card style="margin-top:1rem">
            <template #title>Catalog</template>
            <template #content>
              <p class="muted" style="margin-top:0">
                How GDX handles catalog items with empty / missing descriptions and zero prices.
              </p>
              <div style="display:flex; flex-direction:column; gap:0.75rem;">
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="catalogPolicy.catalog_require_description" data-testid="cat-require-desc" />
                  <div>
                    <strong>Require description on create / edit</strong>
                    <div class="muted">Block save (422) when description is blank. Stricter; turn on once your catalog is clean.</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="catalogPolicy.catalog_render_name_when_desc_empty" data-testid="cat-render-fallback" />
                  <div>
                    <strong>Show item name when description is empty</strong>
                    <div class="muted">Soft fallback — invoice lines + lists fall back to the item name. Default on.</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="catalogPolicy.catalog_ai_suggest_descriptions" data-testid="cat-ai-suggest" />
                  <div>
                    <strong>AI suggestion button in catalog editor</strong>
                    <div class="muted">Adds "Suggest description" — uses your tenant AI assistant. Customer can review before saving.</div>
                  </div>
                </div>
                <Divider />
                <strong>Pricing rules</strong>
                <small class="muted" style="display:block; margin-bottom:0.5rem">
                  Many catalog items have $0 price (typically a QBO import side-effect).
                  These toggles control how the system reacts.
                </small>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="catalogPolicy.catalog_block_zero_price_on_invoice" data-testid="cat-block-zero-invoice" />
                  <div>
                    <strong>Block $0 lines on invoice</strong>
                    <div class="muted">Hard 422 — invoice can't save with a $0 catalog line.</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="catalogPolicy.catalog_warn_zero_price_on_invoice" data-testid="cat-warn-zero-invoice" />
                  <div>
                    <strong>Warn on $0 lines</strong>
                    <div class="muted">Soft yellow banner on invoice; allow save. Default on.</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="catalogPolicy.catalog_block_zero_price_on_save" data-testid="cat-block-zero-save" />
                  <div>
                    <strong>Block zero-price catalog saves</strong>
                    <div class="muted">Catalog item create/edit returns 422 when price is 0.</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="catalogPolicy.catalog_auto_inactivate_zero_price" data-testid="cat-auto-inactivate" />
                  <div>
                    <strong>Auto-inactivate $0 items</strong>
                    <div class="muted">$0 items get marked inactive — they stop appearing in tech pickers until priced.</div>
                  </div>
                </div>
                <div style="margin-top:0.5rem">
                  <a href="#" @click.prevent="loadItemsNeedingPricing" data-testid="cat-items-needing-pricing-link">
                    View items needing pricing →
                  </a>
                </div>
                <Dialog v-model:visible="showItemsNeedingPricing" header="Items needing pricing" style="width:80vw" modal>
                  <p class="muted">Active catalog items with no price set ({{ itemsNeedingPricing.length }} found).</p>
                  <DataTable :value="itemsNeedingPricing" stripedRows responsiveLayout="scroll" :paginator="true" :rows="25">
                    <Column field="sku" header="SKU" />
                    <Column field="name" header="Name" />
                    <Column field="category" header="Category" />
                    <Column field="cost" header="Cost" />
                    <Column field="price" header="Price" />
                    <Column field="description" header="Description" />
                  </DataTable>
                </Dialog>
                <div>
                  <Button label="Save Catalog Settings" icon="pi pi-save" @click="saveCatalogPolicy" :loading="catalogPolicySaving" data-testid="catalog-policy-save" />
                </div>
              </div>
            </template>
          </Card>

          <!-- Job Numbering card (was its own tab; consolidated into Feature Settings 2026-04-30). -->
          <Card style="margin-top:1rem">
            <template #title>Job Numbering</template>
            <template #content>
              <p class="muted" style="margin-top:0">
                Pick how new job numbers look and what number to start at.
                Tokens: <code>{seq}</code> · <code>{seq:003}</code> (zero-pad)
                · <code>{year}</code> · <code>{yy}</code> · <code>{month}</code>
                · <code>{customer_initials}</code>.
              </p>
              <div class="form-grid">
                <div class="form-field" style="max-width:400px">
                  <label>Preset</label>
                  <Select
                    v-model="numberingForm.preset"
                    :options="numberingPresets"
                    optionLabel="label"
                    optionValue="value"
                    @change="onNumberingPresetChange"
                    data-testid="numbering-preset"
                  />
                </div>
                <div class="form-field" style="max-width:500px">
                  <label for="numbering-format">Format Template</label>
                  <InputText
                    id="numbering-format"
                    v-model="numberingForm.format"
                    placeholder="JOB-{year}-{seq:003}"
                    data-testid="numbering-format"
                  />
                  <small class="muted">Edit directly for a custom shape.</small>
                </div>
                <div class="form-field" style="max-width:200px">
                  <label for="numbering-start">Next Number</label>
                  <InputNumber
                    id="numbering-start"
                    v-model="numberingForm.next_seq"
                    :min="1"
                    :useGrouping="false"
                    data-testid="numbering-next-seq"
                  />
                  <small class="muted">The sequence to assign on the next created job.</small>
                </div>
                <div class="form-field" style="max-width:600px">
                  <label>Live Preview</label>
                  <div style="font-family:monospace; font-size:1.1rem; padding:0.5rem 0.75rem; background:var(--surface-elevated, var(--p-content-hover-background)); border-radius:4px;" data-testid="numbering-preview">
                    {{ numberingPreview }}
                  </div>
                </div>
                <div>
                  <Button label="Save Numbering" icon="pi pi-save" @click="saveNumbering" :loading="numberingSaving" data-testid="numbering-save" />
                </div>
              </div>
            </template>
          </Card>

          <!-- Job Workflow card (was its own tab; consolidated into Feature Settings 2026-04-30). -->
          <Card style="margin-top:1rem">
            <template #title>Job Workflow</template>
            <template #content>
              <p class="muted" style="margin-top:0">
                When a tech taps <strong>Start Job</strong>, we always stamp the
                start time and auto-assign the user. Turn on any of the extras below.
              </p>
              <div class="form-grid" style="display:flex; flex-direction:column; gap:0.75rem;">
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="workflowFlags.lock_schedule_on_start" data-testid="wf-lock" />
                  <div>
                    <strong>Lock schedule on start</strong>
                    <div class="muted">Once started, the schedule slot can't be changed without admin override.</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="workflowFlags.post_arrival_event" data-testid="wf-arrival-event" />
                  <div>
                    <strong>Post arrival event to customer timeline</strong>
                    <div class="muted">Adds a "tech arrived at HH:MM" note to the job + customer history.</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="workflowFlags.sms_arrival_notify" data-testid="wf-sms" />
                  <div>
                    <strong>Text customer "Tech is on the way"</strong>
                    <div class="muted">Requires phone.com integration. Sends from the configured tenant number.</div>
                  </div>
                </div>
                <Divider />
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="workflowFlags.require_parts_on_complete" data-testid="wf-req-parts" />
                  <div>
                    <strong>Require parts list on Complete</strong>
                    <div class="muted">Block completion until parts are logged — or the tech explicitly checks "No parts used".</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="workflowFlags.require_invoice_on_complete" data-testid="wf-req-invoice" />
                  <div>
                    <strong>Require invoice before Complete</strong>
                    <div class="muted">Hard gate for invoice-up-front shops. Leave OFF if you bill after the job — the daily billing follow-up chases those instead.</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="workflowFlags.require_hours_on_complete" data-testid="wf-req-hours" />
                  <div>
                    <strong>Require labor hours on Complete</strong>
                    <div class="muted">Block completion until hours are entered.</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="workflowFlags.require_signature_on_complete" data-testid="wf-req-sig" />
                  <div>
                    <strong>Require signature on Complete</strong>
                    <div class="muted">Block completion until the customer has signed.</div>
                  </div>
                </div>
                <div>
                  <Button label="Save Workflow Settings" icon="pi pi-save" @click="saveWorkflowFlags" :loading="workflowSaving" data-testid="workflow-save" />
                </div>
              </div>
            </template>
          </Card>

          <!-- Dispatch card — 2026-05-01. Per-tenant gates for jobs scheduled
               without a tech, plus visibility of the Dispatch board lane. -->
          <!-- Time Clock card — 2026-05-03 / S92. Tenant-local timezone
               drives display formatting on TimeclockView and MobileTodayView. -->
          <Card style="margin-top:1rem">
            <template #title>Time Clock</template>
            <template #content>
              <p class="muted" style="margin-top:0">
                Timezone for time clock display, daily timecard rollup, and
                "Today's Route" date. All timestamps are stored in UTC; this
                only changes how they're shown to the tech.
              </p>
              <div class="form-grid" style="display:flex; flex-direction:column; gap:0.75rem; max-width: 480px;">
                <div>
                  <label style="display:block; font-weight:500; margin-bottom:0.25rem;">Tenant timezone</label>
                  <Select
                    v-model="timeClockSettings.timezone"
                    :options="TIMEZONE_OPTIONS"
                    optionLabel="label"
                    optionValue="value"
                    placeholder="Select timezone"
                    data-testid="timeclock-timezone-select"
                  />
                  <div class="muted" style="font-size:0.85rem; margin-top:0.25rem;">
                    Currently: <strong>{{ timeClockSettings.timezone || 'America/New_York' }}</strong>
                  </div>
                </div>
                <div>
                  <Button
                    label="Save Time Clock Settings"
                    icon="pi pi-save"
                    @click="saveTimeClockSettings"
                    :loading="timeClockSettingsSaving"
                    data-testid="timeclock-settings-save"
                  />
                </div>
              </div>
            </template>
          </Card>

          <Card style="margin-top:1rem">
            <template #title>Shop hours (dispatch capacity)</template>
            <template #content>
              <p class="muted" style="margin-top:0">
                Default working hours for every tech. Used by the Dispatch board
                to show how full each tech's day is. Individual techs can
                override these on their user profile (blank = inherit).
              </p>
              <div class="form-grid" style="display:flex; flex-direction:column; gap:0.75rem; max-width:520px;">
                <div style="display:flex; align-items:center; gap:1rem;">
                  <label style="min-width:140px;">Shift start</label>
                  <InputText
                    v-model="shopHours.default_shift_start"
                    type="time"
                    style="max-width:140px;"
                    data-testid="shop-hours-start"
                  />
                </div>
                <div style="display:flex; align-items:center; gap:1rem;">
                  <label style="min-width:140px;">Shift end</label>
                  <InputText
                    v-model="shopHours.default_shift_end"
                    type="time"
                    style="max-width:140px;"
                    data-testid="shop-hours-end"
                  />
                </div>
                <div style="display:flex; align-items:flex-start; gap:1rem;">
                  <label style="min-width:140px; margin-top:0.4rem;">Working days</label>
                  <div style="display:flex; gap:0.4rem; flex-wrap:wrap;">
                    <button
                      v-for="day in WORKDAY_BITS"
                      :key="day.bit"
                      type="button"
                      class="p-button p-component"
                      :class="isWorkday(day.bit) ? 'p-button-primary' : 'p-button-secondary p-button-outlined'"
                      style="padding:0.35rem 0.7rem; min-width:54px;"
                      :data-testid="`shop-hours-day-${day.label.toLowerCase()}`"
                      @click="toggleWorkdayBit(day.bit)"
                    >{{ day.label }}</button>
                  </div>
                </div>
                <div>
                  <Button
                    label="Save Shop Hours"
                    icon="pi pi-save"
                    :loading="shopHoursSaving"
                    data-testid="shop-hours-save"
                    @click="saveShopHours"
                  />
                </div>
              </div>
            </template>
          </Card>

          <Card style="margin-top:1rem">
            <template #title>Dispatch</template>
            <template #content>
              <p class="muted" style="margin-top:0">
                When a job is scheduled without a tech assigned, decide how
                the system should react. All three default off.
              </p>
              <div class="form-grid" style="display:flex; flex-direction:column; gap:0.75rem;">
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="dispatchSettings.dispatch_warn_save_no_tech" data-testid="ds-warn" />
                  <div>
                    <strong>Warn before saving a scheduled job with no tech</strong>
                    <div class="muted">Shows a confirm dialog. User can still proceed.</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="dispatchSettings.dispatch_block_save_no_tech" data-testid="ds-block" />
                  <div>
                    <strong>Block saving a scheduled job with no tech</strong>
                    <div class="muted">Server returns 422; the Save button is disabled until a tech is picked. Overrides the warn setting.</div>
                  </div>
                </div>
                <div style="display:flex; align-items:center; gap:0.75rem;">
                  <ToggleSwitch v-model="dispatchSettings.dispatch_show_unassigned_lane" data-testid="ds-lane" />
                  <div>
                    <strong>Show "Scheduled — Not Assigned" lane on Dispatch</strong>
                    <div class="muted">A red lane next to the holding areas listing every upcoming scheduled job that still needs a tech (skips today — those are in the per-day Unassigned column).</div>
                  </div>
                </div>
                <div>
                  <Button label="Save Dispatch Settings" icon="pi pi-save" @click="saveDispatchSettings" :loading="dispatchSettingsSaving" data-testid="dispatch-settings-save" />
                </div>
              </div>
            </template>
          </Card>
        </TabPanel>

        <!-- Margin Tiers Tab — 2026-04-30. Pricing engine per-category/class
             tier editor + customer loyalty discount config. Standalone
             /margin-tiers route still works; this is the same panel. -->
        <TabPanel value="margin-tiers">
          <Card>
            <template #content>
              <MarginTiersPanel />
            </template>
          </Card>
        </TabPanel>

        <!-- Billing Terms Tab — 2026-04-29 / UX audit F-36. Per-class
             payment-terms defaults + early-pay discount + late-fee +
             interest configuration. The daily late-fee/interest task
             that ACTS on these settings is a follow-up sprint; the
             config columns + due-date computation land today. -->
        <TabPanel value="billing">
          <Card>
            <template #title>Billing Terms</template>
            <template #content>
              <p class="muted" style="margin-top:0">
                When a new invoice is created, due date = invoice date + payment-terms days.
                Customer-level override beats class default beats tenant default.
              </p>
              <h3>Payment-terms days</h3>
              <div class="form-grid" style="display:grid; grid-template-columns:repeat(2,minmax(200px,1fr)); gap:0.75rem;">
                <div class="form-field">
                  <label>Default (Net days)</label>
                  <InputNumber v-model="billingTerms.default_payment_terms_days" :min="0" :max="365" :useGrouping="false" />
                </div>
                <div class="form-field">
                  <label>Contractor (override)</label>
                  <InputNumber v-model="billingTerms.contractor_payment_terms_days" :min="0" :max="365" :useGrouping="false" placeholder="—" />
                </div>
                <div class="form-field">
                  <label>Retail (override)</label>
                  <InputNumber v-model="billingTerms.retail_payment_terms_days" :min="0" :max="365" :useGrouping="false" placeholder="—" />
                </div>
                <div class="form-field">
                  <label>Wholesale (override)</label>
                  <InputNumber v-model="billingTerms.wholesale_payment_terms_days" :min="0" :max="365" :useGrouping="false" placeholder="—" />
                </div>
              </div>
              <Divider />
              <h3>Early-pay discount</h3>
              <p class="muted" style="margin-top:0">
                If the customer pays within X days, take Y% off the balance.
              </p>
              <div class="form-grid" style="display:grid; grid-template-columns:repeat(2,minmax(200px,1fr)); gap:0.75rem;">
                <div class="form-field">
                  <label>Discount %</label>
                  <InputNumber v-model="billingTermsPct.early_pay_discount_pct" :min="0" :max="100" :minFractionDigits="2" :maxFractionDigits="4" suffix="%" />
                </div>
                <div class="form-field">
                  <label>Within (days)</label>
                  <InputNumber v-model="billingTerms.early_pay_discount_days" :min="0" :max="365" :useGrouping="false" placeholder="—" />
                </div>
              </div>
              <Divider />
              <h3>Late fee</h3>
              <p class="muted" style="margin-top:0">
                Applied N days after the due date. Use either flat amount or percentage (or both).
              </p>
              <div class="form-grid" style="display:grid; grid-template-columns:repeat(3,minmax(180px,1fr)); gap:0.75rem;">
                <div class="form-field">
                  <label>Flat fee ($)</label>
                  <InputNumber v-model="billingTerms.late_fee_flat_amount" :min="0" mode="currency" currency="USD" />
                </div>
                <div class="form-field">
                  <label>Percent of balance</label>
                  <InputNumber v-model="billingTermsPct.late_fee_pct" :min="0" :max="100" :minFractionDigits="2" :maxFractionDigits="4" suffix="%" />
                </div>
                <div class="form-field">
                  <label>Grace days after due</label>
                  <InputNumber v-model="billingTerms.late_fee_grace_days" :min="0" :max="365" :useGrouping="false" />
                </div>
              </div>
              <Divider />
              <h3>Interest on overdue balance</h3>
              <p class="muted" style="margin-top:0">
                Monthly rate, applied N days after the due date.
              </p>
              <div class="form-grid" style="display:grid; grid-template-columns:repeat(2,minmax(200px,1fr)); gap:0.75rem;">
                <div class="form-field">
                  <label>Monthly rate</label>
                  <InputNumber v-model="billingTermsPct.interest_rate_monthly_pct" :min="0" :max="100" :minFractionDigits="2" :maxFractionDigits="4" suffix="%" />
                </div>
                <div class="form-field">
                  <label>Grace days after due</label>
                  <InputNumber v-model="billingTerms.interest_grace_days" :min="0" :max="365" :useGrouping="false" />
                </div>
              </div>
              <div style="margin-top:1rem">
                <Button label="Save Billing Terms" icon="pi pi-save" @click="saveBillingTerms" :loading="billingTermsSaving" data-testid="billing-terms-save" />
              </div>
            </template>
          </Card>
        </TabPanel>

        <!-- Job Workflow Tab — 2026-04-29 / UX audit F-8. Default Start Job
             behavior is hard-coded (stamp started_at + auto-assign). These
             toggles light up the optional behaviors per tenant. -->

        <!-- Job Numbers Tab — 2026-04-29 / UX audit F-11. Tenant picks a format
             template and a starting sequence. Counter + yearly reset live in
             the control plane (tenant_settings). Future: estimates / invoices
             share the same `numbering` module. -->
        </TabPanels>
      </Tabs>

      <Dialog
        v-model:visible="showQBSyncDialog"
        :header="qbSyncHeader"
        :modal="true"
        :closable="!qbSyncRunning"
        :close-on-escape="!qbSyncRunning"
        :style="{ width: '540px' }"
        data-testid="qb-sync-progress-dialog"
      >
        <div class="qb-sync-progress">
          <p v-if="qbSyncRunning" class="qb-sync-hint">
            Syncing with QuickBooks. This can take up to a minute depending on how much data you have.
          </p>
          <p v-else-if="qbSyncOverall === 'done'" class="qb-sync-hint success">
            All entities synced successfully.
          </p>
          <p v-else-if="qbSyncOverall === 'partial'" class="qb-sync-hint warn">
            Sync finished with some issues. Review the details below.
          </p>
          <ul class="qb-sync-steps">
            <li v-for="step in qbSyncSteps" :key="step.key" :class="['qb-sync-step', 'status-' + step.status]" :data-testid="`qb-sync-step-${step.key}`">
              <span class="qb-sync-icon">
                <i v-if="step.status === 'pending'" class="pi pi-clock" />
                <i v-else-if="step.status === 'syncing'" class="pi pi-spin pi-spinner" />
                <i v-else-if="step.status === 'done'" class="pi pi-check-circle" />
                <i v-else class="pi pi-exclamation-circle" />
              </span>
              <span class="qb-sync-label">{{ step.label }}</span>
              <span class="qb-sync-message">{{ step.message || qbSyncStepMessage(step.status) }}</span>
            </li>
          </ul>
          <div v-if="qbSyncHasErrors" class="qb-sync-error-list">
            <h4>Issues</h4>
            <ul>
              <li v-for="(e, i) in qbSyncAllErrors" :key="i">
                <code>{{ e.qb_id || '?' }}</code> — {{ e.error || 'unknown' }}
              </li>
            </ul>
          </div>
        </div>
        <template #footer>
          <Button label="Close" severity="secondary" :disabled="qbSyncRunning" @click="showQBSyncDialog = false" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { useThemeStore } from "../stores/theme";
import AIAssistantIntegrationCard from "../components/AIAssistantIntegrationCard.vue";
import GoogleMapsIntegrationCard from "../components/GoogleMapsIntegrationCard.vue";
import PhoneComIntegrationCard from "../components/PhoneComIntegrationCard.vue";
import OutlookIntegrationCard from "../components/OutlookIntegrationCard.vue";
import OutlookConnectButton from "../components/OutlookConnectButton.vue";
import MarginTiersPanel from "../components/MarginTiersPanel.vue";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import { getIdleTimeoutMin, setIdleTimeoutMin } from "../composables/useIdleLogout";
import { useQBSync } from "../composables/useQBSync";
import { useTenantModules } from "../composables/useTenantModules";
import { formatDateTime } from "../composables/useFormatters";
import Badge from "primevue/badge";
import Button from "primevue/button";
import Card from "primevue/card";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Divider from "primevue/divider";
import Select from "primevue/select";
import ToggleSwitch from "primevue/toggleswitch";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import Textarea from "primevue/textarea";
import ProgressSpinner from "primevue/progressspinner";
import Tab from "primevue/tab";
import TabList from "primevue/tablist";
import TabPanel from "primevue/tabpanel";
import TabPanels from "primevue/tabpanels";
import Tabs from "primevue/tabs";
import Tag from "primevue/tag";
import Toolbar from "primevue/toolbar";
import Password from "primevue/password";
import { useToast } from "primevue/usetoast";
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApi();

// QB sync progress (per-entity visible feedback) — same composable as QuickbooksView
const {
  steps: qbSyncSteps,
  running: qbSyncRunning,
  overallStatus: qbSyncOverall,
  start: startQBSync,
} = useQBSync(api);
const showQBSyncDialog = ref(false);

const qbSyncHeader = computed(() => {
  if (qbSyncRunning.value) return "QuickBooks sync in progress";
  if (qbSyncOverall.value === "done") return "QuickBooks sync complete";
  if (qbSyncOverall.value === "partial") return "QuickBooks sync finished with issues";
  return "QuickBooks sync";
});

const qbSyncStepMessage = (status) => {
  if (status === "pending") return "Waiting";
  if (status === "syncing") return "Fetching from QuickBooks...";
  if (status === "done") return "Up to date";
  if (status === "error") return "Failed";
  return "";
};

const qbSyncAllErrors = computed(() => qbSyncSteps.flatMap((s) => s.errors || []));
const qbSyncHasErrors = computed(() => qbSyncAllErrors.value.length > 0);
const toast = useToast();
const theme = useThemeStore();
const activeTab = ref("branding");
const saveState = ref("");
const brandingSaving = ref(false);
const modulesBusy = ref(false);
const modulesLoading = ref(false);

// ── Branding ──
const branding = reactive({
  companyName: "",
  logo: null,
  primaryColor: "#0057a8",
  accentColor: "#f7b500",
});

async function loadBrandingForm() {
  try {
    const data = await api.get("/api/settings/branding");
    if (data && typeof data === "object") {
      if (typeof data.company_name === "string") branding.companyName = data.company_name;
      if (typeof data.primary_color === "string" && data.primary_color) branding.primaryColor = data.primary_color;
      if (typeof data.secondary_color === "string" && data.secondary_color) branding.accentColor = data.secondary_color;
    }
  } catch (_err) {
    // Non-fatal: form just shows defaults until user edits.
  }
}

function normalizeHex(color) {
  if (!color) return "#000000";
  return color.startsWith("#") ? color : `#${color}`;
}

function onLogoSelect(event) {
  branding.logo = event.target.files?.[0] || null;
}

function applyBrandingTheme() {
  const primary = normalizeHex(branding.primaryColor);
  const accent = normalizeHex(branding.accentColor);
  document.documentElement.style.setProperty("--primary", primary);
  document.documentElement.style.setProperty("--accent", accent);
  theme.branding = {
    ...theme.branding,
    company_name: branding.companyName,
    primary_color: primary,
    accent_color: accent,
  };
  theme.applyThemeVars();
}

async function uploadLogoIfPresent() {
  if (!branding.logo) return;
  const formData = new FormData();
  formData.append("logo", branding.logo);
  await api.request("/api/settings/branding/logo", {
    method: "POST",
    body: formData,
  });
}

async function saveBranding() {
  saveState.value = "Saving...";
  brandingSaving.value = true;
  try {
    await api.patch("/api/settings/branding", {
      company_name: branding.companyName,
      primary_color: normalizeHex(branding.primaryColor),
      secondary_color: normalizeHex(branding.accentColor),
    });
    await uploadLogoIfPresent();
    applyBrandingTheme();
    saveState.value = "Saved";
    toast.add({ severity: "success", summary: "Branding saved", life: 3000 });
  } catch (err) {
    saveState.value = "Error";
    toast.add({ severity: "error", summary: "Save failed", detail: err.message || "Could not save branding", life: 4000 });
  } finally {
    brandingSaving.value = false;
  }
}

// ── Modules ──
const modules = ref([]);
// Sidebar/nav share a session-cached copy of /api/settings/modules; grab the
// loader so Save can force-refresh it (see saveModuleChanges).
const { loadTenantModules } = useTenantModules();
// pendingModuleState[key] = boolean override of mod.enabled until Save/Revert.
// Keys absent here mean "no pending change for that module".
const pendingModuleState = ref({});
const tierLabels = { starter: "Starter", professional: "Professional", business: "Business" };

const modulesByTier = computed(() => {
  const groups = ["starter", "professional", "business"].map((tier) => ({
    tier,
    label: tierLabels[tier] || tier,
    items: modules.value.filter((item) => item.tier === tier),
  }));
  return groups.filter((group) => group.items.length > 0);
});

const dirtyModuleKeys = computed(() => Object.keys(pendingModuleState.value));

function pendingEnabled(key) {
  if (Object.prototype.hasOwnProperty.call(pendingModuleState.value, key)) {
    return pendingModuleState.value[key];
  }
  const mod = modules.value.find((m) => m.key === key);
  return mod ? mod.enabled : false;
}

function isModuleDirty(key) {
  return Object.prototype.hasOwnProperty.call(pendingModuleState.value, key);
}

function setPendingModule(mod, enabled) {
  if (mod.locked) return;
  // If the new value equals the server-persisted value, drop the pending entry
  // so the row stops showing "pending" and the Save button accurately reflects
  // outstanding work.
  const next = { ...pendingModuleState.value };
  if (mod.enabled === enabled) {
    delete next[mod.key];
  } else {
    next[mod.key] = enabled;
  }
  pendingModuleState.value = next;
}

function revertModuleChanges() {
  pendingModuleState.value = {};
}

async function loadModules() {
  modulesLoading.value = true;
  modulesBusy.value = true;
  try {
    const response = await api.get("/api/settings/modules");
    modules.value = Array.isArray(response.modules) ? response.modules : [];
  } catch {
    modules.value = [];
  } finally {
    modulesLoading.value = false;
    modulesBusy.value = false;
  }
}

async function saveModuleChanges() {
  const keys = Object.keys(pendingModuleState.value);
  if (keys.length === 0) return;
  modulesBusy.value = true;
  const failed = [];
  try {
    for (const key of keys) {
      const enabled = pendingModuleState.value[key];
      const path = enabled
        ? `/api/settings/modules/${key}/enable`
        : `/api/settings/modules/${key}/disable`;
      try {
        await api.post(path, {});
      } catch (err) {
        failed.push({ key, err });
      }
    }
    // Refresh from the server regardless — any successful change should
    // reflect, and failures should snap back to the server's truth. The
    // sidebar caches its module list once per session, so force that copy
    // to refetch too — otherwise a newly enabled module doesn't appear in
    // nav until a full page reload.
    await Promise.all([
      loadModules(),
      loadTenantModules({ force: true }),
    ]);
    pendingModuleState.value = {};
    if (failed.length === 0) {
      toast.add({ severity: "success", summary: "Modules saved", detail: `${keys.length} change${keys.length === 1 ? '' : 's'} applied`, life: 3000 });
    } else {
      toast.add({
        severity: failed.length === keys.length ? "error" : "warn",
        summary: failed.length === keys.length ? "Save failed" : "Modules — partial save",
        detail: `${keys.length - failed.length}/${keys.length} applied. Failed: ${failed.map((f) => f.key).join(", ")}`,
        life: 6000,
      });
    }
  } finally {
    modulesBusy.value = false;
  }
}

// ── Users ──
const users = ref([]);
const usersLoading = ref(false);
const userSearch = ref("");
const showInviteDialog = ref(false);
const inviteSaving = ref(false);
const inviteError = ref("");
const inviteForm = ref({ email: "", role: "Technician" });
const roleOptions = ["Admin", "Dispatcher", "Technician", "Sales"];

const filteredUsers = computed(() => {
  const q = userSearch.value.trim().toLowerCase();
  if (!q) return users.value;
  return users.value.filter(
    (u) =>
      (u.name || "").toLowerCase().includes(q) ||
      (u.email || "").toLowerCase().includes(q) ||
      (u.role || "").toLowerCase().includes(q)
  );
});

function roleSeverity(role) {
  const map = { Admin: "danger", Dispatcher: "info", Technician: "success", Sales: "warning" };
  return map[role] || "secondary";
}

async function loadUsers() {
  usersLoading.value = true;
  try {
    const result = await api.get("/api/users");
    users.value = Array.isArray(result) ? result : result?.items || result?.data || [];
  } catch (err) {
    users.value = [];
    console.error("Failed to load users:", err?.message || err);
  } finally {
    usersLoading.value = false;
  }
}

async function submitInvite() {
  inviteError.value = "";
  if (!inviteForm.value.email.trim()) {
    inviteError.value = "Email is required.";
    return;
  }
  inviteSaving.value = true;
  try {
    await api.post("/api/admin/users/invite", {
      email: inviteForm.value.email.trim(),
      role: inviteForm.value.role,
    });
    showInviteDialog.value = false;
    inviteForm.value = { email: "", role: "Technician" };
    await loadUsers();
  } catch (err) {
    inviteError.value = err.message || "Failed to send invite.";
  } finally {
    inviteSaving.value = false;
  }
}

// ── Integrations ──
const integrations = reactive({
  quickbooks: { connected: false, lastSync: null },
  stripe: { connected: false, mode: null },
  sms: { connected: false, provider: null },
  // #57 — operator toggles (bool map from /api/settings/integrations).
  boolMap: {},
  catalogSyncEnabled: false,
  catalogSyncSaving: false,
});

async function loadIntegrations() {
  try {
    const result = await api.get("/api/settings/integrations");
    if (result?.quickbooks) integrations.quickbooks = result.quickbooks;
    if (result?.stripe) integrations.stripe = result.stripe;
    if (result?.sms) integrations.sms = result.sms;
    integrations.boolMap = result?.integrations || {};
    integrations.catalogSyncEnabled = Boolean(result?.integrations?.quickbooks_catalog_sync);
  } catch {
    // Integrations endpoint may not exist yet; leave defaults
  }
  // Also check QB connection status from the QB-specific endpoint
  try {
    const qbStatus = await api.get("/api/qb/status");
    if (qbStatus?.connected) {
      integrations.quickbooks = {
        connected: true,
        lastSync: qbStatus.last_sync_at ? formatDateTime(qbStatus.last_sync_at) : null,
        // S122-13: surface refresh-token health so the UI can prompt the
        // user to reconnect instead of letting sync silently fail.
        needsReconnect: Boolean(qbStatus.needs_reconnect),
        authState: qbStatus.auth_state || "healthy",
      };
    }
  } catch {
    // QB module may not be enabled
  }
}

async function connectIntegration(provider) {
  if (provider === "quickbooks") {
    try {
      const result = await api.post("/api/qb/connect");
      if (result?.redirect_url) {
        const popup = window.open(result.redirect_url, "_blank", "width=600,height=700");
        if (!popup || popup.closed || typeof popup.closed === "undefined") {
          toast.add({ severity: "warn", summary: "Popup blocked", detail: "Redirecting in this window instead...", life: 3000 });
          window.location.href = result.redirect_url;
        }
      }
    } catch (err) {
      toast.add({ severity: "error", summary: "Connection failed", detail: err.message || "Could not connect to QuickBooks", life: 4000 });
    }
    return;
  }
  const oauthUrls = {
    stripe: "/api/stripe-connect/onboard",
    sms: "/api/settings/sms/configure",
  };
  const url = oauthUrls[provider] || `/api/settings/integrations/${provider}/connect`;
  window.open(url, "_blank", "width=600,height=700");
}

async function disconnectIntegration(provider) {
  if (!(await confirmAsync({ header: 'Confirm', message: `Disconnect ${provider}? This will stop syncing.` }))) return;
  try {
    const endpoint = provider === "quickbooks" ? "/api/qb/disconnect" : `/api/settings/integrations/${provider}/disconnect`;
    await api.post(endpoint);
    integrations[provider] = { connected: false };
    await loadIntegrations();
  } catch (err) {
    toast.add({ severity: "error", summary: "Disconnect failed", detail: err.message || "Failed to disconnect.", life: 4000 });
  }
}

async function syncNow(provider) {
  if (provider === "quickbooks") {
    // Use the per-entity progress dialog so the user sees what's happening.
    // Composable does the 4 calls in sequence; dialog renders live state.
    showQBSyncDialog.value = true;
    await startQBSync();
    await loadIntegrations();
    return;
  }
  try {
    const endpoint = `/api/settings/integrations/${provider}/sync`;
    const result = await api.post(endpoint);
    const detail = result?.created != null ? `Created: ${result.created}, Updated: ${result.updated}` : "Sync complete";
    toast.add({ severity: "success", summary: "Sync complete", detail, life: 4000 });
    await loadIntegrations();
  } catch (err) {
    toast.add({ severity: "error", summary: "Sync failed", detail: err.message || "Could not sync.", life: 4000 });
  }
}

// #57 — enable/disable QB catalog pull/push. Defaults off; gates the
// /api/catalogs/{id}/sync/qb/* endpoints server-side.
async function toggleCatalogSync(value) {
  integrations.catalogSyncSaving = true;
  try {
    await api.patch("/api/settings", {
      integrations: { ...integrations.boolMap, quickbooks_catalog_sync: value },
    });
    await loadIntegrations();
    toast.add({
      severity: "success",
      summary: value ? "QB catalog sync enabled" : "QB catalog sync disabled",
      life: 3000,
    });
  } catch (err) {
    integrations.catalogSyncEnabled = !value; // revert optimistic flip
    toast.add({ severity: "error", summary: "Could not update", detail: err.message || "", life: 4000 });
  } finally {
    integrations.catalogSyncSaving = false;
  }
}

// ── Email Settings ──
const emailConfig = ref({
  provider: "disabled", smtp_host: "", smtp_port: 587,
  username: "", password: "", from_email: "", from_name: "", is_verified: false,
});
const emailProviders = [
  { label: "Microsoft 365 / Outlook", value: "microsoft365" },
  { label: "Gmail / Google Workspace", value: "gmail" },
  { label: "SendGrid", value: "sendgrid" },
  { label: "Custom SMTP", value: "smtp" },
  { label: "Disabled", value: "disabled" },
];
const providerDefaults = { microsoft365: { host: "smtp.office365.com", port: 587 }, gmail: { host: "smtp.gmail.com", port: 587 }, sendgrid: { host: "smtp.sendgrid.net", port: 587 } };

async function loadEmailConfig() {
  try {
    const data = await api.get("/api/settings/email");
    if (data && typeof data === 'object') {
      emailConfig.value = { ...emailConfig.value, ...data };
    }
  } catch (e) {
    console.error("Failed to load email config:", e);
  }
}
function onProviderChange() {
  const d = providerDefaults[emailConfig.value.provider];
  if (d) { emailConfig.value.smtp_host = d.host; emailConfig.value.smtp_port = d.port; }
}
async function saveEmailConfig() {
  try {
    await api.put("/api/settings/email", emailConfig.value);
    toast.add({ severity: "success", summary: "Email settings saved", life: 3000 });
  } catch (e) { toast.add({ severity: "error", summary: "Failed to save", detail: e.message, life: 4000 }); }
}
async function testEmailConfig() {
  try {
    await api.post("/api/settings/email/test", {});
    toast.add({ severity: "success", summary: "Test email sent!", detail: "Check your inbox", life: 4000 });
    await loadEmailConfig();
  } catch (e) { toast.add({ severity: "error", summary: "Test failed", detail: e.message, life: 4000 }); }
}

// ── QB OAuth popup listener ──
// The OAuth callback (gdx/modules/quickbooks/router.py) returns an HTML page
// that postMessages the parent window when authorization completes.
// We listen for it here and refresh the integrations panel.
function onOAuthMessage(event) {
  if (event.origin !== window.location.origin) return;
  const data = event.data;
  if (!data || data.type !== "qb_oauth_result") return;
  if (data.status === "connected") {
    toast.add({ severity: "success", summary: "QuickBooks connected", detail: "Connection established.", life: 3000 });
    loadIntegrations();
  } else {
    toast.add({ severity: "error", summary: "QuickBooks connection failed", detail: "Please try again.", life: 4000 });
  }
}

// Warn before unload if module changes are pending — avoids losing
// in-progress toggles when the user closes the tab or navigates away.
function onBeforeUnload(event) {
  if (dirtyModuleKeys.value.length > 0) {
    event.preventDefault();
    event.returnValue = "";
  }
}

// ── Tax (per-tenant default rate) ──
const taxConfig = reactive({
  default_rate_pct: 0,
  tax_labor: false,
  name: "Default",
  description: "",
  configured_at: null,
});
const taxSaving = ref(false);

async function loadTaxConfig() {
  try {
    const cfg = await api.get("/api/tax/config");
    taxConfig.default_rate_pct = (Number(cfg.default_rate) || 0) * 100;
    taxConfig.tax_labor = Boolean(cfg.tax_labor);
    taxConfig.name = cfg.name || "Default";
    taxConfig.description = cfg.description || "";
    taxConfig.configured_at = cfg.configured_at;
  } catch (_e) {
    // tax module may not be present yet — leave defaults
  }
}

async function saveTaxConfig() {
  taxSaving.value = true;
  try {
    const cfg = await api.patch("/api/tax/config", {
      default_rate: (Number(taxConfig.default_rate_pct) || 0) / 100,
      tax_labor: Boolean(taxConfig.tax_labor),
      name: taxConfig.name,
      description: taxConfig.description,
    }, { successMessage: "Tax settings saved" });
    if (cfg) {
      taxConfig.default_rate_pct = (Number(cfg.default_rate) || 0) * 100;
      taxConfig.tax_labor = Boolean(cfg.tax_labor);
      taxConfig.configured_at = cfg.configured_at;
    }
  } finally {
    taxSaving.value = false;
  }
}

// ── Catalog policy (description rules, F-74) ──
const catalogPolicy = reactive({
  catalog_require_description: false,
  catalog_render_name_when_desc_empty: true,
  catalog_ai_suggest_descriptions: false,
  catalog_block_zero_price_on_invoice: false,
  catalog_warn_zero_price_on_invoice: true,
  catalog_block_zero_price_on_save: false,
  catalog_auto_inactivate_zero_price: false,
});
const showItemsNeedingPricing = ref(false);
const itemsNeedingPricing = ref([]);
async function loadItemsNeedingPricing() {
  try {
    const r = await api.get("/api/catalogs/items-needing-pricing?page_size=500");
    itemsNeedingPricing.value = r?.items || [];
    showItemsNeedingPricing.value = true;
  } catch (_e) { /* show modal anyway with empty list */ }
}
const catalogPolicySaving = ref(false);
async function loadCatalogPolicy() {
  try {
    const p = await api.get("/api/catalog-policy");
    if (p) Object.assign(catalogPolicy, p);
  } catch (_e) { /* not deployed */ }
}
async function saveCatalogPolicy() {
  catalogPolicySaving.value = true;
  try {
    const p = await api.patch("/api/catalog-policy", { ...catalogPolicy }, {
      successMessage: "Catalog settings saved",
    });
    if (p) Object.assign(catalogPolicy, p);
  } finally {
    catalogPolicySaving.value = false;
  }
}

// ── Estimates feature toggles (2026-04-30) ──
const estimatesFeatures = reactive({
  estimates_allow_line_margin_override: true,
  estimates_default_terms: "",
  estimate_email_subject_template: "",
  estimate_email_body_template: "",
  estimate_deposit_pct: 50,
  estimates_hide_line_prices: false,
});
const emailSubjectPlaceholder = "{{job_title}}";
const emailBodyPlaceholder = "Hi {{customer_name}},\n\nPlease see the attached estimate for {{job_title}}.\n\nReply with any questions, or to move forward.\n\nThanks,\n{{company_name}}";
const estimatesFeaturesSaving = ref(false);

// Inactivity auto-logout — tenant-wide, stored on TenantSettings via
// /api/session-policy. localStorage is the local cache that drives the timer
// (useIdleLogout, mounted in App.vue).
const idleTimeoutMin = ref(getIdleTimeoutMin());
const idleTimeoutSaving = ref(false);
async function loadIdleTimeout() {
  try {
    const data = await api.get("/api/session-policy");
    if (data && typeof data.idle_timeout_minutes === "number") {
      idleTimeoutMin.value = data.idle_timeout_minutes;
      setIdleTimeoutMin(data.idle_timeout_minutes); // refresh local cache
    }
  } catch {
    /* fall back to the cached localStorage value already in the ref */
  }
}
async function saveIdleTimeout() {
  idleTimeoutSaving.value = true;
  try {
    const data = await api.patch(
      "/api/session-policy",
      { idle_timeout_minutes: idleTimeoutMin.value },
      {
        successMessage: idleTimeoutMin.value > 0
          ? `Auto-logout after ${idleTimeoutMin.value} min of inactivity (tenant-wide).`
          : "Auto-logout disabled (tenant-wide).",
      },
    );
    const v = data?.idle_timeout_minutes ?? idleTimeoutMin.value;
    idleTimeoutMin.value = v;
    setIdleTimeoutMin(v); // update this device immediately
  } finally {
    idleTimeoutSaving.value = false;
  }
}

// ── Debug logging (operator toggle; surfaces swallowed server errors) ──
const debugLoggingEnabled = ref(false);
const debugLoggingSaving = ref(false);
async function loadDebugLogging() {
  try {
    const s = await api.get("/api/settings");
    debugLoggingEnabled.value = !!s?.debug_logging_enabled;
  } catch (_e) { /* leave default off */ }
}
async function saveDebugLogging() {
  debugLoggingSaving.value = true;
  try {
    const s = await api.patch(
      "/api/settings",
      { debug_logging_enabled: debugLoggingEnabled.value },
      { successMessage: debugLoggingEnabled.value
          ? "Debug logging on — handled server errors now appear in Server Errors."
          : "Debug logging off." },
    );
    if (s && typeof s.debug_logging_enabled === "boolean") debugLoggingEnabled.value = s.debug_logging_enabled;
  } finally {
    debugLoggingSaving.value = false;
  }
}

async function loadEstimatesFeatures() {
  try {
    const p = await api.get("/api/estimates-features");
    if (p) Object.assign(estimatesFeatures, p);
  } catch (_e) { /* not deployed */ }
}
async function saveEstimatesFeatures() {
  estimatesFeaturesSaving.value = true;
  try {
    const p = await api.patch("/api/estimates-features", { ...estimatesFeatures }, {
      successMessage: "Estimate settings saved",
    });
    if (p) Object.assign(estimatesFeatures, p);
  } finally {
    estimatesFeaturesSaving.value = false;
  }
}

// ── Billing Terms (per-tenant payment-terms + fees, F-36) ──
const billingTerms = reactive({
  default_payment_terms_days: 30,
  contractor_payment_terms_days: null,
  retail_payment_terms_days: null,
  wholesale_payment_terms_days: null,
  early_pay_discount_percent: null,
  early_pay_discount_days: null,
  late_fee_flat_amount: null,
  late_fee_percent: null,
  late_fee_grace_days: 0,
  interest_rate_monthly_percent: null,
  interest_grace_days: 0,
});
// UI works in percent (7.38) but the API stores fractions (0.0738).
const billingTermsPct = reactive({
  early_pay_discount_pct: null,
  late_fee_pct: null,
  interest_rate_monthly_pct: null,
});
const billingTermsSaving = ref(false);

function _toFraction(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n / 100 : null;
}
function _toPercent(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n * 100 : null;
}

async function loadBillingTerms() {
  try {
    const t = await api.get("/api/billing/terms");
    if (t) {
      Object.assign(billingTerms, t);
      billingTermsPct.early_pay_discount_pct = _toPercent(t.early_pay_discount_percent);
      billingTermsPct.late_fee_pct = _toPercent(t.late_fee_percent);
      billingTermsPct.interest_rate_monthly_pct = _toPercent(t.interest_rate_monthly_percent);
    }
  } catch (_e) { /* module not deployed yet */ }
}

async function saveBillingTerms() {
  billingTermsSaving.value = true;
  try {
    const payload = {
      ...billingTerms,
      early_pay_discount_percent: _toFraction(billingTermsPct.early_pay_discount_pct),
      late_fee_percent: _toFraction(billingTermsPct.late_fee_pct),
      interest_rate_monthly_percent: _toFraction(billingTermsPct.interest_rate_monthly_pct),
    };
    const t = await api.patch("/api/billing/terms", payload, {
      successMessage: "Billing terms saved",
    });
    if (t) {
      Object.assign(billingTerms, t);
      billingTermsPct.early_pay_discount_pct = _toPercent(t.early_pay_discount_percent);
      billingTermsPct.late_fee_pct = _toPercent(t.late_fee_percent);
      billingTermsPct.interest_rate_monthly_pct = _toPercent(t.interest_rate_monthly_percent);
    }
  } finally {
    billingTermsSaving.value = false;
  }
}

// ── Job Workflow (per-tenant Start Job / Complete Job toggles, F-8) ──
const workflowFlags = reactive({
  lock_schedule_on_start: false,
  post_arrival_event: false,
  sms_arrival_notify: false,
  require_parts_on_complete: false,
  require_hours_on_complete: false,
  require_signature_on_complete: false,
  require_invoice_on_complete: false,
});
const workflowSaving = ref(false);

async function loadWorkflowFlags() {
  try {
    const f = await api.get("/api/workflow/flags");
    if (f) Object.assign(workflowFlags, f);
  } catch (_e) {
    // module not deployed yet — defaults stay false
  }
}

async function saveWorkflowFlags() {
  workflowSaving.value = true;
  try {
    const f = await api.patch("/api/workflow/flags", { ...workflowFlags }, {
      successMessage: "Workflow settings saved",
    });
    if (f) Object.assign(workflowFlags, f);
  } finally {
    workflowSaving.value = false;
  }
}

// ── Dispatch settings (2026-05-01) — scheduled-no-tech gates + lane ──
const dispatchSettings = reactive({
  dispatch_warn_save_no_tech: false,
  dispatch_block_save_no_tech: false,
  dispatch_show_unassigned_lane: false,
});
const dispatchSettingsSaving = ref(false);

// ── Shop hours (Sprint dispatch-capacity 2026-05-20) — tenant default
// shift used by the dispatch board to compute per-tech daily capacity.
// Per-user overrides land on the user-edit drawer; NULL there inherits
// these defaults. workdays = Mon=1, Tue=2 ... Sun=64 bitmask (31 = M-F).
const WORKDAY_BITS = [
  { label: "Mon", bit: 1 },
  { label: "Tue", bit: 2 },
  { label: "Wed", bit: 4 },
  { label: "Thu", bit: 8 },
  { label: "Fri", bit: 16 },
  { label: "Sat", bit: 32 },
  { label: "Sun", bit: 64 },
];
const shopHours = reactive({
  default_shift_start: "08:00",
  default_shift_end: "17:00",
  default_workdays: 31,
});
const shopHoursSaving = ref(false);

function toggleWorkdayBit(bit) {
  shopHours.default_workdays = (shopHours.default_workdays || 0) ^ bit;
  if (shopHours.default_workdays < 1) shopHours.default_workdays = 1;
}
function isWorkday(bit) {
  return ((shopHours.default_workdays || 0) & bit) === bit;
}

async function loadShopHours() {
  try {
    const s = await api.get("/api/settings");
    if (s?.default_shift_start) shopHours.default_shift_start = s.default_shift_start;
    if (s?.default_shift_end) shopHours.default_shift_end = s.default_shift_end;
    if (Number.isInteger(s?.default_workdays)) shopHours.default_workdays = s.default_workdays;
  } catch (_e) {
    // leave defaults
  }
}

async function saveShopHours() {
  if (shopHours.default_shift_end <= shopHours.default_shift_start) {
    toast.add({
      severity: "error",
      summary: "Shift end must be after shift start",
      life: 4000,
    });
    return;
  }
  shopHoursSaving.value = true;
  try {
    const s = await api.patch(
      "/api/settings",
      {
        default_shift_start: shopHours.default_shift_start,
        default_shift_end: shopHours.default_shift_end,
        default_workdays: shopHours.default_workdays,
      },
      { successMessage: "Shop hours saved" },
    );
    if (s?.default_shift_start) shopHours.default_shift_start = s.default_shift_start;
    if (s?.default_shift_end) shopHours.default_shift_end = s.default_shift_end;
    if (Number.isInteger(s?.default_workdays)) shopHours.default_workdays = s.default_workdays;
  } finally {
    shopHoursSaving.value = false;
  }
}

// ── Time Clock (S92) — tenant-local timezone for clock display ──
const TIMEZONE_OPTIONS = [
  { label: "Eastern (America/New_York)",  value: "America/New_York" },
  { label: "Central (America/Chicago)",   value: "America/Chicago" },
  { label: "Mountain (America/Denver)",   value: "America/Denver" },
  { label: "Mountain — no DST (America/Phoenix)", value: "America/Phoenix" },
  { label: "Pacific (America/Los_Angeles)", value: "America/Los_Angeles" },
  { label: "Alaska (America/Anchorage)",  value: "America/Anchorage" },
  { label: "Hawaii (Pacific/Honolulu)",   value: "Pacific/Honolulu" },
  { label: "UTC",                         value: "UTC" },
];
const timeClockSettings = reactive({ timezone: "America/New_York" });
const timeClockSettingsSaving = ref(false);

async function loadTimeClockSettings() {
  try {
    const s = await api.get("/api/settings");
    if (s?.timezone) timeClockSettings.timezone = s.timezone;
  } catch (_e) {
    // leave default
  }
}

async function saveTimeClockSettings() {
  timeClockSettingsSaving.value = true;
  try {
    const s = await api.patch(
      "/api/settings",
      { timezone: timeClockSettings.timezone },
      { successMessage: "Time clock settings saved" },
    );
    if (s?.timezone) timeClockSettings.timezone = s.timezone;
  } finally {
    timeClockSettingsSaving.value = false;
  }
}

async function loadDispatchSettings() {
  try {
    const f = await api.get("/api/dispatch-settings");
    if (f) Object.assign(dispatchSettings, f);
  } catch (_e) {
    // module not deployed yet — defaults stay false
  }
}

async function saveDispatchSettings() {
  dispatchSettingsSaving.value = true;
  try {
    const f = await api.patch("/api/dispatch-settings", { ...dispatchSettings }, {
      successMessage: "Dispatch settings saved",
    });
    if (f) Object.assign(dispatchSettings, f);
  } finally {
    dispatchSettingsSaving.value = false;
  }
}

// ── Job Numbering (per-tenant format + sequence) ──
const numberingPresets = [
  { label: "JOB-001 (sequence only)",                   value: "JOB-{seq:003}" },
  { label: "JOB-2026-001 (year + sequence)",            value: "JOB-{year}-{seq:003}" },
  { label: "JOB-26-001 (2-digit year + sequence)",      value: "JOB-{yy}-{seq:003}" },
  { label: "{customer_initials}-2026-001 (initials)",   value: "{customer_initials}-{year}-{seq:003}" },
  { label: "Custom — edit the template field",          value: "__custom__" },
];

const numberingForm = reactive({
  preset: "JOB-{year}-{seq:003}",
  format: "JOB-{year}-{seq:003}",
  next_seq: 1,
});
const numberingSaving = ref(false);

const numberingPreview = computed(() => {
  const tpl = numberingForm.format || "";
  const seq = Number(numberingForm.next_seq) || 1;
  const now = new Date();
  const year = now.getFullYear();
  const yy = String(year % 100).padStart(2, "0");
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const initials = "BM"; // sample
  // Mirror Python's "{seq:003}" zero-pad (seq:NNN means width N).
  return tpl.replace(/\{(\w+)(?::(0?\d+))?\}/g, (_m, name, pad) => {
    let v = "";
    if (name === "seq") v = String(seq);
    else if (name === "year") v = String(year);
    else if (name === "yy") v = yy;
    else if (name === "month") v = month;
    else if (name === "customer_initials") v = initials;
    else return _m;
    if (pad) {
      const width = parseInt(pad, 10);
      v = String(v).padStart(width, "0");
    }
    return v;
  });
});

function onNumberingPresetChange() {
  if (numberingForm.preset && numberingForm.preset !== "__custom__") {
    numberingForm.format = numberingForm.preset;
  }
}

async function loadNumbering() {
  try {
    const cfg = await api.get("/api/numbering/config");
    if (cfg) {
      numberingForm.format = cfg.job_number_format || "JOB-{year}-{seq:003}";
      numberingForm.next_seq = Number(cfg.job_number_next_seq) || 1;
      const matched = numberingPresets.find((p) => p.value === numberingForm.format);
      numberingForm.preset = matched ? matched.value : "__custom__";
    }
  } catch (_e) {
    // module may not be deployed yet — leave defaults
  }
}

async function saveNumbering() {
  numberingSaving.value = true;
  try {
    const cfg = await api.patch("/api/numbering/config", {
      job_number_format: numberingForm.format,
      job_number_next_seq: Number(numberingForm.next_seq) || 1,
    }, { successMessage: "Numbering settings saved" });
    if (cfg) {
      numberingForm.format = cfg.job_number_format;
      numberingForm.next_seq = Number(cfg.job_number_next_seq) || 1;
    }
  } finally {
    numberingSaving.value = false;
  }
}

function formatDate(value) {
  // PG timestamptz serializes space-separated; normalize before parsing.
  return formatDateTime(typeof value === "string" ? value.replace(" ", "T") : value);
}

// ── Init ──
onMounted(async () => {
  window.addEventListener("message", onOAuthMessage);
  window.addEventListener("beforeunload", onBeforeUnload);
  await Promise.allSettled([loadBrandingForm(), loadModules(), loadUsers(), loadIntegrations(), loadEmailConfig(), loadTaxConfig(), loadNumbering(), loadWorkflowFlags(), loadBillingTerms(), loadCatalogPolicy(), loadEstimatesFeatures(), loadDispatchSettings(), loadTimeClockSettings(), loadShopHours(), loadIdleTimeout(), loadDebugLogging()]);
});

onBeforeUnmount(() => {
  window.removeEventListener("message", onOAuthMessage);
  window.removeEventListener("beforeunload", onBeforeUnload);
});
</script>

<style scoped>
.branding-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(220px, 1fr));
  gap: 0.75rem;
  margin-bottom: 1rem;
}

.form-field {
  display: grid;
  gap: 0.25rem;
}

.form-label-text {
  font-weight: 500;
}

/* Native <select> styled to roughly match PrimeVue inputs so the
   Provider field doesn't visually stand out from its siblings. */
.native-select {
  padding: 0.5rem 2rem 0.5rem 0.75rem;
  border: 1px solid var(--p-inputtext-border-color, #d1d5db);
  border-radius: 6px;
  background: var(--p-inputtext-background, #fff);
  color: var(--p-inputtext-color, #0f172a);
  font-size: 1rem;
  font-family: inherit;
  appearance: none;
  -webkit-appearance: none;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23666' stroke-width='2'><polyline points='6 9 12 15 18 9'/></svg>");
  background-repeat: no-repeat;
  background-position: right 0.75rem center;
  cursor: pointer;
}

.native-select:focus {
  outline: 2px solid var(--p-primary-color, #3b82f6);
  outline-offset: 2px;
}

.color-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.color-row input[type="color"] {
  width: 40px;
  height: 36px;
  border: 1px solid var(--border, #ccc);
  border-radius: 6px;
  padding: 2px;
  cursor: pointer;
}

.color-hex-input {
  width: 100px;
}

.branding-preview {
  margin-bottom: 1rem;
}

.preview-chip {
  padding: 0.75rem;
  border-radius: 8px;
  width: fit-content;
  font-weight: 600;
}

.hint {
  color: var(--muted, #888);
  font-size: 0.8rem;
}

.modules-groups {
  display: grid;
  gap: 1rem;
}

.tier-title {
  margin: 0 0 0.5rem;
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.module-list {
  display: grid;
  gap: 0.25rem;
}

.module-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  padding: 0.5rem 0.75rem;
  border-radius: 6px;
  transition: background 0.15s;
}

.module-row:hover {
  background: rgba(255, 255, 255, 0.03);
}

.module-info {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.module-name {
  font-weight: 600;
}

.module-desc {
  color: var(--muted, #888);
  font-size: 0.8rem;
}

.module-actions {
  flex-shrink: 0;
}

.module-row--dirty {
  background: rgba(255, 193, 7, 0.08);
  border-left: 3px solid #ffc107;
  padding-left: calc(0.75rem - 3px);
}

.module-pending {
  color: #ffc107;
  font-size: 0.75rem;
  font-style: italic;
}

.modules-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 1rem;
  padding-top: 0.75rem;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}

.modules-dirty-hint {
  margin: 0 auto 0 0;
  color: #ffc107;
  font-size: 0.85rem;
}

.integrations-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 1rem;
  align-items: start;
}

.integration-shell {
  background: var(--surface-panel);
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.integration-shell-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
}

.integration-shell-header h3 {
  margin: 0 0 0.25rem 0;
  font-size: 1rem;
  font-weight: 600;
}

.integration-shell .muted {
  color: var(--text-muted);
  font-size: 0.875rem;
  margin: 0;
}

.integration-shell .integration-actions {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.integration-header {
  font-size: 1rem;
}

.integration-status {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 0.5rem;
}

.user-name {
  font-weight: 600;
}

.dialog-form {
  display: grid;
  gap: 0.75rem;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 0.5rem;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  margin: 2rem 0;
}

.inline-error {
  color: #b42318;
  margin: 0.5rem 0;
}

.inline-success {
  color: #027a48;
  margin: 0.5rem 0;
}

.inline-info {
  color: var(--muted, #888);
  margin: 0.5rem 0;
}

.muted {
  color: var(--muted, #888);
}

@media (max-width: 768px) {
  .branding-grid {
    grid-template-columns: 1fr;
  }

  .integrations-grid {
    grid-template-columns: 1fr;
  }
}

/* QB sync progress dialog — uses PrimeVue theme vars so contrast is high
   AND dark mode auto-adapts. */
.qb-sync-hint {
  margin: 0 0 1rem;
  color: var(--p-text-color, #0f172a);
  font-weight: 500;
}
.qb-sync-hint.success { color: var(--p-green-700, #15803d); font-weight: 600; }
.qb-sync-hint.warn { color: var(--p-amber-700, #b45309); font-weight: 600; }
.qb-sync-steps { list-style: none; padding: 0; margin: 0; }
.qb-sync-step {
  display: grid;
  grid-template-columns: 32px 180px 1fr;
  gap: 0.75rem;
  align-items: center;
  padding: 0.7rem 0.5rem;
  border-bottom: 1px solid var(--p-content-border-color, #cbd5e1);
}
.qb-sync-step:last-child { border-bottom: none; }
.qb-sync-icon { font-size: 1.35rem; text-align: center; }
.qb-sync-step.status-pending .qb-sync-icon { color: var(--p-text-muted-color, #64748b); }
.qb-sync-step.status-syncing .qb-sync-icon { color: var(--p-blue-600, #2563eb); }
.qb-sync-step.status-done .qb-sync-icon { color: var(--p-green-600, #16a34a); }
.qb-sync-step.status-error .qb-sync-icon { color: var(--p-red-600, #dc2626); }
.qb-sync-label {
  font-weight: 600;
  color: var(--p-text-color, #0f172a);
  font-size: 1rem;
}
.qb-sync-message {
  color: var(--p-text-color, #1e293b);
  font-size: 0.95em;
  font-weight: 500;
}
.qb-sync-error-list {
  margin-top: 1rem; padding: 0.75rem 1rem;
  background: var(--p-red-50, #fef2f2);
  border: 1px solid var(--p-red-300, #fca5a5);
  border-radius: 6px;
}
.qb-sync-error-list h4 {
  margin: 0 0 0.5rem;
  color: var(--p-red-800, #7f1d1d);
  font-weight: 700;
}
.qb-sync-error-list ul {
  margin: 0; padding-left: 1.25rem;
  color: var(--p-red-900, #450a0a);
  font-size: 0.92em;
  font-weight: 500;
}
.qb-sync-error-list code {
  background: var(--p-red-100, #fee2e2);
  color: var(--p-red-900, #450a0a);
  padding: 0 0.35rem;
  border-radius: 3px;
  font-size: 0.88em;
  font-weight: 600;
}
</style>
