<template>
    <section v-if="loading" class="view-card spinner-wrap" data-testid="customer-detail-loading">
      <ProgressSpinner />
    </section>
    <section v-else-if="error" class="view-card">
      <p class="inline-error" data-testid="customer-detail-error">{{ error }}</p>
      <Button label="Back to Customers" icon="pi pi-arrow-left" text @click="$router.push('/customers')" />
    </section>
    <section v-else class="view-card customer-detail">
      <!-- Header with customer info and actions -->
      <div class="detail-header">
        <div class="header-left">
          <Button v-tooltip="'Back to customers'" icon="pi pi-arrow-left" aria-label="Back to customers" text rounded @click="$router.push('/customers')" data-testid="back-btn" />
          <h2>{{ customer.name }}</h2>
          <Tag :value="customer.customer_type || 'Residential'" :severity="customer.customer_type === 'Commercial' ? 'warning' : 'info'" />
        </div>
        <div class="header-actions">
          <Button label="Edit" icon="pi pi-pencil" aria-label="Edit" outlined data-testid="edit-customer-btn" @click="openEditDialog" />
          <Button label="+ New Job" icon="pi pi-briefcase" data-testid="new-job-for-customer-btn" @click="$router.push({ path: '/jobs', query: { new: '1', customer_id: String(customer.id) } })" />
          <Button label="+ New Estimate" icon="pi pi-file" data-testid="new-estimate-for-customer-btn" severity="secondary" @click="$router.push({ path: '/estimates/new', query: { customer_id: String(customer.id) } })" />
        </div>
      </div>

      <!-- Info card -->
      <Card class="info-card" data-testid="customer-info-card">
        <template #content>
          <div class="info-grid">
            <div class="info-item" data-testid="customer-phone">
              <i class="pi pi-phone"></i>
              <a v-if="customer.phone" :href="'tel:' + customer.phone">{{ customer.phone }}</a>
              <a v-else href="#" class="muted add-link" @click.prevent="openEditDialog">+ Add phone</a>
            </div>
            <div class="info-item" data-testid="customer-email">
              <i class="pi pi-envelope"></i>
              <a v-if="customer.email" :href="'mailto:' + customer.email">{{ customer.email }}</a>
              <a v-else href="#" class="muted add-link" @click.prevent="openEditDialog">+ Add email</a>
            </div>
            <div class="info-item" data-testid="customer-address">
              <i class="pi pi-map-marker"></i>
              <a v-if="customer.address" :href="'https://maps.google.com/?q=' + encodeURIComponent(customer.address)" target="_blank">{{ customer.address }}</a>
              <a v-else href="#" class="muted add-link" @click.prevent="openEditDialog">+ Add address</a>
            </div>
          </div>
        </template>
      </Card>

      <!-- Loyalty / pricing tile (Sprint 1.0.6 — admin/owner only) -->
      <Card v-if="showLoyaltyTile" class="info-card loyalty-card" data-testid="customer-loyalty-card">
        <template #content>
          <div class="loyalty-row">
            <div class="loyalty-metric">
              <div class="loyalty-label">12-month paid volume</div>
              <div class="loyalty-value" data-testid="customer-rolling-volume">
                {{ formatCurrency(customer.cached_rolling_volume_paid_12mo) }}
              </div>
              <div v-if="customer.cached_rolling_volume_at" class="loyalty-meta">
                refreshed {{ formatDateTime(customer.cached_rolling_volume_at) }}
              </div>
              <div v-else class="loyalty-meta muted">never refreshed</div>
            </div>
            <div class="loyalty-metric">
              <div class="loyalty-label">Loyalty tier</div>
              <div v-if="loyaltyTier" class="loyalty-value tier-on" data-testid="customer-loyalty-tier">
                {{ (loyaltyTier.discount_pct * 100).toFixed(1) }}% off
              </div>
              <div v-else class="loyalty-value muted" data-testid="customer-loyalty-tier">
                no tier
              </div>
              <div v-if="loyaltyDisabledReason" class="loyalty-meta muted">
                {{ loyaltyDisabledReason }}
              </div>
            </div>
          </div>
        </template>
      </Card>

      <!-- Tabs: Jobs, Locations, Notes -->
      <div class="detail-tabs">
        <Button
          v-for="tab in tabs"
          :key="tab"
          :label="tab"
          :class="{ 'p-button-outlined': activeTab !== tab }"
          size="small"
          :data-testid="`tab-${tab.toLowerCase()}`"
          @click="activeTab = tab"
        />
      </div>

      <!-- Jobs tab -->
      <div v-if="activeTab === 'Jobs'" class="tab-content" data-testid="tab-jobs-content">
        <DataTable
      responsiveLayout="scroll"
          :value="customerJobs"
          :paginator="customerJobs.length > 10"
          :rows="10"
          stripedRows
          data-testid="customer-jobs-table"
          @row-click="goToJob($event.data)"
          class="clickable-table"
        >
          <template #empty>
            <div class="empty-message">No jobs for this customer.</div>
          </template>
          <Column field="job_number" header="Job #" style="width: 110px">
            <template #body="{ data }">
              <span class="job-link">{{ data.job_number || data.jobNumber || '--' }}</span>
            </template>
          </Column>
          <Column field="title" header="Title" />
          <Column field="status" header="Status" style="width: 130px">
            <template #body="{ data }">
              <Tag :value="data.lifecycle_stage || data.status || '--'" :severity="jobStatusSeverity(data.lifecycle_stage || data.status)" />
            </template>
          </Column>
          <Column field="priority" header="Priority" style="width: 110px" />
          <Column field="scheduled_at" header="Scheduled" style="width: 140px">
            <template #body="{ data }">
              {{ formatDate(data.scheduled_at) }}
            </template>
          </Column>
        </DataTable>
      </div>

      <!-- Estimates tab -->
      <div v-if="activeTab === 'Estimates'" class="tab-content" data-testid="tab-estimates-content">
        <DataTable
        class="clickable-rows"
      responsiveLayout="scroll" :value="customerEstimates" stripedRows @row-click="$router.push(`/estimates/${$event.data.id}`)" >
          <template #empty><div class="empty-message">No estimates for this customer.</div></template>
          <Column field="estimate_number" header="Number" />
          <Column field="status" header="Status">
            <template #body="{ data }"><Tag :value="data.status" /></template>
          </Column>
          <Column field="total" header="Total">
            <template #body="{ data }">${{ Number(data.total || 0).toFixed(2) }}</template>
          </Column>
          <Column field="valid_until" header="Valid Until">
            <template #body="{ data }">{{ formatDate(data.valid_until) }}</template>
          </Column>
        </DataTable>
      </div>

      <!-- Invoices tab -->
      <div v-if="activeTab === 'Invoices'" class="tab-content" data-testid="tab-invoices-content">
        <DataTable
        class="clickable-rows"
      responsiveLayout="scroll" :value="customerInvoices" stripedRows @row-click="$router.push(`/billing/${$event.data.id}`)" >
          <template #empty><div class="empty-message">No invoices for this customer.</div></template>
          <Column field="invoice_number" header="Number" />
          <Column field="status" header="Status">
            <template #body="{ data }"><Tag :value="data.status" /></template>
          </Column>
          <Column field="total" header="Total">
            <template #body="{ data }">${{ Number(data.total || 0).toFixed(2) }}</template>
          </Column>
          <Column field="due_date" header="Due">
            <template #body="{ data }">{{ formatDate(data.due_date) }}</template>
          </Column>
        </DataTable>
      </div>

      <!-- Locations tab -->
      <div v-if="activeTab === 'Locations'" class="tab-content" data-testid="tab-locations-content">
        <div class="locations-header">
          <h3>Service Locations</h3>
          <Button label="+ Add Location" icon="pi pi-plus" size="small" outlined data-testid="add-location-btn" @click="openLocationDialog" />
        </div>

        <div v-if="!locations.length" class="empty-message">No locations on file.</div>

        <div v-for="loc in locations" :key="loc.id" class="location-card" :data-testid="`location-${loc.id}`">
          <div class="location-info">
            <strong>{{ loc.label || loc.name || 'Service Address' }}</strong>
            <span>{{ loc.address || loc.street }}</span>
            <span v-if="loc.city || loc.state">{{ [loc.city, loc.state, loc.zip].filter(Boolean).join(', ') }}</span>
            <span v-if="loc.notes" class="muted">{{ loc.notes }}</span>
          </div>
          <div class="location-actions">
            <ToggleSwitch
              :model-value="loc.is_primary"
              onLabel="Primary"
              offLabel="Set Primary"
              :disabled="updatingPrimary === loc.id"
              :data-testid="`primary-toggle-${loc.id}`"
              @change="value => updatePrimaryAddress(loc, value)"
            />
            <Button v-tooltip="'Edit'" icon="pi pi-pencil" aria-label="Edit" text rounded size="small" @click="editLocation(loc)" :data-testid="`edit-location-${loc.id}`" />
          </div>
        </div>
      </div>

      <!-- Notes tab -->
      <div v-if="activeTab === 'Notes'" class="tab-content" data-testid="tab-notes-content">
        <p v-if="!customer.access_notes && !customer.notes" class="muted">No notes on file.</p>
        <p v-else>{{ customer.access_notes || customer.notes }}</p>
      </div>

      <!-- Equipment tab -->
      <div v-if="activeTab === 'Equipment'" class="tab-content" data-testid="tab-equipment-content">
        <div class="panel-header">
          <h3>Equipment</h3>
          <Button
            label="+ Add Equipment"
            icon="pi pi-plus"
            size="small"
            outlined
            data-testid="add-equipment-btn"
            @click="openEquipmentDialog"
          />
        </div>
        <Card class="section-card" data-testid="equipment-card">
          <template #content>
            <DataTable
      responsiveLayout="scroll" :value="equipment" responsive-layout="scroll" stripedRows data-testid="customer-equipment-table">
              <template #empty><div class="empty-message">No equipment recorded yet.</div></template>
              <Column field="brand" header="Brand" />
              <Column field="model" header="Model" />
              <Column field="serial_number" header="Serial #" />
              <Column header="Install Date">
                <template #body="{ data }">{{ formatDate(data.install_date) }}</template>
              </Column>
              <Column field="equipment_type" header="Type" />
            </DataTable>
          </template>
        </Card>
      </div>

      <!-- Recurring Jobs tab -->
      <div v-if="activeTab === 'Recurring Jobs'" class="tab-content" data-testid="tab-recurring-content">
        <div class="panel-header">
          <h3>Recurring Jobs</h3>
          <Button
            label="+ Add Recurring"
            icon="pi pi-plus"
            size="small"
            outlined
            data-testid="add-recurring-btn"
            @click="openRecurringDialog"
          />
        </div>
        <Card class="section-card" data-testid="recurring-card">
          <template #content>
            <DataTable
      responsiveLayout="scroll" :value="recurringJobs" responsive-layout="scroll" stripedRows data-testid="customer-recurring-table">
              <template #empty><div class="empty-message">No recurring jobs yet.</div></template>
              <Column field="title" header="Title" />
              <Column field="job_type" header="Job Type" />
              <Column field="interval_days" header="Interval (days)" />
              <Column header="Next Due Date">
                <template #body="{ data }">{{ formatDate(data.next_due_date) }}</template>
              </Column>
            </DataTable>
          </template>
        </Card>
      </div>

      <!-- Communications tab -->
      <div v-if="activeTab === 'Communications'" class="tab-content" data-testid="tab-communications-content">
        <div class="panel-header">
          <h3>Communication Log</h3>
          <Button
            label="+ Log Communication"
            icon="pi pi-comment"
            size="small"
            outlined
            data-testid="add-communication-btn"
            @click="openCommunicationDialog"
          />
        </div>
        <Card class="section-card" data-testid="communications-card">
          <template #content>
            <DataTable
      responsiveLayout="scroll" :value="communications" responsive-layout="scroll" stripedRows data-testid="customer-communications-table">
              <template #empty><div class="empty-message">No communications logged yet.</div></template>
              <Column header="Date">
                <template #body="{ data }">{{ formatDateTime(data.created_at || data.date) }}</template>
              </Column>
              <Column field="type" header="Type" />
              <Column field="direction" header="Direction" />
              <Column field="subject" header="Subject" />
              <Column header="Body">
                <template #body="{ data }">{{ (data.body || '').slice(0, 80) }}</template>
              </Column>
            </DataTable>
          </template>
        </Card>
      </div>

      <!-- Portal tab -->
      <div v-if="activeTab === 'Portal'" class="tab-content" data-testid="tab-portal-content">
        <Card class="section-card portal-card" data-testid="portal-card">
          <template #title>Customer Portal Account</template>
          <template #content>
            <div class="portal-status" data-testid="portal-status-line">
              <span v-if="portalStatus?.exists" class="portal-active">
                <i class="pi pi-check-circle" /> Active — {{ portalStatus.account?.email }}
                <small v-if="portalStatus.account?.last_login_at">
                  last login {{ formatDateTime(portalStatus.account.last_login_at) }}
                </small>
              </span>
              <span v-else class="muted">No portal account registered for this customer.</span>
            </div>
            <div class="portal-actions">
            <Button
              label="Manage Account"
              icon="pi pi-shield"
              data-testid="portal-manage-btn"
              :loading="isSavingPortal"
              :disabled="isSavingPortal"
              @click="openPortalDialog"
            />
              <Button
                label="Remove Portal Account"
                icon="pi pi-trash" aria-label="Delete"
                severity="danger"
                data-testid="portal-remove-btn"
                :disabled="!portalStatus?.exists || isRemovingPortal"
                @click="removePortalAccount"
              />
            </div>
          </template>
        </Card>
      </div>


      <!-- Edit Customer Dialog -->
      <Dialog
        v-model:visible="showEditDialog"
        header="Edit Customer"
        :style="{ width: '500px' }"
        modal
        data-testid="edit-customer-dialog"
      >
        <form class="dialog-form" @submit.prevent="saveCustomer">
          <div class="form-field">
            <label for="edit-name">Name *</label>
            <InputText id="edit-name" v-model="editForm.name" data-testid="edit-customer-name" class="w-full" />
          </div>
          <div class="form-row">
            <div class="form-field">
              <label for="edit-phone">Phone</label>
              <InputMask id="edit-phone" v-model="editForm.phone" mask="(999) 999-9999" data-testid="edit-customer-phone" class="w-full" />
            </div>
            <div class="form-field">
              <label for="edit-email">Email</label>
              <InputText id="edit-email" v-model="editForm.email" type="email" data-testid="edit-customer-email" class="w-full" />
            </div>
          </div>
          <div class="form-field">
            <label for="edit-address">Address</label>
            <Textarea id="edit-address" v-model="editForm.address" rows="2" data-testid="edit-customer-address" class="w-full" />
          </div>
          <div class="form-field">
            <label for="edit-access-notes">Access notes (gate codes, dogs, parking)</label>
            <Textarea
              id="edit-access-notes"
              v-model="editForm.access_notes"
              rows="3"
              data-testid="edit-customer-access-notes"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label for="edit-type">Customer Type</label>
            <Select id="edit-type" v-model="editForm.customer_type" :options="['Residential', 'Commercial']" data-testid="edit-customer-type" class="w-full" />
          </div>
          <!-- Sprint 1.0.5 — pricing engine inputs -->
          <div class="form-field">
            <label>Pricing Class</label>
            <div style="display: flex; gap: 12px; align-items: center;">
              <label v-for="opt in ['retail', 'contractor', 'wholesale']" :key="opt" style="display: inline-flex; gap: 4px; align-items: center;">
                <RadioButton v-model="editForm.pricing_class" :input-id="`pc-${opt}`" :value="opt" :data-testid="`pricing-class-${opt}`" />
                <span style="text-transform: capitalize;">{{ opt }}</span>
              </label>
            </div>
            <small style="color: var(--p-text-muted-color);">
              Drives default margin lookup. Wholesale/contractor use their own tier sets in pricing settings.
            </small>
          </div>
          <div class="form-field">
            <label for="edit-margin-override">Margin Override (optional)</label>
            <div style="display: flex; gap: 8px; align-items: center;">
              <InputNumber id="edit-margin-override" v-model="editForm.margin_override_pct"
                :min="0" :max="0.99" :max-fraction-digits="4"
                placeholder="e.g. 0.35 for 35%"
                data-testid="margin-override-input"
                style="flex: 1;" />
              <Button v-if="editForm.margin_override_pct != null" type="button" label="Clear" text data-testid="clear-margin-override" @click="editForm.margin_override_pct = null" />
            </div>
            <small style="color: var(--p-text-muted-color);">
              Beats the tier lookup for this customer. Decimal 0–0.99 (0.35 = 35% margin).
            </small>
          </div>
          <div v-if="editError" class="inline-error">{{ editError }}</div>
          <div class="form-actions">
            <Button type="button" label="Cancel" text @click="showEditDialog = false" />
            <Button type="submit" label="Save Changes" :loading="isSaving" data-testid="save-customer-btn" />
          </div>
        </form>
      </Dialog>

      <!-- Add/Edit Location Dialog -->
      <Dialog
        v-model:visible="showLocationDialog"
        :header="locationForm.id ? 'Edit Location' : 'Add Location'"
        :style="{ width: '450px' }"
        modal
        data-testid="location-dialog"
      >
        <form class="dialog-form" @submit.prevent="saveLocation">
          <div class="form-field">
            <label for="loc-label">Label</label>
            <InputText id="loc-label" v-model="locationForm.label" placeholder="e.g. Main Office, Warehouse" data-testid="location-label-input" class="w-full" />
          </div>
          <div class="form-field">
            <label for="loc-address">Address *</label>
            <InputText id="loc-address" v-model="locationForm.address" data-testid="location-address-input" class="w-full" />
          </div>
          <div class="form-row">
            <div class="form-field">
              <label for="loc-city">City</label>
              <InputText id="loc-city" v-model="locationForm.city" data-testid="location-city-input" class="w-full" />
            </div>
            <div class="form-field">
              <label for="loc-state">State</label>
              <InputText id="loc-state" v-model="locationForm.state" data-testid="location-state-input" class="w-full" />
            </div>
          </div>
          <div class="form-field">
            <label for="loc-zip">Zip</label>
            <InputText id="loc-zip" v-model="locationForm.zip" data-testid="location-zip-input" style="width: 150px" />
          </div>
          <div class="form-field">
            <label for="loc-notes">Access notes (gate codes, dogs, parking)</label>
            <Textarea id="loc-notes" v-model="locationForm.notes" rows="2" data-testid="location-notes-input" class="w-full" />
          </div>
          <div class="form-field toggle-row">
            <ToggleSwitch
              v-model="locationForm.is_primary"
              on-label="Primary"
              off-label="Secondary"
              data-testid="location-primary-toggle"
            />
            <span>Set as primary address</span>
          </div>
          <div v-if="locationError" class="inline-error">{{ locationError }}</div>
          <div class="form-actions">
            <Button type="button" label="Cancel" text @click="showLocationDialog = false" />
            <Button type="submit" label="Save Location" :loading="isSavingLocation" data-testid="save-location-btn" />
          </div>
        </form>
      </Dialog>

      <Dialog
        v-model:visible="showEquipmentDialog"
        header="Add Equipment"
        :style="{ width: '520px' }"
        modal
        data-testid="equipment-dialog"
      >
        <form class="dialog-form" @submit.prevent="saveEquipment">
          <div class="form-row">
            <div class="form-field">
              <label for="equipment-brand">Brand</label>
              <InputText id="equipment-brand" v-model="equipmentForm.brand" data-testid="equipment-brand-input" class="w-full" />
            </div>
            <div class="form-field">
              <label for="equipment-model">Model</label>
              <InputText id="equipment-model" v-model="equipmentForm.model" data-testid="equipment-model-input" class="w-full" />
            </div>
          </div>
          <div class="form-row">
            <div class="form-field">
              <label for="equipment-serial">Serial #</label>
              <InputText id="equipment-serial" v-model="equipmentForm.serial" data-testid="equipment-serial-input" class="w-full" />
            </div>
            <div class="form-field">
              <label for="equipment-type">Type</label>
              <Select id="equipment-type" v-model="equipmentForm.type" :options="equipmentTypes" data-testid="equipment-type-input" class="w-full" />
            </div>
          </div>
          <div class="form-row">
            <div class="form-field">
              <label for="equipment-install-date">Install Date</label>
              <DatePicker id="equipment-install-date" v-model="equipmentForm.install_date" data-testid="equipment-install-date-input" class="w-full" />
            </div>
            <div class="form-field">
              <label for="equipment-warranty-expires">Warranty Expires</label>
              <DatePicker id="equipment-warranty-expires" v-model="equipmentForm.warranty_expires" data-testid="equipment-warranty-expires-input" class="w-full" />
            </div>
          </div>
          <div class="form-field">
            <label for="equipment-notes">Notes</label>
            <Textarea id="equipment-notes" v-model="equipmentForm.notes" rows="2" data-testid="equipment-notes-input" class="w-full" />
          </div>
          <div class="form-actions">
            <Button type="button" label="Cancel" text @click="showEquipmentDialog = false" />
            <Button type="submit" label="Save Equipment" :loading="isSavingEquipment" data-testid="save-equipment-btn" />
          </div>
        </form>
      </Dialog>

      <Dialog
        v-model:visible="showRecurringDialog"
        header="Add Recurring Job"
        :style="{ width: '520px' }"
        modal
        data-testid="recurring-dialog"
      >
        <form class="dialog-form" @submit.prevent="saveRecurringJob">
          <div class="form-field">
            <label for="recurring-title">Title *</label>
            <InputText id="recurring-title" v-model="recurringForm.title" data-testid="recurring-title-input" class="w-full" />
          </div>
          <div class="form-row">
            <div class="form-field">
              <label for="recurring-job-type">Job Type</label>
              <Select id="recurring-job-type" v-model="recurringForm.job_type" :options="jobTypeOptions" data-testid="recurring-job-type-input" class="w-full" />
            </div>
            <div class="form-field">
              <label for="recurring-interval">Interval (days) *</label>
              <InputNumber id="recurring-interval" v-model="recurringForm.interval_days" data-testid="recurring-interval-input" class="w-full" :min="1" />
            </div>
          </div>
          <div class="form-field">
            <label for="recurring-next-due">Next Due Date *</label>
            <DatePicker id="recurring-next-due" v-model="recurringForm.next_due_date" data-testid="recurring-date-input" class="w-full" />
          </div>
          <div class="form-field">
            <label for="recurring-description">Description</label>
            <Textarea id="recurring-description" v-model="recurringForm.description" rows="3" data-testid="recurring-description-input" class="w-full" />
          </div>
          <div v-if="recurringError" class="inline-error">{{ recurringError }}</div>
          <div class="form-actions">
            <Button type="button" label="Cancel" text @click="showRecurringDialog = false" />
            <Button type="submit" label="Save" :loading="isSavingRecurring" data-testid="save-recurring-btn" />
          </div>
        </form>
      </Dialog>

      <Dialog
        v-model:visible="showCommunicationDialog"
        header="Log Communication"
        :style="{ width: '520px' }"
        modal
        data-testid="communication-dialog"
      >
        <form class="dialog-form" @submit.prevent="saveCommunication">
          <div class="form-row">
            <div class="form-field">
              <label for="communication-type">Type *</label>
              <Select id="communication-type" name="lcType" v-model="communicationForm.type" :options="communicationTypes" data-testid="communication-type-input" class="w-full" />
            </div>
            <div class="form-field">
              <label for="communication-direction">Direction</label>
              <Select id="communication-direction" name="lcDir" v-model="communicationForm.direction" :options="communicationDirections" data-testid="communication-direction-input" class="w-full" />
            </div>
          </div>
          <div class="form-field">
            <label for="communication-subject">Subject</label>
            <InputText id="communication-subject" name="lcSubject" v-model="communicationForm.subject" data-testid="communication-subject-input" class="w-full" />
          </div>
          <div class="form-field">
            <label for="communication-body">Body / Notes *</label>
            <Textarea id="communication-body" name="lcBody" v-model="communicationForm.body" rows="3" data-testid="communication-body-input" class="w-full" />
          </div>
          <div v-if="communicationError" class="inline-error">{{ communicationError }}</div>
          <div class="form-actions">
            <Button type="button" label="Cancel" text @click="showCommunicationDialog = false" />
            <Button type="submit" label="Save" :loading="isSavingCommunication" data-testid="save-communication-btn" />
          </div>
        </form>
      </Dialog>

      <Dialog
        v-model:visible="showPortalDialog"
        header="Customer Portal Account"
        :style="{ width: '420px' }"
        modal
        close-icon="pi pi-times" aria-label="Remove"
        @hide="portalFormError = ''"
        data-testid="portal-dialog"
      >
        <form class="dialog-form" @submit.prevent="savePortalAccount">
          <p class="muted" style="font-size: 0.9rem; margin: 0;">
            {{ portalStatus?.exists
              ? 'Update the portal account credentials and the customer will receive a notice.'
              : 'Create a portal login for this customer. They will receive an email with their credentials.' }}
          </p>
          <div class="form-field">
            <label for="portal-email">Email *</label>
            <InputText
              id="portal-email"
              v-model="portalForm.email"
              type="email"
              class="w-full"
              autofocus
              data-testid="portal-dialog-email"
            />
          </div>
          <div class="form-field">
            <label for="portal-password">Password *</label>
            <InputText
              id="portal-password"
              v-model="portalForm.password"
              type="password"
              class="w-full"
              data-testid="portal-dialog-password"
            />
          </div>
          <div v-if="portalFormError" class="inline-error">{{ portalFormError }}</div>
          <div class="form-actions">
            <Button type="button" label="Cancel" text @click="closePortalDialog" />
            <Button
              type="submit"
              label="Create / Update"
              :loading="isSavingPortal"
              data-testid="portal-dialog-save"
            />
          </div>
        </form>
      </Dialog>

      <Toast data-testid="customer-detail-toast" />
    </section>
