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
            <Tag
              v-if="job.is_return_visit"
              value="RETURN VISIT"
              severity="warn"
              data-testid="job-detail-return-visit"
              v-tooltip.bottom="'Spawned as a return trip from a completed job — not fresh work'"
            />
            <!-- 055: the RFB dismiss mark. The queue only shows unmarked
                 jobs, so this tag is the one place a wrong mark is visible —
                 and therefore where it gets undone. -->
            <Tag
              v-if="job.not_billable_at"
              value="NOT BILLABLE"
              severity="danger"
              data-testid="job-detail-not-billable"
              style="cursor: pointer"
              v-tooltip.bottom="`${job.not_billable_reason || 'no reason recorded'} — click to make billable again`"
              @click="makeBillable"
            />
          </div>
        </div>
        <div class="header-actions">
          <Button label="Edit" icon="pi pi-pencil" aria-label="Edit" severity="secondary" @click="openEditDialog" />
          <Button v-if="job.status !== 'Complete' && job.status !== 'Invoiced'"
            label="Complete Job" icon="pi pi-check" severity="success"
            @click="completeJob" data-testid="job-detail-complete" />
          <!-- 2026-07-23 deposit/progress billing: invoicing is no longer
               gated on completion — deposits and progress invoices happen
               mid-job. Green when Complete (the normal moment), muted
               otherwise so mid-job invoicing reads as the exception. -->
          <Button
            label="Create Invoice" icon="pi pi-dollar"
            :severity="job.status === 'Complete' ? 'success' : 'secondary'"
            :outlined="job.status !== 'Complete'"
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
          <!-- Doug 2026-08-11: "there is no way of deleting a job when in the
               job page." Delete existed only in the Jobs list row actions and
               the Ready-for-Billing queue — from the job itself, the one verb
               that removes the job was missing. Office-only (same `patchable`
               gate the assignment edits use) and pushed to its own end of the
               row so it isn't a neighbour-miss on Install Sheet. -->
          <Button v-if="patchable"
            label="Delete" icon="pi pi-trash" severity="danger" outlined
            class="delete-job-btn"
            @click="deleteJob" data-testid="job-detail-delete" />
        </div>
      </div>

      <div class="stage-strip">
        <!-- The active "Scheduled" pill relabels to "Awaiting Schedule" when the
             job has no appointment date — a green "Scheduled" pill on a dateless
             job reads as booked. Click semantics + testids stay keyed to the
             canonical stage name. -->
        <Button v-for="stage in stageButtons" :key="stage"
          :label="stage === 'Scheduled' && awaitingSchedule ? 'Awaiting Schedule' : stage"
          :severity="stageSeverity(stage)"
          :rounded="true"
          :outlined="stage !== job.status"
          class="stage-btn"
          :data-testid="`job-detail-stage-${stage.toLowerCase().replace(/\s+/g, '-')}`"
          @click="applyStage(stage)" />
        <span class="stage-divider"></span>
        <Button :label="job.scheduled_at ? 'Reschedule' : 'Schedule'" icon="pi pi-calendar" severity="info"
          @click="openSchedule()" data-testid="job-detail-schedule" />
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
          <Tab value="email">Email</Tab>
          <Tab value="activity">Activity</Tab>
        </TabList>
      </Tabs>

      <div v-if="activeTab === 'details'" class="tab-panel">
        <!-- Live invoices, front and center (Doug 2026-08-07): the closeout
             autodraft means every closed-out job already HAS an invoice,
             but the Invoices table hides two clicks deep in the Costing
             tab. One row per live invoice; drafts get the Review verb and
             land on the editable invoice page. -->
        <div v-if="liveInvoices.length" class="card invoice-strip" data-testid="job-invoice-strip">
          <div v-for="inv in liveInvoices" :key="inv.id" class="invoice-strip-row">
            <span class="invoice-strip-id">
              <strong>{{ inv.invoice_number }}</strong>
              <Tag v-if="inv.billing_type === 'deposit'" value="deposit" severity="info" />
              <Tag :value="inv.status" :severity="invoiceStatusSeverity(inv.status)" />
            </span>
            <span class="invoice-strip-money">
              {{ formatCurrency(inv.total) }}
              <small v-if="Number(inv.balance_due) > 0" class="muted">· {{ formatCurrency(inv.balance_due) }} due</small>
            </span>
            <Button
              :label="inv.status === 'draft' ? 'Review invoice' : 'Open invoice'"
              :icon="inv.status === 'draft' ? 'pi pi-file-edit' : 'pi pi-external-link'"
              :severity="inv.status === 'draft' ? 'success' : 'secondary'"
              size="small"
              data-testid="job-invoice-strip-open"
              @click="openInvoice(inv.id)"
            />
          </div>
        </div>
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
              <strong>{{ lifecycleStageDisplay }}</strong>
            </div>
            <div class="detail-row">
              <span>Scheduled For</span>
              <strong>{{ job.scheduled_at ? formatDate(job.scheduled_at) : 'Not yet scheduled' }}</strong>
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

        <!-- Job dependencies (Tier-2 UI door): blocking relationships existed
             server-side only — nothing could set or even see them. -->
        <div class="card">
          <div class="card-header">
            <h3>Blocked by</h3>
          </div>
          <div class="receipt-input">
            <Select
              v-model="newDependencyJobId"
              :options="dependencyJobOptions"
              optionLabel="label"
              optionValue="value"
              filter
              placeholder="This job can't start until..."
              class="dependency-select"
              data-testid="dependency-select"
            />
            <Button label="Add" icon="pi pi-plus" :disabled="!newDependencyJobId" data-testid="dependency-add-btn" @click="addDependency" />
          </div>
          <DataTable v-if="dependencies.length" :value="dependencies" striped-rows data-testid="dependencies-table">
            <Column field="depends_on_title" header="Job">
              <template #body="{ data }">
                <router-link :to="`/jobs/${data.depends_on_job_id}`">{{ data.depends_on_title || data.depends_on_job_id }}</router-link>
              </template>
            </Column>
            <Column field="depends_on_status" header="Status" />
            <Column header="" style="width: 4rem">
              <template #body="{ data }">
                <Button v-tooltip="'Remove dependency'" icon="pi pi-times" aria-label="Remove dependency" text severity="secondary" data-testid="dependency-remove-btn" @click="removeDependency(data)" />
              </template>
            </Column>
          </DataTable>
          <p v-else class="muted">No blocking jobs.</p>
        </div>

        <!-- Plan §1 — the closeout card. job_closeouts was WRITE-ONLY for 2.5
             months: the tech's attested hours, work notes, parts attestation
             and signer name went into the database and never reached a
             screen, so the office billed blind. This card is what the office
             reads to bill. Never renders the raw signature blob (audit A4) —
             the API doesn't even send it. -->
        <div v-if="closeout" class="card" data-testid="job-closeout-card">
          <div class="card-header">
            <h3>Closeout — work performed</h3>
          </div>
          <div class="detail-row">
            <span>Hours attested</span>
            <strong data-testid="closeout-hours">{{ Number(closeout.hours_worked).toFixed(2) }} h</strong>
          </div>
          <div class="detail-row">
            <span>Parts</span>
            <strong data-testid="closeout-parts">
              {{ closeout.no_parts_used ? 'Tech attested: no parts used' : `${(closeout.parts_used || []).length} part line(s)` }}
            </strong>
          </div>
          <div v-if="(closeout.parts_used || []).length" class="closeout-parts-list">
            <div v-for="(p, i) in closeout.parts_used" :key="i" class="detail-row">
              <span>{{ p.name || p.sku || 'part' }}</span>
              <strong>× {{ p.qty }}</strong>
            </div>
          </div>
          <div v-if="closeout.notes" class="detail-row closeout-notes">
            <span>Work notes</span>
            <strong data-testid="closeout-notes">{{ closeout.notes }}</strong>
          </div>
          <div class="detail-row">
            <span>Signature</span>
            <strong data-testid="closeout-signature">
              {{ closeout.signature_present ? `Signed${closeout.signed_by ? ' by ' + closeout.signed_by : ''}` : 'Not signed' }}
            </strong>
          </div>
          <div class="detail-row">
            <span>Closed out</span>
            <strong>{{ formatDateTime(closeout.closed_at) }}{{ closeout.closed_by_name ? ' — ' + closeout.closed_by_name : '' }}</strong>
          </div>
          <!-- Revision history (plan §14 gap 4): the supersede model keeps
               every attestation; the office must SEE that a closeout was
               restated, or the model is invisible. -->
          <div v-if="closeoutHistory.length > 1" class="closeout-history" data-testid="closeout-history">
            <div class="card-header"><h4>Revisions</h4></div>
            <div v-for="h in closeoutHistory" :key="h.id" class="detail-row">
              <span>{{ formatDateTime(h.closed_at) }}{{ h.closed_by_name ? ' — ' + h.closed_by_name : '' }}</span>
              <strong>
                {{ Number(h.hours_worked).toFixed(2) }} h
                <Tag v-if="!h.superseded_at" value="current" severity="success" style="margin-left: .4rem" />
                <Tag v-else value="superseded" severity="secondary" style="margin-left: .4rem" />
              </strong>
            </div>
          </div>
        </div>
      </div>

      <div v-else-if="activeTab === 'schedule'" class="tab-panel">
        <div class="card">
          <div class="card-header">
            <h3>Appointments</h3>
            <div style="display:flex; gap:0.5rem; align-items:center;">
              <Button label="Open calendar" icon="pi pi-external-link" severity="secondary" outlined
                @click="openAppointmentsPage" data-testid="job-detail-open-appointments" />
              <Button :label="job.scheduled_at ? 'Reschedule' : 'Schedule'" icon="pi pi-calendar" severity="info"
                @click="openSchedule()" data-testid="job-detail-schedule-tab" />
            </div>
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
            <Column header="Expense">
              <template #body="{ data }">
                <!-- Tier-2 UI door (2026-07): receipts could never become
                     expense records from the UI — the promote endpoint had
                     zero callers, breaking the receipt→bookkeeping handoff. -->
                <Tag v-if="data.promoted_expense_id" value="Promoted" severity="success" data-testid="receipt-promoted-tag" />
                <Button
                  v-else-if="Number(data.amount) > 0"
                  v-tooltip="'Create an expense record from this receipt'"
                  label="Promote"
                  icon="pi pi-arrow-up-right"
                  text
                  size="small"
                  :loading="promotingReceiptId === data.id"
                  data-testid="receipt-promote-btn"
                  @click="promoteReceipt(data)"
                />
                <span v-else v-tooltip="'Add an amount before promoting'" class="muted">no amount</span>
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
          <!-- Three distinct states, because they need three different
               actions from the person reading them. This tab said "No photos
               yet" for every photo from every source since it shipped (it
               filtered documents on `entity_type`, which the API does not
               send), so an empty box here has to be trustworthy now. -->
          <div v-if="photosLoading" class="empty-state"><p>Loading photos…</p></div>
          <div v-else-if="photosError" class="empty-state" data-testid="job-photos-error">
            <p>{{ photosError }}</p>
            <Button label="Retry" icon="pi pi-refresh" text @click="fetchJobPhotos" />
          </div>
          <div v-else-if="jobPhotos.length === 0" class="empty-state" data-testid="job-photos-empty">
            <p>No photos on this job yet.</p>
          </div>
          <div v-else class="photo-grid" data-testid="job-photos-grid">
            <div v-for="photo in jobPhotos" :key="photo.id" class="photo-card">
              <!-- The photo itself, not an emoji. AuthedImage fetches the
                   bytes with the Bearer token a bare <img src> cannot send. -->
              <button type="button" class="photo-thumb-btn" @click="openPhoto(photo)">
                <AuthedImage :src="photo.url" :alt="photo.caption || photo.kind || 'Job photo'" class="photo-thumb">
                  <template #fallback>
                    <span class="photo-thumb-failed" data-testid="job-photo-failed">
                      <i class="pi pi-exclamation-triangle" /> Image unavailable
                    </span>
                  </template>
                </AuthedImage>
              </button>
              <div class="photo-meta">
                <Tag v-if="photo.kind" :value="photo.kind" severity="info" />
                <p v-if="photo.caption" class="photo-name">{{ photo.caption }}</p>
                <p class="photo-date">{{ formatDateTime(photo.uploaded_at) }}</p>
                <p v-if="photo.uploaded_by" class="photo-date">{{ photo.uploaded_by }}</p>
                <!-- Share with the customer, per photo, default OFF (Doug
                     2026-08-12). A tech photographs damage found on arrival,
                     hazards and other people's messes too; none of that should
                     reach the customer because someone pressed the shutter.
                     Sharing here shows the photo in their portal; attaching it
                     to an invoice shares it as well (same flag, one decision). -->
                <label class="photo-share" :data-testid="`job-photo-share-${photo.id}`">
                  <input
                    type="checkbox"
                    :checked="photo.customer_visible"
                    :disabled="photoSharing === photo.id"
                    @change="togglePhotoShare(photo)"
                  />
                  <span :class="{ 'photo-share-on': photo.customer_visible }">
                    {{ photo.customer_visible ? 'Customer can see this' : 'Internal only' }}
                  </span>
                </label>
                <Button v-tooltip="'Download photo'" icon="pi pi-download" aria-label="Download photo" text @click="downloadPhoto(photo)" />
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
            <Button label="Add from Catalog" icon="pi pi-book" severity="info" :loading="addingCatalogParts" @click="catalogPickerVisible = true" data-testid="job-detail-add-catalog" />
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

          <!-- Parts recorded as USED on the job — the billing checklist, which
               is a different plane from the cost table above: it carries
               free-text parts that were never inventory rows, and it's what
               the invoice is built from. Split out 2026-08-12: every one of
               these rows used to render under "To order" below, so a part the
               tech had already installed read as an outstanding order. -->
          <div v-if="partsUsedChecklist.length" class="parts-to-order-block" data-testid="job-detail-parts-used-checklist">
            <h4 class="parts-subhead">Recorded as used ({{ partsUsedChecklist.length }})</h4>
            <DataTable :value="partsUsedChecklist" striped-rows responsive-layout="scroll" class="table-small">
              <Column field="part_name" header="Part" />
              <Column field="sku" header="SKU" />
              <Column field="quantity" header="Qty" style="width: 70px" />
              <Column header="Sell price" style="width: 110px">
                <template #body="{ data }">{{ data.unit_price != null ? formatCurrency(data.unit_price) : '—' }}</template>
              </Column>
              <Column header="Recorded" style="width: 130px">
                <template #body="{ data }">
                  <Tag :value="partSourceLabel(data.source)" :severity="data.billed_invoice_id ? 'success' : 'info'" />
                </template>
              </Column>
            </DataTable>
          </div>

          <!-- Parts queued to order (the "+ Order Part" / "Add from Catalog"
               flow). Distinct from Parts Used above (consumption); these carry
               a suggested sell price that flows into the invoice checklist. -->
          <div class="parts-to-order-block" data-testid="job-detail-parts-to-order">
            <h4 class="parts-subhead">To order ({{ partsToOrder.length }})</h4>
            <div v-if="!partsToOrder.length" class="muted">Nothing queued to order.</div>
            <DataTable v-else :value="partsToOrder" striped-rows responsive-layout="scroll" class="table-small">
              <Column field="part_name" header="Part" />
              <Column field="sku" header="SKU" />
              <Column field="quantity" header="Qty" style="width: 70px" />
              <Column header="Sell price" style="width: 110px">
                <template #body="{ data }">{{ data.unit_price != null ? formatCurrency(data.unit_price) : '—' }}</template>
              </Column>
              <Column field="status" header="Status" style="width: 110px">
                <template #body="{ data }"><Tag :value="data.status" severity="info" /></template>
              </Column>
            </DataTable>
          </div>
        </div>

        <CatalogPickerDialog v-model:visible="catalogPickerVisible" @add="addCatalogParts" />
        <div class="card dispatch-status-card">
          <div class="card-header">
            <h3>Dispatch status</h3>
          </div>
          <p class="dispatch-status-value">{{ job?.dispatch_status || job?.status || '—' }}</p>
        </div>
        <div class="card time-entry-card">
          <div class="card-header"><h3>Time Entries</h3></div>
          <!-- Plan §3: three display bugs fixed. The Tech column bound
               `technician_name`, which _entry_to_dict never returned (always
               blank). Clock In/Out used `:body="formatDateTime"` as a PROP —
               PrimeVue's `body` is a SLOT, so the formatter was ignored and
               raw ISO strings rendered. And a closeout row's clock_out is
               stamped in the FUTURE (clock_in + attested_minutes), which reads
               as "still working"; show a dash for it and trust the Hours
               column (the attested truth) instead. -->
          <DataTable :value="timeEntries" striped-rows responsive-layout="scroll" emptyMessage="No time entries yet">
            <Column header="Tech">
              <template #body="{ data }">{{ data.tech_name || data.technician_name || '—' }}</template>
            </Column>
            <Column header="Clock In">
              <template #body="{ data }">{{ data.clock_in ? formatDateTime(data.clock_in) : '—' }}</template>
            </Column>
            <Column header="Clock Out">
              <template #body="{ data }">
                {{ isFutureStamp(data.clock_out) ? '—' : (data.clock_out ? formatDateTime(data.clock_out) : '—') }}
              </template>
            </Column>
            <Column header="Hours">
              <template #body="{ data }">{{ formatHours(data.duration_minutes) }}</template>
            </Column>
            <Column header="Cost">
              <template #body="{ data }">{{ formatCurrency(data.labor_cost) }}</template>
            </Column>
          </DataTable>
        </div>
        <div v-if="financials" class="card financials-card" data-testid="job-financials-card">
          <div class="card-header"><h3>Financials</h3></div>
          <div style="display:flex; flex-wrap:wrap; gap:2rem; padding:.5rem 0;">
            <div>
              <div class="muted" style="font-size:.8rem;">Invoiced</div>
              <div style="font-weight:600;" data-testid="fin-invoiced">{{ formatCurrency(financials.invoiced_total) }}</div>
            </div>
            <div v-if="financials.deposit_total">
              <div class="muted" style="font-size:.8rem;">Deposit (paid / requested)</div>
              <div style="font-weight:600;" data-testid="fin-deposit">
                {{ formatCurrency(financials.deposit_paid) }} / {{ formatCurrency(financials.deposit_total) }}
              </div>
            </div>
            <div>
              <div class="muted" style="font-size:.8rem;">Paid</div>
              <div style="font-weight:600;" data-testid="fin-paid">{{ formatCurrency(financials.paid_total) }}</div>
            </div>
            <div>
              <div class="muted" style="font-size:.8rem;">Balance Due</div>
              <div style="font-weight:600;" data-testid="fin-balance">{{ formatCurrency(financials.balance_due) }}</div>
            </div>
            <!-- Retroactive deposit (2026-07-23): jobs whose estimate was
                 accepted before the deposit feature get a door here too. -->
            <div v-if="!financials.deposit_total && acceptedEstimate" style="align-self:center;">
              <Button label="Collect Deposit" icon="pi pi-wallet" size="small" severity="info" outlined
                :loading="collectingDeposit" data-testid="job-collect-deposit" @click="collectDeposit" />
            </div>
          </div>
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
            <Column field="invoice_number" header="Invoice">
              <template #body="{ data }">
                {{ data.invoice_number }}
                <Tag v-if="data.billing_type === 'deposit'" value="deposit" severity="info" />
              </template>
            </Column>
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
          <!-- Door Specs — captured doors as a by-size clickable list (a job can
               carry several; each opens to its own build spec). -->
          <div v-if="installData.doors && installData.doors.length" class="card" style="margin-bottom:1rem">
            <div class="card-header"><h3>Door Specifications</h3></div>
            <DoorSpecList :doors="installData.doors" />
          </div>
          <!-- Fallback: a non-CHI catalogued door (single flat spec). -->
          <div v-else-if="installData.door_specs" class="card" style="margin-bottom:1rem">
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

      <!-- Email tab (P2.1) — correspondence the tagger linked to this job.
           Mounts only when open, so the job page pays nothing for it
           otherwise. -->
      <div v-else-if="activeTab === 'email'" class="tab-panel">
        <div class="card">
          <div class="card-header">
            <h3>Email</h3>
            <!-- P2.5 — composing FROM the job is what stamps the
                 [Job #<uuid>] subject marker, so the customer's reply links
                 itself back to this job without anyone tagging it by hand. -->
            <Button
              label="Email about this job"
              icon="pi pi-envelope"
              size="small"
              outlined
              data-testid="job-email-compose"
              @click="composeEmailAboutJob"
            />
          </div>
          <EmailTimeline :job-id="route.params.id" />
        </div>

        <!-- P2.3 — where "Save to job" actually lands. The Photos tab filters
             on entity_type, which DocumentOut doesn't carry, so a saved
             attachment would otherwise be invisible on the job it was filed
             to: a button that says "Saved to the job" and shows nothing. -->
        <div class="card" data-testid="job-files-card">
          <div class="card-header"><h3>Files on this job</h3></div>
          <div v-if="!jobFiles.length" class="muted" style="padding:0.5rem">
            No files yet. Saving an email attachment to this job puts it here.
          </div>
          <ul v-else class="job-file-list">
            <li v-for="doc in jobFiles" :key="doc.id" class="job-file-row">
              <i class="pi pi-file" aria-hidden="true" />
              <span class="job-file-name">{{ doc.original_name || doc.title }}</span>
              <span class="job-file-meta">{{ formatDateTime(doc.uploaded_at || doc.created_at) }}</span>
              <Button v-tooltip="'Download'" icon="pi pi-download" aria-label="Download file" text @click="downloadDocument(doc.id)" />
            </li>
          </ul>
        </div>
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

    <!-- Schedule / Reschedule. Writes the JOB (scheduled_at + crew); the
         backend mirrors that into the appointments calendar. -->
    <Dialog v-model:visible="scheduleDialog" :header="job.scheduled_at ? 'Reschedule Job' : 'Schedule Job'"
      modal :style="{ width: '480px' }" data-testid="job-schedule-dialog">
      <div class="schedule-form">
      <div class="form-field">
        <label for="schedule-date">Date &amp; time</label>
        <DatePicker
          id="schedule-date"
          v-model="scheduleForm.scheduled_at"
          showTime
          hourFormat="12"
          dateFormat="mm/dd/yy"
          showIcon
          showButtonBar
          class="w-full"
          data-testid="job-schedule-date"
        />
        <small class="muted">Clearing the date returns the job to Service Call and takes it off the calendar.</small>
      </div>
      <div class="form-field">
        <label for="schedule-techs">Technician(s)</label>
        <MultiSelect
          id="schedule-techs"
          v-model="scheduleForm.tech_ids"
          :options="techOptions"
          optionLabel="label"
          optionValue="value"
          placeholder="Assign a tech"
          display="chip"
          filter
          class="w-full"
          data-testid="job-schedule-techs"
        />
        <!-- Two different outcomes, so two different warnings: with the tenant
             hard gate on the server returns 422 and refuses the save outright;
             without it the job saves and waits in the dispatch lane. -->
        <small v-if="scheduleForm.scheduled_at && !scheduleForm.tech_ids.length" class="schedule-warn"
          data-testid="job-schedule-no-tech-warning">
          <template v-if="dispatchSettings.dispatch_block_save_no_tech">
            A technician is required for scheduled jobs on this account — pick one to save.
          </template>
          <template v-else>
            No tech assigned — this job will wait in the dispatch queue until someone is picked.
          </template>
        </small>
      </div>
      <div class="form-field">
        <label for="schedule-duration">Estimated time (hours)</label>
        <InputText
          id="schedule-duration"
          v-model="scheduleForm.duration_hours"
          type="number"
          step="0.25"
          min="0"
          placeholder="e.g. 1.5"
          class="w-full"
          data-testid="job-schedule-duration"
        />
        <small class="muted">Drives the dispatch capacity bars. Leave blank to size it from the labor matrix.</small>
      </div>
      <p v-if="scheduleError" class="schedule-error" data-testid="job-schedule-error">{{ scheduleError }}</p>
      <div class="dialog-actions">
        <Button label="Cancel" severity="secondary" text @click="scheduleDialog = false" />
        <Button label="Save" icon="pi pi-check" severity="success" :loading="savingSchedule"
          @click="saveSchedule" data-testid="job-schedule-save" />
      </div>
      </div>
    </Dialog>

    <!-- Records a part as USED on this job, now — not at closeout. Inventory
         rows decrement stock; anything not stocked records by name, the same
         way a free-text closeout line does. Before 2026-08-12 the name path
         didn't exist, so a part that wasn't in inventory could not be recorded
         here at all. -->
    <Dialog v-model:visible="addPartDialog" header="Record Part Used" modal :style="{ width: '420px' }">
      <div class="form-field">
        <label>Part from inventory</label>
        <Select
          v-model="addPartForm.part_id"
          :options="inventoryOptions"
          optionLabel="label"
          optionValue="value"
          placeholder="Select a part"
          filter
          show-clear
          data-testid="add-part-select"
        />
      </div>
      <div v-if="!addPartForm.part_id" class="form-field">
        <label>Or type the part *</label>
        <InputText
          v-model="addPartForm.name"
          placeholder="e.g. Torsion spring 2in 27c"
          data-testid="add-part-name"
        />
      </div>
      <div v-if="!addPartForm.part_id" class="form-field">
        <label>SKU (optional)</label>
        <InputText v-model="addPartForm.sku" placeholder="if known" data-testid="add-part-sku" />
      </div>
      <div class="form-field">
        <label>Quantity</label>
        <InputText v-model="addPartForm.quantity" type="number" min="1" />
      </div>
      <div class="dialog-actions">
        <Button label="Cancel" severity="secondary" text @click="addPartDialog = false" />
        <Button
          label="Record"
          severity="success"
          :disabled="!addPartForm.part_id && !addPartForm.name.trim()"
          @click="savePart"
          :loading="addingPart"
          data-testid="add-part-submit"
        />
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
        <PhoneInput v-model="customerEditForm.phone" data-testid="customer-edit-phone" />
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
import { useDestructiveConfirm } from "../composables/useDestructiveConfirm";
import { formatDate, formatDateTime, formatMoney, formatMoney as formatCurrency, formatPercent as fmtPercent, formatPhone } from "../composables/useFormatters";
import { useToast } from "primevue/usetoast";
import { useAuthStore } from "../stores/auth";
import { isTechnician as isTechRole } from "../constants/roles";
import { appointmentStatusSeverity, estimateStatusSeverity } from "../utils/statusSeverity";
import { isAwaitingSchedule } from "../utils/jobDisplayState";
import Button from "primevue/button";
import Tabs from "primevue/tabs";
import TabList from "primevue/tablist";
import Tab from "primevue/tab";
import Select from "primevue/select";
import MultiSelect from "primevue/multiselect";
import DatePicker from "primevue/datepicker";
import InputText from "primevue/inputtext";
import Textarea from "primevue/textarea";
import DataTable from "primevue/datatable";
import Column from "primevue/column";
import FileUpload from "primevue/fileupload";
import Dialog from "primevue/dialog";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import JobStateChip from "../components/JobStateChip.vue";
import AuthedImage from "../components/AuthedImage.vue";
import CatalogPickerDialog from "../components/CatalogPickerDialog.vue";
import DoorSpecList from "../components/DoorSpecList.vue";
import PhoneInput from "../components/PhoneInput.vue";
import EmailTimeline from "../components/EmailTimeline.vue";

const route = useRoute();
const router = useRouter();
const api = useApiWithToast();
const { confirmAsync } = useDestructiveConfirm();
const toast = useToast();
const auth = useAuthStore();

const job = ref({});
const loading = ref(true);
const error = ref("");
const activeTab = ref("details");
const relatedEstimates = ref([]);
const relatedInvoices = ref([]);
// Details-tab invoice strip: live invoices only — void is dead money and
// has no place in the "what's billed on this job" headline.
const liveInvoices = computed(() =>
  (relatedInvoices.value || []).filter((i) => i.status !== "void")
);
const financials = ref(null);
const appointments = ref([]);
const appointmentsLoading = ref(false);
const timeEntries = ref([]);
// A closeout-written labor row closes at clock_in + attested_minutes, which
// can land in the future; don't render that as a real clock-out time.
function isFutureStamp(iso) {
  if (!iso) return false;
  const t = new Date(iso).getTime();
  return Number.isFinite(t) && t > Date.now();
}
// Plan §1 — the closeout card's data. `closeout` = the CURRENT snapshot
// (null until the job is closed out); `closeoutHistory` = every attestation
// newest-first, so a restated closeout is visible as a revision trail.
const closeout = ref(null);
const closeoutHistory = ref([]);
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
const promotingReceiptId = ref(null);
const dependencies = ref([]);
const dependencyJobOptions = ref([]);
const newDependencyJobId = ref(null);
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
// Parts queued to order for this job (the "+ Order Part" / "Add from Catalog"
// flow writes here). Rendered under the Parts card so the queue is visible on
// the job — previously these rows only surfaced in Parts-to-Order + the
// invoice checklist.
const partsNeeded = ref([]);
// job_parts_needed carries two different facts under one endpoint: parts still
// owed to the job (needed/ordered/received) and parts already consumed
// (used). Rendering them in one "To order" table labelled every installed part
// as an outstanding order — wrong on the screen the office bills from.
const partsToOrder = computed(() =>
  partsNeeded.value.filter((p) => p.status !== "used" && p.status !== "wont_bill"),
);
const partsUsedChecklist = computed(() =>
  partsNeeded.value.filter((p) => p.status === "used"),
);
// Where the used row came from, in the office's words rather than the
// database's. 'van' = pulled off a truck's stock, 'mobile' = the tech logged it
// on the job, 'closeout' = attested at completion.
const PART_SOURCE_LABELS = {
  mobile: "on the job",
  closeout: "at closeout",
  van: "van stock",
  request: "requested",
};
function partSourceLabel(source) {
  return PART_SOURCE_LABELS[source] || source || "recorded";
}
const catalogPickerVisible = ref(false);
const addingCatalogParts = ref(false);
const addingPart = ref(false);
// Schedule/Reschedule dialog — see openSchedule() for why this writes the
// job row rather than creating an appointment.
const scheduleDialog = ref(false);
const savingSchedule = ref(false);
const scheduleError = ref("");
const scheduleForm = ref({ scheduled_at: null, tech_ids: [], duration_hours: "" });
// Tenant dispatch policy — decides whether "scheduled with no tech" is a
// warning or a hard 422. Defaults to the permissive shape so a failed read
// never blocks the dialog.
const dispatchSettings = ref({ dispatch_block_save_no_tech: false });
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
// A job in the "Scheduled" pipeline stage with no appointment date (e.g. a
// converted estimate awaiting door delivery) is sold and on the board but NOT
// on the calendar — the bare word "Scheduled" reads, at a glance, as "has an
// appointment". The one rule lives in utils/jobDisplayState.isAwaitingSchedule
// (also drives the JobStateChip header badge); here it relabels the Lifecycle
// Stage row and the stage-strip pill. Display-only: the stored lifecycle_stage
// stays "scheduled" (load-bearing for the scheduling board, recommender, and
// MCP list — do NOT change the underlying value).
const awaitingSchedule = computed(() => isAwaitingSchedule(job.value));
const lifecycleStageDisplay = computed(() => {
  if (awaitingSchedule.value) return "Awaiting Schedule";
  return job.value.lifecycle_stage || job.value.status || "Unknown";
});
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
// Job photos come from job_photos — the record every other photo surface
// already reads (the Photos page, the invoice picker, the mobile job screen).
// See core/job_photos.py: "Documents hold the bytes; job_photos is the photo."
//
// This tab used to filter `documents` on `doc.entity_type === "job_photo"`, a
// field DocumentOut has never serialized, so the filter was never true and the
// tab was empty for every photo ever taken. Worse, it was structurally
// unreachable: the only writer that sets entity_type='job_photo' does not set
// documents.job_id, and this page queries documents BY job_id — so no row could
// satisfy both conditions at once. Reading the photo record ends the whole
// class of bug rather than patching the filter.
const jobPhotos = ref([]);
const photosLoading = ref(false);
const photosError = ref("");
// id of the photo whose share state is mid-flight, so its checkbox can't be
// double-fired while the PATCH is in the air.
const photoSharing = ref(null);

/**
 * Share a photo with the customer, or take it back.
 *
 * Optimistic on purpose — a checkbox that waits on the network reads as
 * broken — but it ROLLS BACK on failure. Silently keeping the new state after
 * a failed PATCH would tell the office a customer can see a photo they can't,
 * or worse, that an internal photo is withheld when it is still shared.
 */
async function togglePhotoShare(photo) {
  if (photoSharing.value) return;
  const next = !photo.customer_visible;
  photoSharing.value = photo.id;
  photo.customer_visible = next;
  try {
    await api.patch(
      `/api/jobs/${route.params.id}/photos/${photo.id}`,
      { customer_visible: next },
      { successMessage: next ? "Shared with the customer" : "Hidden from the customer" },
    );
  } catch {
    photo.customer_visible = !next;
  } finally {
    photoSharing.value = null;
  }
}
// Everything filed on this job that isn't a photo or the signature — which is
// where an email attachment saved with "Save to job" lands.
const jobFiles = computed(() =>
  documents.value.filter(
    (doc) => doc.entity_type !== "job_photo" && doc.entity_type !== "job_signature",
  ),
);
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
  // Active "Scheduled" with no date: amber, not green — it needs a date,
  // it isn't booked. Pairs with the "Awaiting Schedule" relabel above.
  if (stage === "Scheduled" && stage === job.value.status && awaitingSchedule.value) return "warn";
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
  return appointmentStatusSeverity(status);
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

// 055: undo for Billing's "Not billable" verb. Not destructive (the job just
// re-enters Ready for Billing), but confirmed so a stray click on the tag
// doesn't silently refill the queue.
async function makeBillable() {
  const ok = await confirmAsync({
    header: "Make billable again?",
    icon: "pi pi-ban",
    message: `This job returns to Ready for Billing. (Marked not billable: ${job.value?.not_billable_reason || "no reason recorded"})`,
    acceptLabel: "Make billable",
    acceptClass: "p-button-primary",
  });
  if (!ok) return;
  try {
    await api.del(`/api/jobs/${job.value.id}/not-billable`);
    toast.add({ severity: "success", summary: "Billable again", detail: "Job returned to Ready for Billing", life: 3000 });
    await fetchJob();
  } catch (e) {
    toast.add({ severity: "error", summary: "Error", detail: e.message || "Failed to update", life: 4000 });
  }
}

// Doug 2026-08-11: the job page carried every verb except the one that
// removes the job. Same soft-delete the Jobs list trash icon calls —
// DELETE /api/jobs/{id} stamps deleted_at and cascades to the mirrored
// appointment, so the job leaves the list, the board and the schedule
// together. It does NOT cascade to invoices or estimates, so when the job
// carries live invoices the confirm says so out loud rather than letting
// them quietly orphan in Billing.
async function deleteJob() {
  if (!job.value?.id) return;
  const n = liveInvoices.value.length;
  const invoiceWarning = n
    ? ` ${n === 1 ? "Invoice" : "Invoices"} ${liveInvoices.value.map((i) => i.invoice_number).join(", ")} ${n === 1 ? "stays" : "stay"} in Billing — void ${n === 1 ? "it" : "them"} separately if the work isn't happening.`
    : "";
  const label = `Job #${job.value.job_number || String(job.value.id).slice(0, 8)}`;
  const ok = await confirmAsync({
    header: "Delete this job?",
    message: `${label}${job.value.title ? ` — ${job.value.title}` : ""} will be removed from the Jobs list, the dispatch board and the schedule.${invoiceWarning}`,
    acceptLabel: "Delete job",
  });
  if (!ok) return;
  try {
    await api.del(`/api/jobs/${job.value.id}`, { successMessage: `${label} deleted` });
    router.push("/jobs");
  } catch {
    /* api helper toasts the error; stay on the page so nothing looks deleted */
  }
}

async function refreshRelated() {
  if (!job.value?.id) return;
  await Promise.all([
    fetchRelatedEstimates(),
    fetchRelatedInvoices(),
    fetchTimeEntries(),
    fetchDocuments(),
    fetchJobPhotos(),
    fetchActivity(),
    fetchNotes(),
    fetchCosting(),
    fetchPartsNeeded(),
    fetchCloseout(),
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
    fetchDependencies(),
    fetchDependencyJobOptions(),
    fetchFinancials(),
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

async function fetchFinancials() {
  try {
    financials.value = await api.get(`/api/jobs/${route.params.id}/financials`);
  } catch {
    financials.value = null;
  }
}

// Retroactive deposit (2026-07-23): create the deposit invoice for this
// job's accepted estimate — the same invoice the accept-time flow makes.
const collectingDeposit = ref(false);
const acceptedEstimate = computed(() =>
  relatedEstimates.value.find((e) => (e.status || "").toLowerCase() === "accepted") || null
);

async function collectDeposit() {
  if (!acceptedEstimate.value) return;
  collectingDeposit.value = true;
  try {
    const resp = await api.post(`/api/estimates/${acceptedEstimate.value.id}/deposit-invoice`, {});
    toast.add({
      severity: "success",
      summary: resp.existing ? "Deposit already requested" : "Deposit invoice created",
      detail: `${resp.invoice_number} — ${formatCurrency(resp.amount)}${resp.pay_url ? " (pay link on the invoice)" : ""}`,
      life: 6000,
    });
    await Promise.all([fetchFinancials(), fetchRelatedInvoices()]);
  } catch (err) {
    toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to create deposit invoice", life: 5000 });
  } finally {
    collectingDeposit.value = false;
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

/**
 * The job's photos, from the photo record.
 *
 * Failure is surfaced, never swallowed into an empty list: "no photos" and
 * "couldn't load photos" ask the reader for different things, and this tab
 * spent its whole life telling everyone the first one.
 */
async function fetchJobPhotos() {
  photosLoading.value = true;
  photosError.value = "";
  try {
    const data = await api.get(`/api/jobs/${route.params.id}/photos`, { suppressErrorToast: true });
    jobPhotos.value = Array.isArray(data) ? data : [];
  } catch (err) {
    jobPhotos.value = [];
    // 403/404 here is the access gate, not an empty job — say so rather than
    // reporting "no photos" to someone who simply isn't allowed to see them.
    photosError.value = (err?.status === 403 || err?.status === 404)
      ? "You don't have access to this job's photos."
      : "Couldn't load photos.";
  } finally {
    photosLoading.value = false;
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

async function fetchPartsNeeded() {
  try {
    const data = await api.get(`/api/jobs/${route.params.id}/parts-needed`);
    partsNeeded.value = Array.isArray(data) ? data : data?.items || [];
  } catch {
    partsNeeded.value = [];
  }
}

async function fetchCloseout() {
  try {
    const data = await api.get(`/api/jobs/${route.params.id}/closeout`, {
      suppressErrorToast: true,
    });
    closeout.value = data?.closeout || null;
    closeoutHistory.value = Array.isArray(data?.history) ? data.history : [];
  } catch {
    closeout.value = null;
    closeoutHistory.value = [];
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

async function promoteReceipt(rec) {
  promotingReceiptId.value = rec.id;
  try {
    // Idempotent server-side: re-promoting returns the existing expense.
    const expense = await api.post(
      "/api/expenses/promote-from-receipt",
      { job_receipt_id: rec.id },
      { successMessage: "Expense created from receipt" },
    );
    rec.promoted_expense_id = expense?.id || "promoted";
  } catch {
    /* api helper toasts (422 when the receipt has no amount) */
  } finally {
    promotingReceiptId.value = null;
  }
}

async function fetchDependencies() {
  try {
    const data = await api.get(`/api/jobs/${route.params.id}/dependencies`);
    dependencies.value = Array.isArray(data) ? data : [];
  } catch {
    dependencies.value = [];
  }
}

async function fetchDependencyJobOptions() {
  try {
    const data = await api.get("/api/jobs?per_page=200", { suppressErrorToast: true });
    const rows = Array.isArray(data) ? data : data?.items || [];
    const terminal = new Set(["complete", "completed", "cancelled", "paid", "invoiced", "failed"]);
    dependencyJobOptions.value = rows
      .filter((j) => String(j.id) !== String(route.params.id))
      // A finished job can't block anything — offering it just invites noise.
      .filter((j) => !terminal.has(String(j.lifecycle_stage || j.status || "").toLowerCase()))
      .map((j) => ({ label: j.title || j.job_number || String(j.id).slice(0, 8), value: String(j.id) }));
  } catch {
    dependencyJobOptions.value = [];
  }
}

async function addDependency() {
  if (!newDependencyJobId.value) return;
  try {
    await api.post(
      `/api/jobs/${route.params.id}/dependencies`,
      { depends_on_job_id: newDependencyJobId.value },
      { successMessage: "Dependency added" },
    );
    newDependencyJobId.value = null;
    await fetchDependencies();
  } catch {
    /* api helper toasts (422 on self/unknown job) */
  }
}

async function removeDependency(dep) {
  try {
    await api.del(`/api/jobs/${route.params.id}/dependencies/${dep.id}`, { successMessage: "Dependency removed" });
    dependencies.value = dependencies.value.filter((d) => d.id !== dep.id);
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
    // Refetch PHOTOS, not documents. Refetching documents was the old bug's
    // second half: the upload route doesn't set documents.job_id, so the row
    // never came back and the office watched its own upload disappear.
    await fetchJobPhotos();
  } catch {
  } finally {
    event.options?.clear?.();
    event.clear?.();
  }
}

async function downloadPhoto(photo) {
  // photo.url is already the authed download route for the underlying
  // document; opening it in a tab lets the browser's own auth-less GET 401.
  // Fetch with the token and hand the browser a blob instead — same trick
  // AuthedImage uses for the thumbnail.
  try {
    let token = null;
    try { token = sessionStorage.getItem("gdx_access_token") || null; } catch { /* private mode */ }
    const resp = await fetch(photo.url, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blobUrl = URL.createObjectURL(await resp.blob());
    window.open(blobUrl, "_blank", "noopener");
    // Revoke on the next tick — the new tab has already claimed the blob.
    setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
  } catch {
    toast.add({ severity: "error", summary: "Could not open photo", life: 3000 });
  }
}

function openPhoto(photo) {
  downloadPhoto(photo);
}

async function downloadDocument(id) {
  const url = `/api/documents/${encodeURIComponent(id)}/download`;
  window.open(url, "_blank", "noopener");
}

// Scheduling happens HERE, against the job row — not on the Appointments
// page. Two reasons this is the only correct path:
//   1. `Job.scheduled_at` is canonical. routers/jobs._sync_job_appointment
//      mirrors it into `appointments` (one row per assigned tech) on every
//      job write, and the same function SOFT-DELETES a job's appointments
//      whenever the job has no date. So an appointment created standalone
//      against a dateless job is deleted by the next unrelated job edit.
//   2. PATCHing scheduled_at also advances lifecycle_stage → scheduled and
//      clears the "Awaiting Schedule" pill. Creating an appointment does
//      neither, leaving the job looking unscheduled on every other surface.
// This used to router.push('/appointments?job_id=…'); that view never read
// the query param, so the button landed on an unfiltered appointment list
// with no job context — a dead end.
async function loadDispatchSettings() {
  try {
    const f = await api.get("/api/dispatch-settings");
    if (f) dispatchSettings.value = { ...dispatchSettings.value, ...f };
  } catch { /* defaults stay permissive */ }
}

function openSchedule() {
  loadDispatchSettings();
  const existing = job.value.scheduled_at ? new Date(job.value.scheduled_at) : null;
  scheduleForm.value = {
    scheduled_at: existing && !Number.isNaN(existing.getTime()) ? existing : null,
    tech_ids: assignments.value.length
      ? assignments.value.map((a) => a.tech_id).filter(Boolean)
      : (job.value.assigned_to ? [job.value.assigned_to] : []),
    duration_hours:
      job.value.scheduled_duration_hours != null ? String(job.value.scheduled_duration_hours) : "",
  };
  scheduleError.value = "";
  scheduleDialog.value = true;
}

// Save = PATCH the job. `assigned_tech_ids` is the post-S109 crew shape the
// jobs router resolves through _set_job_assignments, so the crew edited here
// and the crew on the Details tab stay the same list.
async function saveSchedule() {
  if (!job.value.id) return;
  const picked = scheduleForm.value.scheduled_at;
  if (picked && Number.isNaN(new Date(picked).getTime())) {
    scheduleError.value = "That date isn't valid.";
    return;
  }
  savingSchedule.value = true;
  scheduleError.value = "";
  try {
    const payload = {
      scheduled_at: picked ? new Date(picked).toISOString() : null,
      assigned_tech_ids: scheduleForm.value.tech_ids || [],
    };
    // Scheduling a job means it is no longer waiting to be scheduled. Without
    // this it keeps the "Ready to Schedule" holding-area stamp create_job gave
    // it forever — stale data that puts a booked job back in the dispatch
    // intake queue the moment anything reads holding_area_id.
    if (picked && job.value.holding_area_id) {
      payload.holding_area_id = null;
    }
    const rawHours = String(scheduleForm.value.duration_hours ?? "").trim();
    if (rawHours) {
      const hours = Number(rawHours);
      if (!Number.isFinite(hours) || hours < 0) {
        scheduleError.value = "Estimated time must be a positive number of hours.";
        return;
      }
      payload.scheduled_duration_hours = hours;
    } else {
      payload.scheduled_duration_hours = null;
    }
    await api.patch(`/api/jobs/${job.value.id}`, payload, {
      successMessage: picked ? "Job scheduled." : "Schedule cleared.",
    });
    scheduleDialog.value = false;
    // The job write mirrors into appointments server-side; re-read all three
    // so the header pill, the crew list and the Schedule tab agree.
    await Promise.all([fetchJob(), fetchAssignments(), fetchAppointments()]);
  } catch (e) {
    // The tenant hard gate ("A technician is required for scheduled jobs")
    // comes back as a 422 — surface it in the dialog instead of only as a
    // toast behind the modal, so the operator sees why the save didn't take.
    scheduleError.value = e?.message || "Couldn't save the schedule.";
  } finally {
    savingSchedule.value = false;
  }
}

// Plain /appointments, NOT ?job_id= — that param now redirects back here and
// reopens the schedule dialog, so passing it would bounce the user straight
// back to the page they clicked from.
function openAppointmentsPage() {
  router.push("/appointments");
}

// P2.5 — open the Inbox composer already attached to this job. The server
// stamps `[Job #<uuid>]` on the subject, which is what lets the customer's
// reply auto-link back here instead of relying on address matching alone.
function composeEmailAboutJob() {
  const q = new URLSearchParams({ job_id: String(route.params.id) });
  const label = job.value?.job_number || job.value?.title;
  if (label) q.set('job_label', label);
  if (customerDetail.value?.email) q.set('to', customerDetail.value.email);
  if (label) q.set('subject', `${label} — update`);
  router.push(`/inbox?${q.toString()}`);
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
      // One request, not two: the previous form called the endpoint inside the
      // ternary AND again in the branch, throwing the first response away.
      const rows = await api.get("/api/inventory/items");
      inventoryItems.value = Array.isArray(rows) ? rows : [];
    } catch {
      inventoryItems.value = [];
    }
  }
  addPartForm.value = { part_id: null, name: "", sku: "", quantity: 1 };
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

// Add-from-Catalog — the CatalogPickerDialog emits normalized catalog items.
// Each becomes a job parts-to-order row carrying the catalog SELL price
// (unit_price), so it reaches the invoice-create checklist pre-priced. Qty
// defaults to 1 (editable later via the order/edit flow). Best-effort per
// item: one failure doesn't abort the rest.
async function addCatalogParts(items) {
  if (!Array.isArray(items) || !items.length) return;
  addingCatalogParts.value = true;
  let failed = 0;
  for (const it of items) {
    try {
      await api.post(`/api/jobs/${route.params.id}/parts-needed`, {
        part_name: it.name || it.description || it.sku || 'Catalog item',
        quantity: 1,
        sku: it.sku || null,
        unit_price: Number(it.price) > 0 ? Number(it.price) : null,
        urgency: 'normal',
        notes: '',
      });
    } catch {
      failed += 1;
    }
  }
  addingCatalogParts.value = false;
  if (failed) {
    toast.add({ severity: 'warn', summary: 'Some parts failed', detail: `${failed} of ${items.length} could not be added`, life: 4000 });
  } else {
    toast.add({ severity: 'success', summary: 'Added', detail: `${items.length} part${items.length === 1 ? '' : 's'} added to job order list`, life: 3000 });
  }
  await fetchPartsNeeded();
}

async function openApplyTemplate() {
  try {
    const templates = await api.get('/api/job-templates');
    if (!Array.isArray(templates) || !templates.length) {
      toast.add({ severity: 'info', summary: 'No job templates available', life: 3000 });
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
  const name = (addPartForm.value.name || "").trim();
  if ((!addPartForm.value.part_id && !name) || addPartForm.value.quantity <= 0) {
    toast.add({ severity: "warn", summary: "Pick a part (or type one) and a quantity" });
    return;
  }
  addingPart.value = true;
  try {
    await api.post(`/api/mobile/jobs/${route.params.id}/parts-used`, {
      parts: [{
        part_id: addPartForm.value.part_id || null,
        name: addPartForm.value.part_id ? null : name,
        sku: addPartForm.value.part_id ? null : ((addPartForm.value.sku || "").trim() || null),
        qty: Number(addPartForm.value.quantity),
      }],
    }, { successMessage: "Part recorded" });
    addPartDialog.value = false;
    // Both views move: job_parts drives costing, job_parts_needed drives the
    // parts checklist the office bills from.
    await Promise.all([fetchCosting(), fetchPartsNeeded()]);
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
  // `?schedule=1` deep-link — the Dispatch board's "Schedule" verb and the
  // legacy /appointments?job_id=… redirect both land here wanting the dialog
  // open, not just the job page.
  if (route.query.schedule === "1" && job.value?.id) {
    await fetchAssignments();
    openSchedule();
  }
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
/* Separate the one destructive verb from the six constructive ones. The
   auto margin only bites once the row has slack, so on a narrow window it
   simply wraps like the rest instead of stranding itself. */
.header-actions .delete-job-btn { margin-left: auto; }
.stage-strip { display: flex; align-items: center; gap: 0.5rem; margin: 1rem 0; flex-wrap: wrap; }
.stage-btn { min-width: 140px; }
.stage-divider { flex: 1; height: 1px; background: var(--border); }
.job-tabs { margin-bottom: 1rem; }
.tab-panel { margin-top: 1rem; }
.details-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1rem; }
.card { background: var(--surface-card); border-radius: 8px; padding: 1rem; border: 1px solid var(--border); }
/* Details-tab invoice strip — one row per live invoice, Review verb on drafts. */
.invoice-strip { margin-bottom: 1rem; padding: 0.6rem 1rem; }
.invoice-strip-row { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; padding: 0.25rem 0; }
.invoice-strip-row + .invoice-strip-row { border-top: 1px solid var(--border); }
.invoice-strip-id { display: inline-flex; align-items: center; gap: 0.4rem; }
.invoice-strip-money { margin-left: auto; display: inline-flex; align-items: baseline; gap: 0.35rem; }
.card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; gap: 0.5rem; flex-wrap: wrap; }
.parts-to-order-block { margin-top: 1rem; padding-top: 0.75rem; border-top: 1px solid var(--border); }
.parts-subhead { margin: 0 0 0.5rem; font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.03em; color: var(--p-text-muted-color, #94a3b8); }
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
.photo-card { border: 1px solid var(--border); border-radius: 8px; padding: 0.75rem; display: flex; flex-direction: column; gap: 0.75rem; }
.photo-meta { display: flex; flex-direction: column; gap: 0.25rem; }
/* The thumbnail is the control — clicking the photo opens it full size, which
   is what everyone tries first. Both tokens are theme variables so the frame
   holds in dark mode. */
.photo-thumb-btn {
  display: block; width: 100%; padding: 0; cursor: pointer;
  border: 0; background: none;
}
.photo-thumb-btn :deep(img) {
  width: 100%; aspect-ratio: 4 / 3; object-fit: cover; display: block;
  border-radius: 6px; background: var(--surface-subtle);
}
/* The share control reads as a state, not just a checkbox: "Internal only" is
   the default and has to be legible at a glance on a wall of thumbnails. */
.photo-share { display: flex; align-items: center; gap: 0.35rem; font-size: 0.75rem; cursor: pointer; }
.photo-share input { cursor: pointer; }
.photo-share-on { color: var(--p-green-600, #16a34a); font-weight: 600; }
.photo-thumb-failed {
  display: flex; align-items: center; justify-content: center; gap: 0.4rem;
  width: 100%; aspect-ratio: 4 / 3; border-radius: 6px;
  background: var(--surface-subtle); color: var(--p-text-muted-color);
  font-size: 0.8rem;
}
.signature-card .signature-preview { display: flex; justify-content: space-between; align-items: center; }
.signature-canvas-wrap { width: 100%; min-height: 240px; border: 1px dashed var(--border); border-radius: 8px; padding: 0.5rem; background: var(--surface-subtle); }
.signature-canvas { width: 100%; height: 220px; display: block; }
.signature-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 0.75rem; }
.costing-summary .costing-values { display: flex; flex-wrap: wrap; gap: 0.5rem; font-size: 0.85rem; }
.parts-card .p-datatable-wrapper, .time-entry-card .p-datatable-wrapper { max-height: 320px; }
.job-file-list { list-style: none; margin: 0; padding: 0.25rem 0.5rem 0.5rem; }
.job-file-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.35rem 0;
  border-bottom: 1px solid var(--p-content-border-color, #e5e7eb);
  font-size: 0.88rem;
}
.job-file-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.job-file-meta { flex: 0 0 auto; color: var(--p-text-muted-color, #6b7280); font-size: 0.78rem; }

.activity-list { list-style: none; padding: 0; margin: 0; }
.activity-row { display: flex; gap: 0.75rem; padding: 0.75rem 0; border-bottom: 1px solid var(--border); }
.activity-row:last-child { border-bottom: none; }
.activity-symbol { width: 12px; height: 12px; background: var(--accent-b); border-radius: 50%; margin-top: 4px; }
.activity-text { font-size: 0.9rem; margin: 0; }
.activity-meta { margin: 0.2rem 0 0; font-size: 0.8rem; color: var(--p-text-muted-color); }
.dialog-actions { display: flex; justify-content: flex-end; gap: 0.5rem; margin-top: 1rem; }
.muted { color: var(--p-text-muted-color); }

/* Schedule dialog. `.w-full` is used all over this app but never defined
   anywhere, so the controls need real width rules to fill the dialog. */
.schedule-form { display: flex; flex-direction: column; gap: 1rem; }
.schedule-form .form-field { display: flex; flex-direction: column; gap: 0.35rem; }
.schedule-form .form-field > label { font-weight: 600; font-size: 0.9rem; }
.schedule-form .form-field small { font-size: 0.78rem; line-height: 1.35; }
.schedule-form :deep(.p-datepicker),
.schedule-form :deep(.p-multiselect),
.schedule-form :deep(.p-inputtext) { width: 100%; }
/* Both themes: severity colors come from the PrimeVue tokens, which already
   flip with the light/dark surface. */
.schedule-warn { color: var(--p-orange-500, #f59e0b); }
.schedule-error { color: var(--p-red-500, #ef4444); margin: 0; font-size: 0.85rem; }
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
.dependency-select { flex: 1; min-width: 260px; }
</style>
