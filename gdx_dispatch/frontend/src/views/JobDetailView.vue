<template>
    <section v-if="loading" class="view-card job-detail"><ProgressSpinner /></section>
    <section v-else-if="error" class="view-card job-detail">
      <Button icon="pi pi-arrow-left" label="Back to Jobs" text size="small" @click="$router.push('/jobs')" />
      <p class="error" style="margin-top:1rem">{{ error }}</p>
    </section>
    <section v-else class="view-card job-detail">
      <div class="job-header">
        <div>
          <Button icon="pi pi-arrow-left" label="Back to Jobs" text size="small" @click="$router.push('/jobs')" />
          <h2 class="job-title">
            Job #{{ job.job_number || job.id?.toString().slice(0, 8) }}
            <span class="job-subtitle">{{ job.title || job.job_type || 'Service' }}</span>
          </h2>
          <div class="job-badges">
            <JobStateChip :job="job" data-testid="job-detail-status" />
            <Tag :value="job.priority || 'Normal'" severity="warn" />
            <Tag v-if="job.job_type" :value="job.job_type" severity="info" />
            <Tag
              v-if="job.is_callback"
              value="CALLBACK"
              severity="danger"
              data-testid="job-detail-callback"
              v-tooltip.bottom="`Return visit within ${job.callback_window_days || 90} days — different P&L treatment`"
            />
          </div>
        </div>
        <div class="header-actions">
          <Button label="Edit" icon="pi pi-pencil" aria-label="Edit" severity="secondary" @click="openEditDialog" />
          <Button v-if="job.status !== 'Complete' && job.status !== 'Invoiced'"
            label="Complete Job" icon="pi pi-check" severity="success"
            @click="completeJob" data-testid="job-detail-complete" />
          <Button v-if="job.status === 'Complete'"
            label="Create Invoice" icon="pi pi-dollar" severity="success"
            @click="createInvoice" data-testid="job-detail-create-invoice" />
          <!-- F-32 / 2026-04-29: when a job is completed or cancelled, the
               next action almost always means "warranty / callback" — but
               can also mean "un-complete by mistake" or "other reason".
               JobStateOverrideDialog handles all three with a required
               reason on the non-warranty paths. -->
          <Button v-if="job.lifecycle_stage === 'completed' || job.lifecycle_stage === 'cancelled' || job.status === 'Complete' || job.status === 'Cancelled'"
            label="Re-open / Warranty" icon="pi pi-refresh" severity="warn"
            @click="showStateOverride = true" data-testid="job-detail-reopen" />
          <Button label="Create Estimate" icon="pi pi-file-edit" severity="info"
            @click="createEstimate" data-testid="job-detail-create-estimate" />
          <Button label="Install Sheet" icon="pi pi-print" severity="secondary"
            @click="openInstallSheet" data-testid="job-detail-install-sheet" />
        </div>
      </div>

      <div class="stage-strip">
        <Button v-for="stage in stageButtons" :key="stage"
          :label="stage"
          :severity="stageSeverity(stage)"
          :rounded="true"
          :outlined="stage !== job.status"
          class="stage-btn"
          :data-testid="`job-detail-stage-${stage.toLowerCase().replace(/\s+/g, '-')}`"
          @click="applyStage(stage)" />
        <span class="stage-divider"></span>
        <Button label="Schedule/Reschedule" icon="pi pi-calendar" severity="info"
          @click="openSchedule(job.id)" data-testid="job-detail-schedule" />
      </div>

      <Tabs v-model:value="activeTab" class="job-tabs">
        <TabList>
          <Tab value="details">Details</Tab>
          <Tab value="schedule">Schedule</Tab>
          <Tab value="diagnosis">Diagnosis</Tab>
          <Tab value="hazards">Hazards</Tab>
          <Tab value="receipts">Receipts</Tab>
          <Tab value="notes">Notes</Tab>
          <Tab value="photos">Photos</Tab>
          <Tab value="signature">Signature</Tab>
          <Tab value="costing">Costing</Tab>
          <Tab value="install">Install Specs</Tab>
          <Tab value="activity">Activity</Tab>
        </TabList>
      </Tabs>

      <div v-if="activeTab === 'details'" class="tab-panel">
        <div class="details-grid">
          <div class="card">
            <div class="card-header">
              <h3>Job Details</h3>
            </div>
            <div class="detail-row">
              <span>Type</span>
              <strong>{{ job.job_type || 'Service' }}</strong>
            </div>
            <div class="detail-row">
              <span>Lifecycle Stage</span>
              <strong>{{ job.lifecycle_stage || job.status || 'Unknown' }}</strong>
            </div>
            <div class="detail-row">
              <span>Scheduled</span>
              <strong>{{ job.scheduled_at ? formatDate(job.scheduled_at) : 'Not scheduled' }}</strong>
            </div>
            <div class="detail-row">
              <span>Priority</span>
              <Select
                v-model="selectedPriority"
                :options="priorityOptions"
                optionLabel="label"
                optionValue="value"
                placeholder="Priority"
                class="w-full"
                :disabled="!patchable"
                @change="updatePriority"
                data-testid="job-detail-priority"
              />
            </div>
            <div class="detail-row">
              <span>Technician</span>
              <Select
                v-model="selectedTech"
                :options="techOptions"
                optionLabel="label"
                optionValue="value"
                placeholder="Assign technician"
                class="w-full"
                :disabled="!patchable"
                filter
                show-clear
                @change="assignTech"
                data-testid="job-detail-tech"
              />
            </div>
            <!-- S97 slice 6 — multi-tech assignments (desktop dispatch). Calls
                 the existing /api/jobs/{id}/assignments + /api/jobs/{id}/lead.
                 The single-Select above stays as a quick "primary tech" knob;
                 this widget is the truth for assignments. -->
            <div class="detail-row" data-testid="job-detail-assignments">
              <span>Assigned Crew</span>
              <div class="assignments-block">
                <div v-if="assignmentsLoading" class="muted">Loading…</div>
                <div v-else-if="!assignments.length" class="muted">No additional techs assigned yet.</div>
                <div v-else class="assignment-chips">
                  <span
                    v-for="a in assignments"
                    :key="a.id"
                    class="assignment-chip"
                    :class="{ 'is-lead': a.is_lead }"
                    :data-testid="`assignment-${a.tech_id}`"
                  >
                    <i v-if="a.is_lead" class="pi pi-star-fill" title="Lead tech" />
                    <span class="assignment-name">{{ techLabel(a.tech_id) }}</span>
                    <Button v-if="!a.is_lead && patchable"
                      v-tooltip="'Make lead'"
                      icon="pi pi-star" text size="small"
                      :aria-label="`Make ${techLabel(a.tech_id)} the lead tech`"
                      :data-testid="`assignment-make-lead-${a.tech_id}`"
                      @click="setLead(a.tech_id)" />
                    <Button v-if="patchable"
                      v-tooltip="`Remove ${techLabel(a.tech_id)}`"
                      icon="pi pi-times" text size="small" severity="danger"
                      :aria-label="`Remove ${techLabel(a.tech_id)} from this job`"
                      :data-testid="`assignment-remove-${a.id}`"
                      @click="removeAssignment(a.id)" />
                  </span>
                </div>
                <div v-if="patchable" class="assignment-add-row">
                  <Select
                    v-model="addAssignmentTechId"
                    :options="unassignedTechOptions"
                    optionLabel="label"
                    optionValue="value"
                    placeholder="Add another tech…"
                    class="w-full"
                    filter
                    :disabled="!unassignedTechOptions.length"
                    data-testid="assignment-add-select"
                  />
                  <Button
                    label="Add"
                    icon="pi pi-plus"
                    size="small"
                    :disabled="!addAssignmentTechId"
                    :loading="addingAssignment"
                    data-testid="assignment-add-btn"
                    @click="addAssignment(addAssignmentTechId)"
                  />
                </div>
              </div>
            </div>
            <div class="detail-row">
              <span>Description</span>
              <div class="detail-text">{{ job.description || 'No description provided.' }}</div>
            </div>
            <div v-if="isTechnician" class="detail-row">
              <span>Dispatch Notes</span>
              <Textarea :value="dispatchNotes" rows="3" class="w-full" readonly />
            </div>
          </div>
          <div class="card">
            <div class="card-header">
              <h3>Customer</h3>
              <Button
                v-if="customerDetail?.id"
                label="Edit"
                icon="pi pi-pencil"
                size="small"
                severity="secondary"
                text
                data-testid="job-detail-edit-customer"
                @click="openCustomerEditDialog"
              />
            </div>
            <div class="customer-info">
              <p class="customer-name">{{ customerDetail?.name || job.customer_name || 'Unassigned' }}</p>
              <p v-if="customerDetail?.phone" class="customer-contact">
                <a :href="`tel:${customerDetail.phone}`">📞 {{ formatPhone(customerDetail.phone) }}</a>
              </p>
              <p v-if="customerDetail?.email" class="customer-contact">
                <a :href="`mailto:${customerDetail.email}`">✉️ {{ customerDetail.email }}</a>
              </p>
              <p v-if="customerAddress" class="customer-contact" data-testid="job-customer-address">
                <a :href="`https://maps.google.com/?q=${encodeURIComponent(customerAddress)}`" target="_blank">📍 {{ customerAddress }}</a>
                <!--
                  Sprint customer-multi-location (2026-05-21) — show the
                  picked site's label so the tech sees "Warehouse #3" not
                  just the address line, when the customer has multiple sites.
                -->
                <span v-if="pickedLocationLabel" class="location-label" data-testid="job-location-label">
                  · {{ pickedLocationLabel }}
                </span>
              </p>
              <!--
                /audit 2026-05-21 — picked location with NULL address
                must surface, NOT silently fall back to HQ. Tech sees
                this and knows to ask customer for the address.
              -->
              <p
                v-else-if="pickedLocationAddressMissing"
                class="customer-contact location-missing-address"
                data-testid="job-location-no-address"
              >
                ⚠️ {{ pickedLocationLabel }} — no address on file. Add one in the customer's locations.
              </p>
              <p v-if="accessNotes" class="access-notes" title="Access notes (gate codes, dogs, parking)">
                <span class="access-notes-label">Access notes (gate codes, dogs, parking):</span>
                🔐 {{ accessNotes }}
              </p>
              <div v-if="customerDetail?.notes" class="customer-notes" data-testid="customer-notes">
                <span class="customer-notes-label">Customer notes:</span>
                <p>{{ customerDetail.notes }}</p>
              </div>
            </div>
          </div>
        </div>
        <div class="card equipment-card" v-if="equipmentList.length">
          <div class="card-header">
            <h3>Customer Equipment</h3>
          </div>
          <DataTable :value="equipmentList" striped-rows responsive-layout="scroll" data-testid="equipment-table">
            <Column field="equipment_type" header="Type" />
            <Column field="manufacturer" header="Manufacturer" />
            <Column field="model" header="Model" />
            <Column field="serial_number" header="Serial" />
            <Column field="install_date" header="Installed">
              <template #body="{ data }">
                {{ data.install_date || '—' }}
              </template>
            </Column>
            <Column header="Warranty">
              <template #body="{ data }">
                <Tag
                  v-if="data.warranty_expires_on && new Date(data.warranty_expires_on) >= new Date()"
                  severity="success"
                  :value="`Until ${data.warranty_expires_on}`"
                  data-testid="warranty-active"
                />
                <Tag
                  v-else-if="data.warranty_expires_on"
                  severity="secondary"
                  :value="`Expired ${data.warranty_expires_on}`"
                />
                <span v-else class="muted">—</span>
              </template>
            </Column>
          </DataTable>
        </div>
        <div v-else class="card">
          <div class="card-header">
            <h3>Customer Equipment</h3>
          </div>
          <p class="muted">No equipment linked to this customer.</p>
        </div>
        <div class="card">
          <div class="card-header">
            <h3>Past Visits</h3>
          </div>
          <DataTable
            v-if="pastJobs.length"
            :value="pastJobs"
            striped-rows
            responsive-layout="scroll"
            data-testid="past-visits-table"
          >
            <Column field="scheduled_at" header="Date">
              <template #body="{ data }">
                {{ formatDate(data.scheduled_at) }}
              </template>
            </Column>
            <Column field="job_number" header="Job #" />
            <Column field="title" header="Work" />
            <Column field="tech_name" header="Tech" />
            <Column field="status" header="Status" />
            <Column header="">
              <template #body="{ data }">
                <router-link :to="`/jobs/${data.id}`" class="link">Open</router-link>
              </template>
            </Column>
          </DataTable>
          <p v-else class="muted">No prior jobs at this customer.</p>
        </div>
      </div>

      <div v-else-if="activeTab === 'schedule'" class="tab-panel">
        <div class="card">
          <div class="card-header">
            <h3>Appointments</h3>
            <Button label="Schedule / Reschedule" icon="pi pi-calendar" severity="info" @click="openSchedule(job.id)" />
          </div>
          <div v-if="appointmentsLoading" class="spinner-wrap small"><ProgressSpinner /></div>
          <DataTable v-else :value="appointments" striped-rows responsive-layout="scroll" emptyMessage="No appointments found">
            <Column field="title" header="Title" />
            <Column header="Tech">
              <template #body="{ data }">{{ techLabel(data.tech_id) }}</template>
            </Column>
            <Column field="start_at" header="Start" :body="formatDateTime" />
            <Column field="end_at" header="End" :body="formatDateTime" />
            <Column field="status" header="Status">
              <template #body="{ data }">
                <Tag :value="formatAppointmentStatus(data.status)" :severity="appointmentSeverity(data.status)" />
              </template>
            </Column>
            <Column field="address" header="Address" />
          </DataTable>
        </div>
      </div>

      <div v-else-if="activeTab === 'diagnosis'" class="tab-panel">
        <div class="card">
          <div class="card-header">
            <h3>Diagnosis</h3>
            <div style="display:flex; gap:0.5rem; align-items:center;">
              <Select
                v-model="newDiagnosisType"
                :options="Object.keys(diagnosisSchemas)"
                placeholder="Service type"
                data-testid="diagnosis-type-select"
              />
              <Button
                label="Add"
                icon="pi pi-plus"
                :disabled="!newDiagnosisType"
                data-testid="diagnosis-add-btn"
                @click="addDiagnosis"
              />
            </div>
          </div>
          <div v-if="diagnosesLoading" class="spinner-wrap small"><ProgressSpinner /></div>
          <div v-else-if="diagnoses.length === 0" class="muted" style="padding:0.75rem 0;">
            No diagnosis recorded yet. Pick a service type above and add one.
          </div>
          <div v-else>
            <div
              v-for="diag in diagnoses"
              :key="diag.id"
              class="diagnosis-card"
              :data-testid="`diagnosis-${diag.service_type}`"
            >
              <div class="diagnosis-header">
                <strong>{{ diag.service_type.replace(/_/g, ' ') }}</strong>
                <Button
                  v-tooltip="`Delete diagnosis ${diag.service_type}`"
                  icon="pi pi-trash"
                  :aria-label="`Delete diagnosis ${diag.service_type}`"
                  severity="secondary"
                  text
                  :data-testid="`diagnosis-delete-${diag.id}`"
                  @click="deleteDiagnosis(diag)"
                />
              </div>
              <div
                v-for="field in diagnosisSchemas[diag.service_type] || []"
                :key="field.key"
                class="diagnosis-field"
              >
                <label>{{ field.label }}</label>
                <InputText
                  v-if="field.type === 'text'"
                  v-model="diag.data[field.key]"
                  @change="saveDiagnosis(diag)"
                />
                <InputText
                  v-else-if="field.type === 'number'"
                  type="number"
                  v-model="diag.data[field.key]"
                  @change="saveDiagnosis(diag)"
                />
                <Select
                  v-else-if="field.type === 'select'"
                  v-model="diag.data[field.key]"
                  :options="field.options"
                  showClear
                  @change="saveDiagnosis(diag)"
                />
                <input
                  v-else-if="field.type === 'boolean'"
                  type="checkbox"
                  :checked="!!diag.data[field.key]"
                  @change="(e) => { diag.data[field.key] = e.target.checked; saveDiagnosis(diag); }"
                />
              </div>
              <Textarea
                v-model="diag.notes"
                rows="2"
                placeholder="Free-form notes"
                @change="saveDiagnosis(diag)"
              />
            </div>
          </div>
        </div>
      </div>

      <div v-else-if="activeTab === 'hazards'" class="tab-panel">
        <div class="card">
          <div class="card-header">
            <h3>Hazards</h3>
          </div>
          <div class="hazard-input">
            <Textarea v-model="newHazardDesc" rows="2" placeholder="Describe hazard (gas leak, dog, weak roof, exposed wiring...)" data-testid="hazard-input" />
            <div class="hazard-actions">
              <Select v-model="newHazardSeverity" :options="['low','medium','high','critical']" />
              <InputText v-model="newHazardPhotoUrl" placeholder="Photo URL (optional)" />
              <label style="display:flex;align-items:center;gap:0.4rem;font-size:0.85rem;">
                <input type="checkbox" v-model="newHazardSticky" />
                Applies to customer (every future job)
              </label>
              <Button label="Add" icon="pi pi-plus" data-testid="hazard-add-btn" @click="addHazard" />
            </div>
          </div>
          <div v-if="hazards.length" class="hazard-list">
            <div
              v-for="haz in hazards"
              :key="haz.id"
              class="hazard-card"
              :class="`severity-${haz.severity}`"
              :data-testid="`hazard-${haz.id}`"
            >
              <div class="hazard-header">
                <Tag :value="haz.severity.toUpperCase()" :severity="hazardSeverityColor(haz.severity)" />
                <Tag v-if="haz.applies_to_customer" value="STICKY" severity="warn" />
                <span class="muted">{{ formatDate(haz.created_at) }}</span>
                <Button v-tooltip="'Delete hazard'" icon="pi pi-trash" aria-label="Delete hazard" text severity="secondary" @click="deleteHazard(haz)" />
              </div>
              <p style="white-space:pre-wrap;">{{ haz.description }}</p>
              <a v-if="haz.photo_url" :href="haz.photo_url" target="_blank">View photo</a>
            </div>
          </div>
          <p v-else class="muted">No hazards recorded.</p>
        </div>
      </div>

      <div v-else-if="activeTab === 'receipts'" class="tab-panel">
        <div class="card">
          <div class="card-header">
            <h3>Receipts</h3>
          </div>
          <div class="receipt-input">
            <InputText v-model="newReceiptVendor" placeholder="Vendor (Home Depot, Lowes...)" />
            <InputText v-model="newReceiptAmount" type="number" placeholder="Amount" />
            <InputText v-model="newReceiptPhotoUrl" placeholder="Photo URL" />
            <Button label="Add" icon="pi pi-plus" data-testid="receipt-add-btn" @click="addReceipt" />
          </div>
          <DataTable v-if="receipts.length" :value="receipts" striped-rows responsive-layout="scroll" data-testid="receipts-table">
            <Column field="vendor" header="Vendor" />
            <Column field="amount" header="Amount">
              <template #body="{ data }">
                {{ formatMoney(data.amount) }}
              </template>
            </Column>
            <Column field="created_at" header="When">
              <template #body="{ data }">
                {{ formatDate(data.created_at) }}
              </template>
            </Column>
            <Column header="Photo">
              <template #body="{ data }">
                <a v-if="data.photo_url" :href="data.photo_url" target="_blank">view</a>
                <span v-else class="muted">—</span>
              </template>
            </Column>
            <Column header="">
              <template #body="{ data }">
                <Button v-tooltip="'Delete receipt'" icon="pi pi-trash" aria-label="Delete receipt" text severity="secondary" @click="deleteReceipt(data)" />
              </template>
            </Column>
          </DataTable>
          <p v-else class="muted">No receipts attached.</p>
        </div>
      </div>

      <div v-else-if="activeTab === 'notes'" class="tab-panel">
        <div class="card note-card">
          <div class="card-header">
            <h3>Add Note</h3>
          </div>
          <div class="note-input">
            <Textarea v-model="newNoteBody" rows="3" placeholder="Enter note" data-testid="job-detail-note-input" />
            <div class="note-actions">
              <Select v-model="newNoteVisibility" :options="noteVisibilityOptions" placeholder="Visibility" data-testid="job-detail-note-visibility" />
              <Button label="Add Note" icon="pi pi-plus" severity="primary" @click="addNote" data-testid="job-detail-add-note" />
            </div>
          </div>
        </div>
        <div class="notes-feed">
          <div v-if="!jobNotes.length" class="muted">No notes yet.</div>
          <div v-else class="note-entry" v-for="note in jobNotes" :key="note.id">
            <div class="note-meta">
              <strong>{{ note.author_name || 'Unknown' }}</strong>
              <span>· {{ note.visibility }} · {{ formatDateTime(note.created_at) }}</span>
            </div>
            <p class="note-body">{{ note.body }}</p>
          </div>
        </div>
      </div>

      <div v-else-if="activeTab === 'photos'" class="tab-panel">
        <div class="card photo-card">
          <div class="card-header">
            <h3>Photos & Documents</h3>
            <FileUpload
              mode="basic"
              custom-upload
              :uploadHandler="handlePhotoUpload"
              choose-label="+ Add Photo"
              accept="image/*"
              :maxFileSize="10000000"
              data-testid="job-detail-photo-upload"
            />
          </div>
          <div v-if="photoDocs.length === 0" class="empty-state"><p>No photos yet.</p></div>
          <div v-else class="photo-grid">
            <div v-for="doc in photoDocs" :key="doc.id" class="photo-card">
              <div class="photo-preview">📷</div>
              <div class="photo-meta">
                <p class="photo-name">{{ doc.original_name }}</p>
                <p class="photo-date">{{ formatDateTime(doc.created_at) }}</p>
                <Button v-tooltip="'Download document'" icon="pi pi-download" aria-label="Download document" text @click="downloadDocument(doc.id)" />
              </div>
            </div>
          </div>
        </div>
      </div>

      <div v-else-if="activeTab === 'signature'" class="tab-panel">
        <div class="card signature-card">
          <div class="card-header">
            <h3>Signature</h3>
            <Button label="Capture Signature" icon="pi pi-pen" severity="primary" @click="signatureDialog = true" data-testid="job-detail-capture-signature" />
          </div>
          <div v-if="signatureDoc" class="signature-preview">
            <p class="muted">Signature captured on {{ formatDateTime(signatureDoc.created_at) }}</p>
            <Button v-tooltip="'Download signature'" icon="pi pi-download" aria-label="Download signature" text @click="downloadDocument(signatureDoc.id)" />
          </div>
          <p v-else class="muted">No signature captured yet.</p>
        </div>
      </div>

      <div v-else-if="activeTab === 'costing'" class="tab-panel">
        <div class="card costing-summary">
          <div class="card-header">
            <h3>Costing</h3>
            <div class="costing-values">
              <span>Labor: {{ formatCurrency(costing?.labor?.total) }}</span>
              <span>Parts: {{ formatCurrency(costing?.parts?.total) }}</span>
              <span>Overhead: {{ formatCurrency(costing?.overhead?.total) }}</span>
              <span>Total Cost: {{ formatCurrency(costing?.total_cost) }}</span>
              <span>Invoiced: {{ formatCurrency(costing?.invoiced_amount) }}</span>
              <span>Profit: {{ formatCurrency(costing?.profit) }}</span>
              <span>Margin: {{ formatPercent(costing?.margin_percent) }}</span>
            </div>
          </div>
        </div>
        <div class="card parts-card">
          <div class="card-header">
            <h3>Parts Used</h3>
            <Button label="+ Add Part" icon="pi pi-plus" severity="secondary" @click="openAddPart" data-testid="job-detail-add-part" />
            <Button label="+ Order Part" icon="pi pi-shopping-cart" severity="info" @click="openOrderPart" data-testid="job-detail-order-part" />
            <Button label="Apply Template" icon="pi pi-file" severity="info" text @click="openApplyTemplate" data-testid="job-detail-apply-template" />
          </div>
          <div v-if="!costing?.parts?.items?.length" class="muted">No parts recorded.</div>
          <DataTable v-else :value="costing.parts.items" striped-rows responsive-layout="scroll">
            <Column field="name" header="Part / Item" />
            <Column field="qty" header="Qty" />
            <Column field="unit_cost" header="Unit cost (override, optional)">
              <template #body="{ data }">{{ formatCurrency(data.unit_cost) }}</template>
            </Column>
            <Column field="subtotal" header="Total">
              <template #body="{ data }">{{ formatCurrency(data.subtotal) }}</template>
            </Column>
          </DataTable>
        </div>
        <div class="card dispatch-status-card">
          <div class="card-header">
            <h3>Dispatch status</h3>
          </div>
          <p class="dispatch-status-value">{{ job?.dispatch_status || job?.status || '—' }}</p>
        </div>
        <div class="card time-entry-card">
          <div class="card-header"><h3>Time Entries</h3></div>
          <DataTable :value="timeEntries" striped-rows responsive-layout="scroll" emptyMessage="No time entries yet">
            <Column field="technician_name" header="Tech" />
            <Column field="clock_in" header="Clock In" :body="formatDateTime" />
            <Column field="clock_out" header="Clock Out" :body="formatDateTime" />
            <Column header="Hours">
              <template #body="{ data }">{{ formatHours(data.duration_minutes) }}</template>
            </Column>
          </DataTable>
        </div>
        <div class="card estimates-card">
          <div class="card-header"><h3>Estimates ({{ relatedEstimates.length }})</h3></div>
          <DataTable :value="relatedEstimates" striped-rows responsive-layout="scroll" class="table-small">
            <Column field="estimate_number" header="Estimate" />
            <Column field="status" header="Status">
              <template #body="{ data }"><Tag :value="data.status" :severity="estimateStatusSeverity(data.status)" /></template>
            </Column>
            <Column field="total" header="Total">
              <template #body="{ data }">{{ formatCurrency(data.total) }}</template>
            </Column>
            <Column field="valid_until" header="Valid" />
            <Column header="Actions">
              <template #body="{ data }">
                <Button v-tooltip="'Open estimate'" icon="pi pi-external-link" aria-label="Open estimate" text size="small" @click="openEstimate(data.id)" />
              </template>
            </Column>
          </DataTable>
        </div>
        <div class="card invoices-card">
          <div class="card-header"><h3>Invoices ({{ relatedInvoices.length }})</h3></div>
          <DataTable :value="relatedInvoices" striped-rows responsive-layout="scroll" class="table-small">
            <Column field="invoice_number" header="Invoice" />
            <Column field="status" header="Status">
              <template #body="{ data }"><Tag :value="data.status" :severity="invoiceStatusSeverity(data.status)" /></template>
            </Column>
            <Column field="total" header="Total">
              <template #body="{ data }">{{ formatCurrency(data.total) }}</template>
            </Column>
            <Column field="due_date" header="Due" />
            <Column header="Actions">
              <template #body="{ data }">
                <Button v-tooltip="'Open invoice'" icon="pi pi-external-link" aria-label="Open invoice" text size="small" @click="openInvoice(data.id)" />
              </template>
            </Column>
          </DataTable>
        </div>
      </div>

      <div v-else-if="activeTab === 'install'" class="tab-panel">
        <div v-if="installLoading" class="muted">Loading install specs...</div>
        <div v-else-if="!installData" class="muted">No install specs available. Create an estimate with door catalog items first.</div>
        <template v-else>
          <!-- Door Specs -->
          <div v-if="installData.door_specs" class="card" style="margin-bottom:1rem">
            <div class="card-header"><h3>Door Specifications</h3></div>
            <div class="specs-grid">
              <div class="spec-item" v-for="(val, key) in installData.door_specs" :key="key">
                <span class="spec-label">{{ formatSpecLabel(key) }}</span>
                <span class="spec-value">{{ val || '—' }}</span>
              </div>
            </div>
          </div>
          <!-- Parts List -->
          <div class="card" style="margin-bottom:1rem">
            <div class="card-header"><h3>Parts & Materials</h3></div>
            <DataTable :value="installData.lines" stripedRows responsiveLayout="scroll">
              <Column field="description" header="Item" />
              <Column header="Qty" style="width:70px;text-align:center">
                <template #body="{ data }">{{ data.quantity }}</template>
              </Column>
              <Column header="Price" style="width:100px;text-align:right">
                <template #body="{ data }">{{ formatMoney(data.unit_price) }}</template>
              </Column>
            </DataTable>
          </div>
          <!-- Notes -->
          <div v-if="installData.notes" class="card">
            <div class="card-header"><h3>Install Notes</h3></div>
            <p style="white-space:pre-wrap;padding:0.5rem">{{ installData.notes }}</p>
          </div>
        </template>
      </div>

      <div v-else class="tab-panel">
        <div class="card activity-card">
          <div class="card-header"><h3>Activity</h3></div>
          <div v-if="!activityLog.length" class="muted">No activity recorded.</div>
          <ul v-else class="activity-list">
            <li v-for="act in activityLog" :key="act.id" class="activity-row">
              <span class="activity-symbol"></span>
              <div>
                <p class="activity-text">{{ act.details?.message || act.action }}</p>
                <p class="activity-meta">{{ act.user_name || act.user_id || 'System' }} · {{ formatDateTime(act.created_at) }}</p>
              </div>
            </li>
          </ul>
        </div>
      </div>
    </section>

    <Dialog v-model:visible="addPartDialog" header="Add Part" modal :style="{ width: '420px' }">
      <div class="form-field">
        <label>Part</label>
        <Select
          v-model="addPartForm.part_id"
          :options="inventoryOptions"
          optionLabel="label"
          optionValue="value"
          placeholder="Select a part"
          filter
          show-clear
        />
      </div>
      <div class="form-field">
        <label>Quantity</label>
        <InputText v-model="addPartForm.quantity" type="number" min="1" />
      </div>
      <div class="dialog-actions">
        <Button label="Cancel" severity="secondary" text @click="addPartDialog = false" />
        <Button label="Add" severity="success" @click="savePart" :loading="addingPart" />
      </div>
    </Dialog>

    <!-- D-S122-job-detail-add-parts-desktop: dispatcher-side "order parts for
         this job" dialog. POSTs /api/jobs/:id/parts-needed (the pre-order
         flow) so the parts surface in `/billing/new`'s parts-from-job
         checklist later. Distinct from "Add Part" above which writes
         parts-used (closeout snapshot, inventory math). -->
    <Dialog v-model:visible="orderPartDialog" header="Order Part for this Job" modal :style="{ width: '480px' }">
      <div class="form-field">
        <label>Part name *</label>
        <InputText v-model="orderPartForm.part_name" placeholder="e.g. Torsion spring 2in 27c" data-testid="order-part-name" />
      </div>
      <div class="form-field">
        <label>SKU (optional)</label>
        <InputText v-model="orderPartForm.sku" placeholder="if known" data-testid="order-part-sku" />
      </div>
      <div class="form-field">
        <label>Quantity</label>
        <InputText v-model="orderPartForm.quantity" type="number" min="1" data-testid="order-part-qty" />
      </div>
      <div class="form-field">
        <label>Supplier (optional)</label>
        <InputText v-model="orderPartForm.supplier" data-testid="order-part-supplier" />
      </div>
      <div class="form-field">
        <label>Urgency</label>
        <Select
          v-model="orderPartForm.urgency"
          :options="[{label:'Normal',value:'normal'},{label:'Urgent',value:'urgent'},{label:'Critical',value:'critical'}]"
          optionLabel="label"
          optionValue="value"
          data-testid="order-part-urgency"
        />
      </div>
      <div class="form-field">
        <label>Notes (optional)</label>
        <InputText v-model="orderPartForm.notes" data-testid="order-part-notes" />
      </div>
      <div class="dialog-actions">
        <Button label="Cancel" severity="secondary" text @click="orderPartDialog = false" />
        <Button
          label="Order"
          severity="success"
          :disabled="!orderPartForm.part_name"
          :loading="orderingPart"
          @click="saveOrderPart"
          data-testid="order-part-submit"
        />
      </div>
    </Dialog>

    <Dialog v-model:visible="signatureDialog" header="Capture Signature" modal :style="{ width: '560px' }">
      <div class="signature-canvas-wrap">
        <canvas
          ref="signatureCanvas"
          class="signature-canvas"
          @pointerdown="startSignature"
          @pointermove="drawSignature"
          @pointerup="endSignature"
          @pointerleave="endSignature"
        ></canvas>
      </div>
      <div class="signature-actions">
        <Button label="Clear" text @click="clearSignature" />
        <Button label="Save" severity="success" @click="saveSignature" />
      </div>
    </Dialog>

    <JobStateOverrideDialog
      v-model="showStateOverride"
      :job="job"
      @applied="onStateOverrideApplied"
    />

    <Dialog
      v-model:visible="customerEditDialog"
      header="Edit Customer"
      modal
      :style="{ width: '480px' }"
      data-testid="job-detail-customer-edit-dialog"
    >
      <div class="form-field">
        <label>Name</label>
        <InputText v-model="customerEditForm.name" data-testid="customer-edit-name" />
      </div>
      <div class="form-field">
        <label>Phone</label>
        <InputText v-model="customerEditForm.phone" data-testid="customer-edit-phone" />
      </div>
      <div class="form-field">
        <label>Email</label>
        <InputText v-model="customerEditForm.email" type="email" data-testid="customer-edit-email" />
      </div>
      <div class="form-field">
        <label>Address</label>
        <Textarea v-model="customerEditForm.address" rows="2" data-testid="customer-edit-address" />
      </div>
      <p v-if="customerEditError" class="p-error" style="margin:0.5rem 0">{{ customerEditError }}</p>
      <div class="dialog-actions">
        <Button label="Cancel" severity="secondary" text @click="customerEditDialog = false" />
        <Button
          label="Save"
          severity="success"
          :loading="savingCustomer"
          data-testid="customer-edit-save"
          @click="saveCustomerEdit"
        />
      </div>
    </Dialog>