</template>

<script setup>
import { computed, ref, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useToast } from "primevue/usetoast";
import { useApiWithToast } from "../composables/useApiWithToast";
import { useAuthStore } from "../stores/auth";
import Button from "primevue/button";
import Card from "primevue/card";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Select from "primevue/select";
import DatePicker from "primevue/datepicker";
import InputMask from "primevue/inputmask";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
import InputNumber from "primevue/inputnumber";
import RadioButton from "primevue/radiobutton";
import ToggleSwitch from "primevue/toggleswitch";
import Toast from "primevue/toast";

const route = useRoute();
const router = useRouter();
const api = useApiWithToast();
const toast = useToast();
const auth = useAuthStore();

const customer = ref({});
const customerJobs = ref([]);
const locations = ref([]);
const loading = ref(true);
const error = ref("");

// Sprint 1.0.6 — loyalty tile (admin/owner only)
const pricingSettings = ref(null);
const showLoyaltyTile = computed(() => {
  const role = (auth.user?.role || auth.user?.user_role || '').toLowerCase();
  return role === 'admin' || role === 'owner';
});

const loyaltyTier = computed(() => {
  const s = pricingSettings.value;
  const v = customer.value?.cached_rolling_volume_paid_12mo;
  if (!s || !s.volume_discount_enabled || v == null || v <= 0) return null;
  if (!Array.isArray(s.volume_tiers) || s.volume_tiers.length === 0) return null;
  // Per-class gate
  const cls = customer.value?.pricing_class || 'retail';
  const classMatch = (s.class_settings || []).find(c => c.pricing_class === cls);
  if (classMatch && !classMatch.rolling_volume_discount_enabled) return null;
  const matches = s.volume_tiers.filter(t =>
    v >= t.volume_min_12mo &&
    (t.volume_max_12mo == null || v < t.volume_max_12mo)
  );
  return matches.length === 1 ? matches[0] : null;
});

const loyaltyDisabledReason = computed(() => {
  const s = pricingSettings.value;
  if (!s) return '';
  if (!s.volume_discount_enabled) return 'master toggle off';
  const cls = customer.value?.pricing_class || 'retail';
  const classMatch = (s.class_settings || []).find(c => c.pricing_class === cls);
  if (classMatch && !classMatch.rolling_volume_discount_enabled) {
    return `${cls} class disabled`;
  }
  return '';
});

function formatCurrency(v) {
  if (v == null || isNaN(v)) return '$0.00';
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(v);
}
// formatDateTime — declared further down in the existing util block, reused here.
const activeTab = ref("Jobs");
const tabs = ["Jobs", "Estimates", "Invoices", "Locations", "Notes", "Equipment", "Recurring Jobs", "Communications", "Portal"];
const customerEstimates = ref([]);
const customerInvoices = ref([]);
const equipment = ref([]);
const showEquipmentDialog = ref(false);
const equipmentForm = ref({ brand: "", model: "", serial: "", install_date: null, warranty_expires: null, type: "", notes: "" });
const equipmentTypes = ["door", "opener", "motor", "remote", "other"];
const isSavingEquipment = ref(false);
const recurringJobs = ref([]);
const showRecurringDialog = ref(false);
const recurringForm = ref({ title: "", job_type: "", interval_days: 365, next_due_date: null, description: "" });
const jobTypeOptions = ["Service", "Installation", "New Construction", "Repair", "Maintenance", "Inspection", "Other"];
const recurringError = ref("");
const isSavingRecurring = ref(false);
const communications = ref([]);
const showCommunicationDialog = ref(false);
const communicationForm = ref({ type: "email", direction: "outbound", subject: "", body: "" });
const communicationTypes = ["email", "sms", "call", "note"];
const communicationDirections = ["inbound", "outbound"];
const communicationError = ref("");
const isSavingCommunication = ref(false);
const portalStatus = ref(null);
const showPortalDialog = ref(false);
const portalForm = ref({ email: '', password: '' });
const portalFormError = ref('');
const isSavingPortal = ref(false);
const isRemovingPortal = ref(false);
const updatingPrimary = ref(null);