</template>

<script setup>
import { ref, computed, onMounted, nextTick, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import JobStateOverrideDialog from "../components/JobStateOverrideDialog.vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatDate, formatDateTime, formatMoney, formatMoney as formatCurrency, formatPercent as fmtPercent } from "../composables/useFormatters";
import { useToast } from "primevue/usetoast";
import { useAuthStore } from "../stores/auth";
import { isTechnician as isTechRole } from "../constants/roles";
import Button from "primevue/button";
import Tabs from "primevue/tabs";
import TabList from "primevue/tablist";
import Tab from "primevue/tab";
import Select from "primevue/select";
import InputText from "primevue/inputtext";
import Textarea from "primevue/textarea";
import DataTable from "primevue/datatable";
import Column from "primevue/column";
import FileUpload from "primevue/fileupload";
import Dialog from "primevue/dialog";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import JobStateChip from "../components/JobStateChip.vue";

const route = useRoute();
const router = useRouter();
const api = useApiWithToast();
const toast = useToast();
const auth = useAuthStore();

const job = ref({});
const loading = ref(true);
const error = ref("");
const activeTab = ref("details");
const relatedEstimates = ref([]);
const relatedInvoices = ref([]);
const appointments = ref([]);
const appointmentsLoading = ref(false);
const timeEntries = ref([]);
const documents = ref([]);
const activityLog = ref([]);
const installData = ref(null);
const installLoading = ref(false);
const jobNotes = ref([]);
const technicians = ref([]);
const equipmentList = ref([]);
const customerDetail = ref(null);
const pastJobs = ref([]);
const diagnoses = ref([]);
const diagnosesLoading = ref(false);
const diagnosisSchemas = ref({});
const newDiagnosisType = ref(null);
const hazards = ref([]);
const newHazardDesc = ref("");
const newHazardSeverity = ref("medium");
const newHazardPhotoUrl = ref("");
const newHazardSticky = ref(false);
const receipts = ref([]);
const newReceiptVendor = ref("");
const newReceiptAmount = ref("");
const newReceiptPhotoUrl = ref("");
const customerLocations = ref([]);
const costing = ref(null);
const inventoryItems = ref([]);
const addPartDialog = ref(false);
const addPartForm = ref({ part_id: null, quantity: 1 });
// D-S122-job-detail-add-parts-desktop — separate "order parts" pre-order flow.
const orderPartDialog = ref(false);
const orderingPart = ref(false);
const orderPartForm = ref({
  part_name: '',
  sku: '',
  quantity: 1,
  supplier: '',
  urgency: 'normal',
  notes: '',
});
const addingPart = ref(false);
const signatureDialog = ref(false);
const signatureCanvas = ref(null);
const isDrawing = ref(false);
const customerEditDialog = ref(false);
const customerEditForm = ref({ name: "", phone: "", email: "", address: "" });
const customerEditError = ref("");
const savingCustomer = ref(false);
// 2026-04-29 nav-cleanup: align with the canonical lifecycle stages from
// /api/jobs (Lead, Estimate, Scheduled, In Progress, Complete). Previous
// {Scheduled, Sold, In Progress, Complete} introduced a "Sold" stage the
// backend doesn't know about and dropped Lead+Estimate, so the user couldn't
// move a job back if status had advanced incorrectly.
const stageButtons = ["Service Call", "Estimate", "Scheduled", "In Progress", "Complete"];
const newNoteBody = ref("");
const newNoteVisibility = ref("internal");
const selectedTech = ref(null);
const selectedPriority = ref(null);
// S97 slice 6 — multi-tech crew assignments (desktop).
const assignments = ref([]);
const assignmentsLoading = ref(false);
const addAssignmentTechId = ref(null);
const addingAssignment = ref(false);

const priorityOptions = ["Low", "Normal", "High", "Urgent"].map((value) => ({ label: value, value }));
const noteVisibilityOptions = [
  { label: "Internal", value: "internal" },
  { label: "External", value: "external" },
];

// `.value` was breaking these — captured at setup before fetchTechnicians /
// fetchInventory resolved, so the dropdowns rendered with empty options
// forever. Keep them as proper computed refs so they react to load.
const techOptions = computed(() => technicians.value.map((tech) => ({
  label: tech.name || tech.display_name || tech.email || `Tech ${String(tech.id).slice(0, 8)}`,
  value: tech.id,
})));
// Tech options minus anyone already on the crew — used by the "Add another tech" select.
const unassignedTechOptions = computed(() => {
  const taken = new Set(assignments.value.map((a) => a.tech_id));
  return techOptions.value.filter((opt) => !taken.has(opt.value));
});
const inventoryOptions = computed(() => inventoryItems.value.map((item) => ({
  label: `${item.part_name} (${item.sku || item.name})`,
  value: item.id,
})));
const dispatchNotes = computed(() => job.value.notes || job.value.description || "No dispatch notes." );
// Sprint customer-multi-location (2026-05-21) — if the job is bound to
// a specific customer_locations row, that wins. Otherwise fall back to
// the customer's primary location, then their (deprecated single-string)
// address column. Same precedence for accessNotes.
//
// /audit catch 2026-05-21: when a picked location has a NULL address
// (legal per the schema — label-only rows exist), we must NOT silently
// substitute the customer's HQ address. The tech would see "Warehouse #3"
// on the label and drive to the HQ — the exact field-error this sprint
// is built to prevent. `pickedLocationAddressMissing` surfaces a visible
// signal in the template.
const pickedLocation = computed(() => {
  const lid = job.value?.location_id;
  if (!lid) return null;
  return customerLocations.value.find((loc) => String(loc.id) === String(lid)) || null;
});
const customerAddress = computed(() => {
  if (pickedLocation.value) {
    // Explicit: only the picked location's address, never the customer
    // HQ fallback. NULL/empty surfaces as missing in the template.
    return pickedLocation.value.address || null;
  }
  if (customerLocations.value.length) {
    return customerLocations.value.find((loc) => loc.is_primary)?.address || customerLocations.value[0].address;
  }
  return customerDetail.value?.address;
});
const accessNotes = computed(() => {
  if (pickedLocation.value) return pickedLocation.value.access_notes || "";
  if (customerLocations.value.length) {
    return customerLocations.value.find((loc) => loc.is_primary)?.access_notes || customerLocations.value[0].access_notes;
  }
  return "";
});
const pickedLocationLabel = computed(() => pickedLocation.value?.label || null);
const pickedLocationAddressMissing = computed(
  () => Boolean(pickedLocation.value) && !pickedLocation.value.address,
);
const photoDocs = computed(() => documents.value.filter((doc) => doc.entity_type === "job_photo"));
const signatureDoc = computed(() => documents.value.find((doc) => doc.entity_type === "job_signature"));
const isTechnician = computed(() => isTechRole(auth.user?.role));
// Techs can't patch job fields (variant-aware: was `!== "tech"`, which missed
// the long-form 'technician' spelling).
const patchable = computed(() => !isTechnician.value);

function invoiceStatusSeverity(status) {
  const map = {
    draft: "secondary",
    sent: "info",
    paid: "success",
    overdue: "danger",
    void: "secondary",
  };
  return map[(status || "").toLowerCase()] || "secondary";
}

function stageSeverity(stage) {
  if (!job.value.status) return "secondary";
  if (stage === job.value.status) return "success";
  if (stage === "Complete" && job.value.status === "Invoiced") return "success";
  return "secondary";
}

const noteVisibilityLabel = (vis) => (vis === "external" ? "External" : "Internal");

function formatHours(minutes) {
  if (minutes == null) return "—";
  return `${(Number(minutes) / 60).toFixed(2)} h`;
}

function formatPercent(value) {
  return fmtPercent(value, { whole: true });
}

function formatPhone(raw) {
  if (!raw) return "—";
  return raw;
}

function techLabel(id) {
  if (!id) return "Unassigned";
  const tech = technicians.value.find((tech) => tech.id === id);
  if (!tech) return `Tech ${String(id).slice(0, 8)}`;
  return tech.name || tech.display_name || tech.email || tech.user_id || `Tech ${String(id).slice(0, 8)}`;
}

function formatAppointmentStatus(status) {
  if (!status) return "Scheduled";
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function appointmentSeverity(status) {
  const map = {
    scheduled: "info",
    confirmed: "success",
    cancelled: "danger",
    en_route: "warning",
    arrived: "warning",
    completed: "success",
  };
  return map[status] || "secondary";
}

async function fetchJob() {
  loading.value = true;
  error.value = "";
  try {
    const data = await api.get(`/api/jobs/${route.params.id}`);
    job.value = data?.data || data || {};
    selectedTech.value = job.value.assigned_to || null;
    selectedPriority.value = job.value.priority || null;
    await refreshRelated();
  } catch (err) {
    error.value = err?.message || "Failed to load job";
  } finally {
    loading.value = false;
  }
}

async function refreshRelated() {
  if (!job.value?.id) return;
  await Promise.all([
    fetchRelatedEstimates(),
    fetchRelatedInvoices(),
    fetchTimeEntries(),
    fetchDocuments(),
    fetchActivity(),
    fetchNotes(),
    fetchCosting(),
    fetchTechnicians(),
    fetchAssignments(),
    fetchAppointments(),
    fetchEquipment(),
    fetchCustomerDetail(),
    fetchPastJobs(),
    fetchDiagnosisSchemas(),
    fetchDiagnoses(),
    fetchHazards(),
    fetchReceipts(),
  ]);
}

async function fetchRelatedEstimates() {
  try {
    const data = await api.get(`/api/estimates?job_id=${encodeURIComponent(route.params.id)}`);
    relatedEstimates.value = Array.isArray(data) ? data : data?.items || [];
  } catch {
    relatedEstimates.value = [];
  }
}

async function fetchRelatedInvoices() {
  try {
    const data = await api.get(`/api/invoices?job_id=${encodeURIComponent(route.params.id)}`);
    relatedInvoices.value = Array.isArray(data) ? data : data?.items || [];
  } catch {
    relatedInvoices.value = [];
  }
}

async function fetchTimeEntries() {
  try {
    const data = await api.get(`/api/labor/jobs/${route.params.id}/time-entries`);
    timeEntries.value = Array.isArray(data) ? data : data?.items || [];
  } catch {
    timeEntries.value = [];
  }
}

async function fetchDocuments() {
  try {
    const data = await api.get(`/api/documents?job_id=${encodeURIComponent(route.params.id)}`);
    documents.value = Array.isArray(data) ? data : data?.items || [];
  } catch {
    documents.value = [];
  }
}

async function fetchActivity() {
  try {
    const data = await api.get(`/api/jobs/${route.params.id}/activity`);
    activityLog.value = Array.isArray(data?.items) ? data.items : Array.isArray(data) ? data : data?.items || [];
  } catch {
    activityLog.value = [];
  }
}

async function fetchNotes() {
  try {
    const data = await api.get(`/api/jobs/${route.params.id}/notes`);
    jobNotes.value = Array.isArray(data) ? data : data?.items || [];
  } catch {
    jobNotes.value = [];
  }
}

async function fetchCosting() {
  try {
    const data = await api.get(`/api/costing/jobs/${route.params.id}`);
    costing.value = data || null;
  } catch {
    costing.value = null;
  }
}

async function fetchTechnicians() {
  try {
    const data = await api.get("/api/technicians");
    technicians.value = Array.isArray(data) ? data : [];
  } catch {
    technicians.value = [];
  }
}

async function fetchAppointments() {
  if (!job.value?.id) return;
  appointmentsLoading.value = true;
  try {
    const start = new Date();
    start.setDate(start.getDate() - 15);
    const end = new Date();
    end.setDate(end.getDate() + 15);
    const params = new URLSearchParams({
      start: start.toISOString().split("T")[0],
      end: end.toISOString().split("T")[0],
      limit: "200",
    });
    const data = await api.get(`/api/appointments?${params.toString()}`);
    const list = Array.isArray(data) ? data : data?.items || [];
    appointments.value = list.filter((appt) => appt.job_id === route.params.id);
  } catch {
    appointments.value = [];
  } finally {
    appointmentsLoading.value = false;
  }
}

async function fetchEquipment() {
  try {
    const data = await api.get("/api/equipment");
    const list = Array.isArray(data) ? data : data?.items || [];
    equipmentList.value = list.filter((item) => item.customer_id === job.value.customer_id);
  } catch {
    equipmentList.value = [];
  }
}

async function fetchDiagnosisSchemas() {
  if (Object.keys(diagnosisSchemas.value).length) return;
  try {
    const data = await api.get("/api/diagnosis/schemas");
    diagnosisSchemas.value = data?.schemas || {};
  } catch {
    diagnosisSchemas.value = {};
  }
}

async function fetchDiagnoses() {
  diagnosesLoading.value = true;
  try {
    const data = await api.get(`/api/jobs/${route.params.id}/diagnosis`);
    diagnoses.value = Array.isArray(data) ? data : [];
  } catch {
    diagnoses.value = [];
  } finally {
    diagnosesLoading.value = false;
  }
}

async function addDiagnosis() {
  if (!newDiagnosisType.value) return;
  try {
    const created = await api.post(
      `/api/jobs/${route.params.id}/diagnosis`,
      { service_type: newDiagnosisType.value, data: {}, notes: null },
      { successMessage: "Diagnosis added" }
    );
    diagnoses.value.unshift(created);
    newDiagnosisType.value = null;
  } catch {
    /* api helper toasts errors */
  }
}

async function saveDiagnosis(diag) {
  try {
    await api.patch(`/api/diagnosis/${diag.id}`, {
      service_type: diag.service_type,
      data: diag.data || {},
      notes: diag.notes,
    });
  } catch {
    /* swallow — change handler */
  }
}

async function deleteDiagnosis(diag) {
  try {
    await api.del(`/api/diagnosis/${diag.id}`, { successMessage: "Diagnosis deleted" });
    diagnoses.value = diagnoses.value.filter((d) => d.id !== diag.id);
  } catch {
    /* api helper toasts errors */
  }
}

function hazardSeverityColor(s) {
  return s === "critical" ? "danger" : s === "high" ? "warn" : s === "low" ? "secondary" : "info";
}

async function fetchHazards() {
  try {
    const data = await api.get(`/api/jobs/${route.params.id}/hazards`);
    hazards.value = Array.isArray(data) ? data : [];
  } catch {
    hazards.value = [];
  }
}

async function addHazard() {
  if (!newHazardDesc.value.trim()) return;
  try {
    const created = await api.post(
      `/api/jobs/${route.params.id}/hazards`,
      {
        description: newHazardDesc.value.trim(),
        severity: newHazardSeverity.value,
        photo_url: newHazardPhotoUrl.value || null,
        applies_to_customer: newHazardSticky.value,
      },
      { successMessage: "Hazard recorded" }
    );
    hazards.value.unshift(created);
    newHazardDesc.value = "";
    newHazardPhotoUrl.value = "";
    newHazardSticky.value = false;
    newHazardSeverity.value = "medium";
  } catch {
    /* api helper toasts */
  }
}

async function deleteHazard(haz) {
  try {
    await api.del(`/api/hazards/${haz.id}`, { successMessage: "Hazard deleted" });
    hazards.value = hazards.value.filter((h) => h.id !== haz.id);
  } catch {
    /* api helper toasts */
  }
}

async function fetchReceipts() {
  try {
    const data = await api.get(`/api/jobs/${route.params.id}/receipts`);
    receipts.value = Array.isArray(data) ? data : [];
  } catch {
    receipts.value = [];
  }
}

async function addReceipt() {
  if (!newReceiptVendor.value.trim() && !newReceiptAmount.value) return;
  try {
    const created = await api.post(
      `/api/jobs/${route.params.id}/receipts`,
      {
        vendor: newReceiptVendor.value || null,
        amount: newReceiptAmount.value ? Number(newReceiptAmount.value) : null,
        photo_url: newReceiptPhotoUrl.value || null,
      },
      { successMessage: "Receipt added" }
    );
    receipts.value.unshift(created);
    newReceiptVendor.value = "";
    newReceiptAmount.value = "";
    newReceiptPhotoUrl.value = "";
  } catch {
    /* api helper toasts */
  }
}

async function deleteReceipt(rec) {
  try {
    await api.del(`/api/receipts/${rec.id}`, { successMessage: "Receipt deleted" });
    receipts.value = receipts.value.filter((r) => r.id !== rec.id);
  } catch {
    /* api helper toasts */
  }
}

async function fetchPastJobs() {
  if (!job.value.customer_id) {
    pastJobs.value = [];
    return;
  }
  try {
    const data = await api.get(
      `/api/jobs?customer_id=${encodeURIComponent(job.value.customer_id)}&page_size=50`
    );
    const list = Array.isArray(data) ? data : data?.items || [];
    pastJobs.value = list.filter((j) => j.id !== job.value.id);
  } catch {
    pastJobs.value = [];
  }
}

async function fetchCustomerDetail() {
  if (!job.value.customer_id) {
    customerDetail.value = null;
    customerLocations.value = [];
    return;
  }
  try {
    customerDetail.value = await api.get(`/api/customers/${encodeURIComponent(job.value.customer_id)}`);
  } catch {
    customerDetail.value = null;
  }
  try {
    const locs = await api.get(`/api/customers/${encodeURIComponent(job.value.customer_id)}/locations`);
    customerLocations.value = Array.isArray(locs) ? locs : [];
  } catch {
    customerLocations.value = [];
  }
}

function openCustomerEditDialog() {
  if (!customerDetail.value?.id) return;
  customerEditError.value = "";
  customerEditForm.value = {
    name: customerDetail.value.name || "",
    phone: customerDetail.value.phone || "",
    email: customerDetail.value.email || "",
    address: customerDetail.value.address || "",
  };
  customerEditDialog.value = true;
}

async function saveCustomerEdit() {
  customerEditError.value = "";
  const name = customerEditForm.value.name?.trim();
  if (!name) {
    customerEditError.value = "Name is required.";
    return;
  }
  savingCustomer.value = true;
  try {
    const patch = {
      name,
      phone: customerEditForm.value.phone?.trim() || "",
      email: customerEditForm.value.email?.trim() || "",
      address: customerEditForm.value.address?.trim() || "",
    };
    await api.patch(
      `/api/customers/${encodeURIComponent(customerDetail.value.id)}`,
      patch,
      { successMessage: "Customer updated." },
    );
    customerEditDialog.value = false;
    await fetchCustomerDetail();
  } catch (e) {
    customerEditError.value = e?.message || "Failed to save.";
  } finally {
    savingCustomer.value = false;
  }
}

async function applyStage(stage) {
  if (!job.value.id) return;
  try {
    await api.patch(`/api/jobs/${job.value.id}`, { status: stage }, { successMessage: `Status set to ${stage}` });
    await fetchJob();
  } catch {
    // handled in composable
  }
}

async function assignTech(techId) {
  if (!job.value.id) return;
  try {
    await api.patch(`/api/jobs/${job.value.id}`, { assigned_tech_id: techId }, { successMessage: techId ? "Technician assigned" : "Technician cleared" });
    await fetchJob();
  } catch {
  }
}

// --- S97 slice 6 — multi-tech assignment widget ---
async function fetchAssignments() {
  if (!route.params.id) return;
  assignmentsLoading.value = true;
  try {
    const data = await api.get(`/api/jobs/${route.params.id}/assignments`);
    assignments.value = Array.isArray(data) ? data : (data?.data || []);
  } catch {
    assignments.value = [];
  } finally {
    assignmentsLoading.value = false;
  }
}

async function addAssignment(techId) {
  if (!techId || !route.params.id) return;
  addingAssignment.value = true;
  try {
    // First crew member auto-becomes lead; subsequent additions stay non-lead.
    const isLead = assignments.value.length === 0;
    await api.post(
      `/api/jobs/${route.params.id}/assignments`,
      { tech_id: techId, is_lead: isLead },
      { successMessage: "Tech assigned" }
    );
    addAssignmentTechId.value = null;
    await fetchAssignments();
    // Primary tech (Job.assigned_to) recomputes server-side; reflect it.
    await fetchJob();
  } catch { /* api toasts errors */ }
  finally { addingAssignment.value = false; }
}

async function removeAssignment(assignmentId) {
  if (!assignmentId || !route.params.id) return;
  try {
    await api.del(
      `/api/jobs/${route.params.id}/assignments/${assignmentId}`,
      { successMessage: "Tech removed" }
    );
    await fetchAssignments();
    await fetchJob();
  } catch { /* api toasts errors */ }
}

async function setLead(techId) {
  if (!techId || !route.params.id) return;
  try {
    await api.put(
      `/api/jobs/${route.params.id}/lead`,
      { tech_id: techId },
      { successMessage: "Lead tech set" }
    );
    await fetchAssignments();
    await fetchJob();
  } catch { /* api toasts errors */ }
}

async function updatePriority() {
  if (!job.value.id) return;
  try {
    await api.patch(`/api/jobs/${job.value.id}`, { priority: selectedPriority.value }, { successMessage: "Priority updated" });
    await fetchJob();
  } catch {
  }
}

async function addNote() {
  if (!newNoteBody.value.trim()) {
    toast.add({ severity: "warn", summary: "Enter a note", life: 2500 });
    return;
  }
  try {
    await api.post(`/api/jobs/${route.params.id}/notes`, { body: newNoteBody.value.trim(), visibility: newNoteVisibility.value }, { successMessage: "Note added" });
    newNoteBody.value = "";
    await fetchNotes();
  } catch {
  }
}

async function handlePhotoUpload(event) {
  if (!event.files?.length) return;
  const formData = new FormData();
  event.files.forEach((file) => formData.append("file", file));
  try {
    await api.request(`/api/jobs/${route.params.id}/photos`, { method: "POST", body: formData });
    toast.add({ severity: "success", summary: "Photo uploaded", life: 2500 });
    await fetchDocuments();
  } catch {
  } finally {
    event.options?.clear?.();
    event.clear?.();
  }
}

async function downloadDocument(id) {
  const url = `/api/documents/${encodeURIComponent(id)}/download`;
  window.open(url, "_blank", "noopener");
}

function openSchedule(jobId) {
  router.push(`/appointments?job_id=${encodeURIComponent(jobId || route.params.id)}`);
}

function openEstimate(id) {
  router.push(`/estimates/${id}`);
}

function openInvoice(id) {
  router.push(`/billing/${id}`);
}

async function openAddPart() {
  if (!inventoryItems.value.length) {
    try {
      inventoryItems.value = Array.isArray(await api.get("/api/inventory/items")) ? await api.get("/api/inventory/items") : [];
    } catch {
      inventoryItems.value = [];
    }
  }
  addPartForm.value = { part_id: null, quantity: 1 };
  addPartDialog.value = true;
}

// D-S122-job-detail-add-parts-desktop — dispatcher-side "order parts for
// this job" handler. POSTs to /api/jobs/:id/parts-needed (pre-order flow).
function openOrderPart() {
  orderPartForm.value = {
    part_name: '',
    sku: '',
    quantity: 1,
    supplier: '',
    urgency: 'normal',
    notes: '',
  };
  orderPartDialog.value = true;
}

async function saveOrderPart() {
  const f = orderPartForm.value;
  if (!f.part_name?.trim()) {
    toast.add({ severity: 'warn', summary: 'Missing', detail: 'Part name is required', life: 3000 });
    return;
  }
  orderingPart.value = true;
  try {
    await api.post(`/api/jobs/${route.params.id}/parts-needed`, {
      part_name: f.part_name.trim(),
      quantity: Math.max(1, Number(f.quantity) || 1),
      sku: f.sku?.trim() || null,
      supplier: f.supplier?.trim() || '',
      urgency: f.urgency || 'normal',
      notes: f.notes?.trim() || '',
    });
    orderPartDialog.value = false;
    toast.add({ severity: 'success', summary: 'Ordered', detail: 'Part added to job order list', life: 3000 });
    await fetchJob();
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Error', detail: e?.message || 'Failed to order part', life: 4000 });
  } finally {
    orderingPart.value = false;
  }
}

async function openApplyTemplate() {
  try {
    const templates = await api.get('/api/job-templates');
    if (!Array.isArray(templates) || !templates.length) {
      api.toast?.('No job templates available', 'info');
      return;
    }
    // For now apply the first template; UI will be extended with a picker later.
    const tpl = templates[0];
    await api.post(`/api/jobs/${props.jobId}/apply-template`, { template_id: tpl.id }, {
      successMessage: `Template "${tpl.name}" applied`,
    });
    await loadJobDetail();
  } catch (err) {
    // toast handled by useApiWithToast
  }
}

async function savePart() {
  if (!addPartForm.value.part_id || addPartForm.value.quantity <= 0) {
    toast.add({ severity: "warn", summary: "Select part and quantity" });
    return;
  }
  addingPart.value = true;
  try {
    await api.post(`/api/mobile/jobs/${route.params.id}/parts-used`, {
      parts: [{ part_id: addPartForm.value.part_id, qty: Number(addPartForm.value.quantity) }],
    }, { successMessage: "Part recorded" });
    addPartDialog.value = false;
    await fetchCosting();
  } catch {
  } finally {
    addingPart.value = false;
  }
}

async function completeJob() {
  try {
    await api.patch(`/api/jobs/${route.params.id}`, { status: "Complete" }, { successMessage: "Job completed" });
    await fetchJob();
  } catch {
  }
}

// F-32 / 2026-04-29 — state override (warranty / un-complete / reactivate)
const showStateOverride = ref(false);
async function onStateOverrideApplied(payload) {
  // Warranty path returns the new child job — jump to it. Other paths
  // mutate this job in place — refetch.
  if (payload?.path === "warranty" && payload?.result?.id) {
    router.push(`/jobs/${payload.result.id}`);
    return;
  }
  await fetchJob();
}

function openEditDialog() {
  router.push(`/jobs?edit=${route.params.id}`);
}

function createInvoice() {
  const params = new URLSearchParams({
    customer_id: job.value.customer_id || "",
    job_id: job.value.id || route.params.id,
    action: "create",
  });
  router.push(`/billing?${params.toString()}`);
}

function openInstallSheet() {
  window.open(`/api/jobs/${route.params.id}/install-sheet`, "_blank");
}

async function fetchInstallData() {
  installLoading.value = true;
  try {
    const data = await api.get(`/api/jobs/${route.params.id}/install-specs`);
    installData.value = data?.data || data;
  } catch {
    installData.value = null;
  } finally {
    installLoading.value = false;
  }
}

function formatSpecLabel(key) {
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function createEstimate() {
  const params = new URLSearchParams({
    customer_id: job.value.customer_id || "",
    job_id: job.value.id || route.params.id,
    action: "create",
  });
  router.push(`/estimates?${params.toString()}`);
}

function startSignature(event) {
  isDrawing.value = true;
  const canvas = signatureCanvas.value;
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const x = (event.clientX - rect.left);
  const y = (event.clientY - rect.top);
  ctx.beginPath();
  ctx.moveTo(x, y);
}

function drawSignature(event) {
  if (!isDrawing.value) return;
  const canvas = signatureCanvas.value;
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const x = (event.clientX - rect.left);
  const y = (event.clientY - rect.top);
  ctx.lineTo(x, y);
  ctx.stroke();
}

function endSignature() {
  isDrawing.value = false;
}

function clearSignature() {
  const canvas = signatureCanvas.value;
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
}

async function saveSignature() {
  const canvas = signatureCanvas.value;
  if (!canvas) return;
  const dataUrl = canvas.toDataURL("image/png");
  try {
    await api.post(`/api/jobs/${route.params.id}/signature`, { signature: dataUrl }, { successMessage: "Signature saved" });
    signatureDialog.value = false;
    await fetchDocuments();
  } catch {
  }
}

function resizeCanvas() {
  const canvas = signatureCanvas.value;
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width;
  canvas.height = rect.height;
  const ctx = canvas.getContext("2d");
  ctx.lineWidth = 2;
  ctx.lineCap = "round";
  ctx.strokeStyle = "#111827";
}

watch(() => activeTab.value, (tab) => {
  if (tab === "install" && !installData.value && !installLoading.value) {
    fetchInstallData();
  }
});

onMounted(async () => {
  await fetchJob();
  nextTick(resizeCanvas);
});
</script>

<style scoped>
.specs-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 0.5rem; padding: 0.5rem; }
.spec-item { border: 1px solid var(--p-content-border-color, #334155); padding: 0.5rem; border-radius: 6px; }
.spec-label { font-size: 0.7rem; text-transform: uppercase; color: var(--p-text-muted-color, #94a3b8); display: block; }
.spec-value { font-weight: 700; font-size: 0.95rem; margin-top: 2px; }

.job-detail { max-width: 1200px; margin: 0 auto; }
.job-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem; flex-wrap: wrap; }
.job-title { margin: 0.5rem 0; display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
.job-subtitle { font-weight: 400; color: var(--p-text-muted-color); font-size: 0.9rem; }
.job-badges { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.header-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.stage-strip { display: flex; align-items: center; gap: 0.5rem; margin: 1rem 0; flex-wrap: wrap; }
.stage-btn { min-width: 140px; }
.stage-divider { flex: 1; height: 1px; background: var(--border); }
.job-tabs { margin-bottom: 1rem; }
.tab-panel { margin-top: 1rem; }
.details-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; }
.card { background: var(--surface-card); border-radius: 8px; padding: 1rem; border: 1px solid var(--border); }
.card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
.detail-row { display: flex; justify-content: space-between; padding: 0.4rem 0; border-bottom: 1px solid var(--border); }
.detail-row:last-child { border-bottom: none; }
.detail-text { max-width: 100%; }
.customer-info { display: flex; flex-direction: column; gap: 0.4rem; }
.customer-name { font-weight: 600; }
.customer-contact a { color: var(--p-primary-color); text-decoration: none; }
.access-notes { color: var(--p-text-muted-color); font-size: 0.85rem; }
.customer-notes { background: var(--p-content-hover-background); border-left: 3px solid var(--p-primary-color); padding: 0.5rem 0.75rem; border-radius: 4px; margin-top: 0.25rem; }
.customer-notes-label { font-size: 0.75rem; font-weight: 600; color: var(--p-text-muted-color); text-transform: uppercase; }
.customer-notes p { margin: 0.25rem 0 0 0; white-space: pre-wrap; font-size: 0.9rem; }
.diagnosis-card { border: 1px solid var(--surface-border, #e5e7eb); border-radius: 6px; padding: 0.75rem; margin-bottom: 0.75rem; }
.diagnosis-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; text-transform: capitalize; }
.diagnosis-field { display: grid; grid-template-columns: 180px 1fr; gap: 0.5rem; align-items: center; margin-bottom: 0.4rem; }
.diagnosis-field label { font-size: 0.85rem; color: var(--p-text-muted-color); }
.equipment-card .p-datatable-wrapper { max-height: 260px; }
.note-card .note-input { display: flex; flex-direction: column; gap: 0.75rem; }
.note-actions { display: flex; gap: 0.5rem; align-items: center; }
.notes-feed { margin-top: 1rem; display: flex; flex-direction: column; gap: 0.75rem; }
.note-entry { padding: 0.75rem; border: 1px solid var(--border); border-radius: 6px; }
.note-meta { font-size: 0.8rem; color: var(--p-text-muted-color); }
.note-body { margin: 0.4rem 0 0; }
.photo-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; }
.photo-card { border: 1px solid var(--border); border-radius: 8px; padding: 0.75rem; display: flex; gap: 0.75rem; }
.photo-preview { font-size: 2rem; }
.photo-meta { display: flex; flex-direction: column; gap: 0.25rem; }
.signature-card .signature-preview { display: flex; justify-content: space-between; align-items: center; }
.signature-canvas-wrap { width: 100%; min-height: 240px; border: 1px dashed var(--border); border-radius: 8px; padding: 0.5rem; background: var(--surface-subtle); }
.signature-canvas { width: 100%; height: 220px; display: block; }
.signature-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 0.75rem; }
.costing-summary .costing-values { display: flex; flex-wrap: wrap; gap: 0.5rem; font-size: 0.85rem; }
.parts-card .p-datatable-wrapper, .time-entry-card .p-datatable-wrapper { max-height: 320px; }
.activity-list { list-style: none; padding: 0; margin: 0; }
.activity-row { display: flex; gap: 0.75rem; padding: 0.75rem 0; border-bottom: 1px solid var(--border); }
.activity-row:last-child { border-bottom: none; }
.activity-symbol { width: 12px; height: 12px; background: var(--accent-b); border-radius: 50%; margin-top: 4px; }
.activity-text { font-size: 0.9rem; margin: 0; }
.activity-meta { margin: 0.2rem 0 0; font-size: 0.8rem; color: var(--p-text-muted-color); }
.dialog-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1rem; }
.muted { color: var(--p-text-muted-color); }
.spinner-wrap.small { display: flex; justify-content: center; padding: 1rem; }
/* S97 slice 6 — multi-tech assignment chips */
.assignments-block { display: flex; flex-direction: column; gap: 0.5rem; width: 100%; }
.assignment-chips { display: flex; flex-wrap: wrap; gap: 0.4rem; }
.assignment-chip {
  display: inline-flex; align-items: center; gap: 0.4rem;
  padding: 0.25rem 0.6rem; border-radius: 999px;
  background: var(--surface-card, #2a2f3a);
  border: 1px solid var(--surface-border, #3b424f);
  font-size: 0.85rem;
}
.assignment-chip.is-lead {
  border-color: var(--accent-a, #f7b32b);
  background: color-mix(in srgb, var(--accent-a, #f7b32b) 12%, transparent);
}
.assignment-chip .pi-star-fill { color: var(--accent-a, #f7b32b); }
.assignment-name { font-weight: 500; }
.assignment-add-row { display: flex; gap: 0.4rem; align-items: center; }
.assignment-add-row .p-select { flex: 1; }
</style>