async function loadCustomerEstimates() {
  try {
    const data = await api.get(`/api/estimates?customer_id=${customer.value.id}`);
    customerEstimates.value = Array.isArray(data) ? data : data?.items || data?.data || [];
  } catch {
    customerEstimates.value = [];
  }
}

async function loadCustomerInvoices() {
  try {
    const data = await api.get(`/api/invoices?customer_id=${customer.value.id}`);
    customerInvoices.value = Array.isArray(data) ? data : data?.items || data?.data || [];
  } catch {
    customerInvoices.value = [];
  }
}

// Edit customer state
const showEditDialog = ref(false);
const editForm = ref({});
const editError = ref("");
const isSaving = ref(false);

// Location dialog state
const showLocationDialog = ref(false);
const locationForm = ref(emptyLocation());
const locationError = ref("");
const isSavingLocation = ref(false);

function emptyLocation() {
  return { id: null, label: "", address: "", city: "", state: "", zip: "", notes: "", is_primary: false };
}

function formatDate(dateStr) {
  if (!dateStr) return "";
  try {
    return new Date(dateStr).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  } catch {
    return dateStr;
  }
}

function formatDateTime(value) {
  if (!value) return "";
  try {
    return new Date(value).toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "numeric",
      minute: "2-digit",
    });
  } catch {
    return value;
  }
}

function toDatePayload(value) {
  if (!value) return null;
  if (value instanceof Date) {
    return value.toISOString().split("T")[0];
  }
  return value;
}

function jobStatusSeverity(status) {
  const map = {
    Estimate: "info", Sold: "success", Scheduled: "warning",
    InProgress: "info", "In Progress": "info",
    Complete: "success", Invoiced: "secondary",
  };
  return map[status] || "secondary";
}

function goToJob(job) {
  router.push(`/jobs/${job.id}`);
}

function resetEquipmentForm() {
  equipmentForm.value = { brand: "", model: "", serial: "", install_date: null, warranty_expires: null, type: "", notes: "" };
}

function openEquipmentDialog() {
  resetEquipmentForm();
  showEquipmentDialog.value = true;
}

function openRecurringDialog() {
  recurringError.value = "";
  recurringForm.value = {
    title: "",
    job_type: "",
    interval_days: 365,
    next_due_date: new Date(),
    description: "",
  };
  showRecurringDialog.value = true;
}

function openCommunicationDialog() {
  communicationError.value = "";
  communicationForm.value = { type: "email", direction: "outbound", subject: "", body: "" };
  showCommunicationDialog.value = true;
}

async function fetchEquipment() {
  try {
    const data = await api.get(`/api/customers/${route.params.id}/equipment`);
    equipment.value = Array.isArray(data) ? data : data?.data || data?.items || [];
  } catch {
    equipment.value = [];
  }
}

async function saveEquipment() {
  isSavingEquipment.value = true;
  try {
    await api.post(`/api/customers/${route.params.id}/equipment`, {
      brand: equipmentForm.value.brand || null,
      model: equipmentForm.value.model || null,
      serial_number: equipmentForm.value.serial || null,
      install_date: toDatePayload(equipmentForm.value.install_date),
      equipment_type: equipmentForm.value.type || null,
      notes: equipmentForm.value.notes || null,
    });
    toast.add({ severity: "success", summary: "Saved", detail: "Equipment record added.", life: 3000 });
    showEquipmentDialog.value = false;
    await fetchEquipment();
  } catch {
    // errors surfaced by useApiWithToast
  } finally {
    isSavingEquipment.value = false;
  }
}

async function fetchRecurringJobs() {
  try {
    const data = await api.get(`/api/customers/${route.params.id}/recurring-jobs`);
    recurringJobs.value = Array.isArray(data) ? data : data?.data || data?.items || [];
  } catch {
    recurringJobs.value = [];
  }
}

async function saveRecurringJob() {
  recurringError.value = "";
  if (!recurringForm.value.title.trim()) {
    recurringError.value = "Title is required.";
    return;
  }
  if (!recurringForm.value.next_due_date) {
    recurringError.value = "Next due date is required.";
    return;
  }
  if (!recurringForm.value.interval_days || Number(recurringForm.value.interval_days) < 1) {
    recurringError.value = "Interval must be at least 1 day.";
    return;
  }

  isSavingRecurring.value = true;
  try {
    await api.post(`/api/customers/${route.params.id}/recurring-jobs`, {
      title: recurringForm.value.title.trim(),
      job_type: recurringForm.value.job_type || null,
      interval_days: Number(recurringForm.value.interval_days),
      next_due_date: toDatePayload(recurringForm.value.next_due_date),
      description: recurringForm.value.description || null,
    });
    toast.add({ severity: "success", summary: "Saved", detail: "Recurring job added.", life: 3000 });
    showRecurringDialog.value = false;
    await fetchRecurringJobs();
  } catch {
    // errors surfaced by useApiWithToast
  } finally {
    isSavingRecurring.value = false;
  }
}

async function fetchCommunications() {
  try {
    const data = await api.get(`/api/customers/${route.params.id}/communications`);
    if (Array.isArray(data)) {
      communications.value = data;
    } else if (data?.communications) {
      communications.value = data.communications;
    } else {
      communications.value = data?.data || data?.items || [];
    }
  } catch {
    communications.value = [];
  }
}

async function saveCommunication() {
  communicationError.value = "";
  if (!communicationForm.value.body.trim()) {
    communicationError.value = "Body is required.";
    return;
  }

  isSavingCommunication.value = true;
  try {
    await api.post(`/api/customers/${route.params.id}/communications`, {
      type: communicationForm.value.type,
      direction: communicationForm.value.direction,
      subject: communicationForm.value.subject?.trim() || null,
      body: communicationForm.value.body.trim(),
    });
    toast.add({ severity: "success", summary: "Saved", detail: "Communication logged.", life: 3000 });
    showCommunicationDialog.value = false;
    await fetchCommunications();
  } catch {
    // errors surfaced by useApiWithToast
  } finally {
    isSavingCommunication.value = false;
  }
}

async function fetchPortalStatus() {
  try {
    portalStatus.value = await api.get(`/api/customers/${route.params.id}/portal-account`);
  } catch {
    portalStatus.value = null;
  }
}

function openPortalDialog() {
  portalFormError.value = '';
  const email = portalStatus.value?.account?.email || customer.value.email || '';
  portalForm.value = { email, password: '' };
  showPortalDialog.value = true;
}

function closePortalDialog() {
  showPortalDialog.value = false;
}

async function savePortalAccount() {
  portalFormError.value = '';
  const email = (portalForm.value.email || '').trim();
  if (!email) {
    portalFormError.value = 'Email is required.';
    return;
  }
  const password = portalForm.value.password || '';
  if (password.length < 8) {
    portalFormError.value = 'Password must be at least 8 characters.';
    return;
  }

  isSavingPortal.value = true;
  try {
    await api.post(
      `/api/customers/${route.params.id}/portal-account`,
      { email, password },
      { successMessage: 'Portal account saved' },
    );
    closePortalDialog();
    await fetchPortalStatus();
  } catch (err) {
    portalFormError.value = err?.message || 'Failed to save portal account.';
  } finally {
    isSavingPortal.value = false;
  }
}

async function removePortalAccount() {
  if (!portalStatus.value?.exists) {
    return;
  }
  isRemovingPortal.value = true;
  try {
    await api.del(`/api/customers/${route.params.id}/portal-account`);
    toast.add({ severity: "success", summary: "Removed", detail: "Portal account deleted.", life: 4000 });
    await fetchPortalStatus();
  } catch {
    // handled
  } finally {
    isRemovingPortal.value = false;
  }
}

async function updatePrimaryAddress(loc, value) {
  updatingPrimary.value = loc.id;
  try {
    await api.patch(`/api/customers/${route.params.id}/locations/${loc.id}`, { is_primary: value });
    await fetchLocations();
  } catch {
    await fetchLocations();
  } finally {
    updatingPrimary.value = null;
  }
}

function openEditDialog() {
  editError.value = "";
  editForm.value = {
    name: customer.value.name || "",
    phone: customer.value.phone || "",
    email: customer.value.email || "",
    address: customer.value.address || "",
    customer_type: customer.value.customer_type || "Residential",
    pricing_class: customer.value.pricing_class || null,
    margin_override_pct: customer.value.margin_override_pct ?? null,
    access_notes: customer.value.access_notes || "",
  };
  showEditDialog.value = true;
}

async function saveCustomer() {
  editError.value = "";
  if (!editForm.value.name.trim()) {
    editError.value = "Name is required.";
    return;
  }
  isSaving.value = true;
  try {
    const patch = {
      name: editForm.value.name.trim(),
      phone: editForm.value.phone || "",
      email: editForm.value.email || "",
      address: editForm.value.address || "",
      customer_type: editForm.value.customer_type,
      access_notes: editForm.value.access_notes?.trim() || null,
    };
    if (editForm.value.pricing_class) patch.pricing_class = editForm.value.pricing_class;
    if (editForm.value.margin_override_pct == null) {
      // Sentinel — server PATCH can't otherwise distinguish "set to null" from "not set"
      patch.clear_margin_override = true;
    } else {
      patch.margin_override_pct = editForm.value.margin_override_pct;
    }
    await api.patch(`/api/customers/${route.params.id}`, patch);
    toast.add({ severity: "success", summary: "Saved", detail: "Customer updated.", life: 3000 });
    showEditDialog.value = false;
    await fetchCustomer();
  } catch (e) {
    editError.value = e?.message || "Failed to save.";
    toast.add({ severity: "error", summary: "Error", detail: editError.value, life: 5000 });
  } finally {
    isSaving.value = false;
  }
}

function openLocationDialog() {
  locationError.value = "";
  locationForm.value = emptyLocation();
  showLocationDialog.value = true;
}

function editLocation(loc) {
  locationError.value = "";
  locationForm.value = {
    id: loc.id,
    label: loc.label || loc.name || "",
    address: loc.address || loc.street || "",
    city: loc.city || "",
    state: loc.state || "",
    zip: loc.zip || "",
    notes: loc.notes || "",
    is_primary: Boolean(loc.is_primary),
  };
  showLocationDialog.value = true;
}

async function saveLocation() {
  locationError.value = "";
  if (!locationForm.value.address.trim()) {
    locationError.value = "Address is required.";
    return;
  }
  isSavingLocation.value = true;
  try {
    const payload = {
      label: locationForm.value.label || "",
      address: locationForm.value.address.trim(),
      city: locationForm.value.city || "",
      state: locationForm.value.state || "",
      zip: locationForm.value.zip || "",
      notes: locationForm.value.notes || "",
      is_primary: Boolean(locationForm.value.is_primary),
    };
    if (locationForm.value.id) {
      await api.patch(`/api/customers/${route.params.id}/locations/${locationForm.value.id}`, payload);
    } else {
      await api.post(`/api/customers/${route.params.id}/locations`, payload);
    }
    toast.add({ severity: "success", summary: "Saved", detail: "Location saved.", life: 3000 });
    showLocationDialog.value = false;
    await fetchLocations();
  } catch (e) {
    locationError.value = e?.message || "Failed to save location.";
    toast.add({ severity: "error", summary: "Error", detail: locationError.value, life: 5000 });
  } finally {
    isSavingLocation.value = false;
  }
}

async function fetchCustomer() {
  loading.value = true;
  try {
    const data = await api.get(`/api/customers/${route.params.id}`);
    customer.value = data?.data || data || {};
    // Extract jobs if embedded in response
    const rawJobs = customer.value.jobs || [];
    customerJobs.value = Array.isArray(rawJobs) ? rawJobs : [];
  } catch (e) {
    error.value = e?.message || "Failed to load customer.";
  } finally {
    loading.value = false;
  }
}

async function fetchLocations() {
  try {
    const result = await api.get(`/api/customers/${route.params.id}/locations`);
    locations.value = Array.isArray(result) ? result : result?.data || result?.items || [];
  } catch {
    // Locations endpoint may not exist yet -- degrade gracefully
    locations.value = [];
  }
}

async function loadPricingSettings() {
  // Admin/owner only — viewers without role won't render the tile, no need to fetch.
  if (!showLoyaltyTile.value) return;
  try {
    pricingSettings.value = await api.get('/api/pricing-engine/settings');
  } catch {
    // Silent — tile just shows "no tier" without pricing context if endpoint fails.
    pricingSettings.value = null;
  }
}

onMounted(async () => {
  await fetchCustomer();
  await Promise.all([
    fetchLocations(),
    loadCustomerEstimates(),
    loadCustomerInvoices(),
    fetchEquipment(),
    fetchRecurringJobs(),
    fetchCommunications(),
    fetchPortalStatus(),
    loadPricingSettings(),
  ]);
});
</script>

<style scoped>
.customer-detail {
  max-width: 1000px;
}

.detail-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 1rem;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.header-left h2 {
  margin: 0;
}

.header-actions {
  display: flex;
  gap: 8px;
}

.info-card {
  margin-bottom: 1rem;
}

.loyalty-card {
  border-left: 3px solid var(--p-primary-color);
}

.loyalty-row {
  display: flex;
  gap: 32px;
  flex-wrap: wrap;
}

.loyalty-metric {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.loyalty-label {
  font-size: 0.85em;
  color: var(--p-text-muted-color);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.loyalty-value {
  font-size: 1.4em;
  font-weight: 600;
}

.loyalty-value.tier-on {
  color: var(--p-green-600);
}

.loyalty-meta {
  font-size: 0.8em;
  color: var(--p-text-muted-color);
}

.info-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
}

.info-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  border: 1px solid var(--surface-border, #dee2e6);
  border-radius: 8px;
}

.info-item a {
  color: var(--p-primary-color);
  text-decoration: none;
}

.info-item a:hover {
  text-decoration: underline;
}

.detail-tabs {
  display: flex;
  gap: 0.5rem;
  margin: 1rem 0;
}

.tab-content {
  min-height: 150px;
}

.clickable-table {
  cursor: pointer;
}

.job-link {
  color: var(--p-primary-color);
  font-weight: 600;
}

.locations-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 1rem;
}

.locations-header h3 {
  margin: 0;
}

.location-card {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  padding: 12px 16px;
  border: 1px solid var(--surface-border, #dee2e6);
  border-radius: 8px;
  margin-bottom: 8px;
}

.location-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.location-actions {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 0.5rem;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 1rem;
  margin-bottom: 1rem;
}

.section-card {
  margin-bottom: 1rem;
}

.portal-card {
  max-width: 640px;
}

.portal-status {
  margin-bottom: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.95rem;
}

.portal-status small {
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
}

.portal-actions {
  display: flex;
  gap: 0.5rem;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  margin: 2rem 0;
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

.toggle-row {
  display: flex;
  align-items: center;
  gap: 0.65rem;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 0.75rem;
}

.inline-error {
  color: #b42318;
  margin: 0.5rem 0;
  font-size: 0.9rem;
}

.empty-message {
  text-align: center;
  padding: 2rem;
  color: var(--p-text-muted-color);
}

.muted {
  color: var(--p-text-muted-color);
}

.w-full {
  width: 100%;
}
</style>
