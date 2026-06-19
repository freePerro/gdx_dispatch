<template>
    <section class="dispatch-view view-card">
      <!-- Toolbar -->
      <div class="dispatch-toolbar">
        <h2 class="dispatch-title">Dispatch Board</h2>
        <div class="toolbar-controls">
          <div class="date-controls">
            <Button
              icon="pi pi-angle-left"
              aria-label="Previous day"
              class="p-button-text"
              severity="secondary"
              size="small"
              data-testid="dispatch-prev-day-btn"
              @click="goPrevDay"
            />
            <DatePicker
              v-model="selectedDate"
              data-testid="dispatch-calendar"
              showIcon
              dateFormat="yy-mm-dd"
              :showButtonBar="true"
              class="dispatch-calendar"
            />
            <Button
              icon="pi pi-angle-right"
              aria-label="Next day"
              class="p-button-text"
              severity="secondary"
              size="small"
              data-testid="dispatch-next-day-btn"
              @click="goNextDay"
            />
            <Button
              label="Today"
              icon="pi pi-calendar"
              severity="secondary"
              size="small"
              data-testid="dispatch-today-btn"
              @click="goToday"
            />
          </div>
          <SelectButton
            v-model="viewMode"
            :options="viewOptions"
            optionLabel="label"
            optionValue="value"
            data-testid="dispatch-view-toggle"
            :allowEmpty="false"
          />
          <DatePicker
            v-model="dateRangeFilter"
            selection-mode="range"
            date-format="yy-mm-dd"
            showIcon
            class="dispatch-range-picker"
            placeholder="Date range"
            data-testid="dispatch-date-range"
          />
          <div class="toolbar-actions">
          <Button
            icon="pi pi-refresh"
            aria-label="Refresh dispatch board"
            severity="secondary"
            size="small"
            data-testid="dispatch-refresh-btn"
            :loading="refreshing"
            @click="refreshBoard"
          />
          <Button
            :label="showMap ? 'Hide Map' : 'Show Map'"
            icon="pi pi-map"
            :severity="showMap ? 'primary' : 'secondary'"
            size="small"
            data-testid="dispatch-map-toggle-btn"
            @click="toggleMap"
          />
          <Button
            label="Optimize Routes"
            icon="pi pi-play"
            severity="primary"
            size="small"
            :loading="optimizerLoading"
            data-testid="dispatch-run-optimizer-btn"
            @click="runOptimizer"
          />
          <Button
            label="Route Order"
            icon="pi pi-sort-alt"
            severity="secondary"
            size="small"
            text
            data-testid="dispatch-route-order-btn"
            @click="requestRouteOrder"
          />
          <Button
            label="Geocode Missing"
            icon="pi pi-map-marker"
            severity="secondary"
            size="small"
            text
            data-testid="dispatch-geocode-btn"
            @click="geocodeMissing"
          />
          </div>
        </div>
      </div>

      <div
        v-if="showMap"
        class="dispatch-map-panel"
        data-testid="dispatch-map-panel"
      >
        <div class="dispatch-map-panel-header">
          <h3>Technician map (latest locations)</h3>
          <Button
            icon="pi pi-refresh"
            aria-label="Refresh technician map"
            class="p-button-text"
            size="small"
            data-testid="dispatch-map-refresh-btn"
            :loading="mapLoading"
            @click="refreshMap"
          />
        </div>
        <div class="dispatch-map-body">
          <div v-if="mapLoading" class="map-loading">Loading map data…</div>
          <div v-else>
            <div v-if="!mapLocations.length" class="empty-message">
              <i class="pi pi-map"></i> No current location pings.
            </div>
            <div v-else class="map-table">
              <div
                v-for="pin in mapLocations"
                :key="pin.tech_id"
                class="map-row"
                :data-testid="`dispatch-map-row-${pin.tech_id}`"
              >
                <span class="map-tech">{{ techName(pin.tech_id) }}</span>
                <span class="map-coords">{{ formatLatLng(pin.lat, pin.lng) }}</span>
                <span class="map-timestamp">{{ formatTimestamp(pin.recorded_at) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
      <p v-if="routeOrderSummary" class="route-order-summary" data-testid="dispatch-route-summary">
        {{ routeOrderSummary }}
      </p>

      <!-- Day View -->
      <div v-if="viewMode === 'day'" style="display: flex; flex-direction: column;">
        <!-- Unassigned Jobs -->
        <Card class="board-section unassigned-section-card" data-testid="unassigned-section" style="order: 2;">
          <template #title>
            <div class="section-header">
              <span class="section-icon pi pi-exclamation-triangle"></span>
              New Jobs to Schedule
              <Badge
                v-if="unassignedJobs.length"
                :value="unassignedJobs.length"
                severity="danger"
              />
            </div>
          </template>
          <template #content>
            <div class="unassigned-grid">
              <div
                v-for="job in unassignedJobs"
                :key="job.id"
                class="job-card unassigned-card"
                :data-testid="`unassigned-job-${job.id}`"
                draggable="true"
                style="cursor:pointer"
                @dragstart="onDragStart(job, $event)"
                @dragend="draggingJobId = null"
                @click="openJobDrawer(job)"
              >
                <div class="job-card-header">
                  <span class="job-customer">{{ displayCustomer(job) }}</span>
                  <JobStateChip :job="job" data-testid="job-status-tag" />
                </div>
                <div class="job-card-body">
                  <p class="job-line"><i class="pi pi-briefcase"></i> {{ job.job_type || 'Service' }}</p>
                  <p class="job-line"><i class="pi pi-clock"></i> {{ job.time_window || 'Anytime' }} · <span class="job-duration">{{ formatDurationHours(job.effective_duration_hours) }}</span></p>
                  <p v-if="job.address" class="job-line"><i class="pi pi-map-marker"></i> {{ job.address }}</p>
                </div>
                <div class="job-card-actions" @click.stop>
                  <Select
                    :options="assignableTechnicians"
                    optionLabel="name"
                    optionValue="id"
                    placeholder="Assign to tech..."
                    class="assign-dropdown"
                    :data-testid="`assign-dropdown-${job.id}`"
                    @change="assignJob(job.id, $event.value)"
                  />
                </div>
              </div>
              <p v-if="!unassignedJobs.length" class="empty-message">
                <i class="pi pi-check-circle"></i> All jobs assigned for this date.
              </p>
            </div>
          </template>
        </Card>

        <div class="skill-filter-row" style="order: 0;" v-if="skillOptions.length">
          <!-- Hide the filter entirely when no tech has any skill data —
               renders cleaner than a permanently-disabled control. -->
          <label for="skillFilter">Filter by skill</label>
          <Select
            id="skillFilter"
            v-model="skillFilter"
            :options="skillSelectOptions"
            optionLabel="label"
            optionValue="value"
            placeholder="All skills"
            class="skill-filter-select"
            data-testid="dispatch-skill-filter"
          />
          <Button
            :label="showCompleted ? 'Hide Completed Jobs' : 'Show Completed Jobs'"
            :icon="showCompleted ? 'pi pi-eye-slash' : 'pi pi-check-circle'"
            :severity="showCompleted ? 'secondary' : 'success'"
            size="small"
            outlined
            class="completed-toggle"
            data-testid="dispatch-toggle-completed"
            @click="showCompleted = !showCompleted"
          />
        </div>

        <!-- Technician Columns -->
        <div class="tech-columns-grid" style="order: 1;">
          <Card
            v-for="tech in technicianColumns"
            :key="tech.id"
            class="tech-column"
            :class="{ 'drag-over': dragOverTechId === tech.id }"
            :data-testid="`tech-column-${tech.id}`"
            @dragover.prevent="onDragOver(tech.id)"
            @dragleave="dragOverTechId = null"
          >
            <template #title>
              <div class="tech-header">
                <Avatar
                  :label="tech.name?.charAt(0) || '?'"
                  shape="circle"
                  class="tech-avatar"
                />
                <span>{{ tech.name }}</span>
                <Badge
                  v-if="tech.jobs.length"
                  :value="tech.jobs.length"
                  severity="info"
                />
              </div>
              <div
                class="tech-capacity"
                :class="{ 'tech-capacity--over': tech.isOverCapacity, 'tech-capacity--off': tech.isOffToday }"
                :data-testid="`tech-capacity-${tech.id}`"
              >
                <span v-if="tech.isOffToday" class="tech-capacity-label">Off today</span>
                <span v-else class="tech-capacity-label">
                  {{ formatDurationHours(tech.scheduledKnownHours) }} of {{ formatDurationHours(tech.capacityHours) }}
                  <span v-if="tech.scheduledUnknownCount" class="tech-capacity-unknown">
                    + {{ tech.scheduledUnknownCount }} no-est
                  </span>
                </span>
                <div v-if="!tech.isOffToday" class="tech-capacity-bar">
                  <div
                    class="tech-capacity-bar-fill"
                    :style="{ width: (tech.capacityRatio * 100).toFixed(0) + '%' }"
                  />
                </div>
              </div>
            </template>
            <template #content>
              <!-- Sprint dispatch-timeline (2026-05-21) — vertical hour-axis
                   layout. The TechTimelineColumn owns drops + tray + block
                   positioning. Card-wrapper @drop above is now a no-op for
                   timeline-area drops; the timeline body intercepts them. -->
              <TechTimelineColumn
                :tech="tech"
                :jobs="tech.jobs"
                :selected-date="selectedDate"
                @open-drawer="openJobDrawer"
                @job-drag-start="(job, e) => onDragStart(job, e)"
                @job-drag-end="draggingJobId = null"
                @place="onTimelinePlace"
                @place-tray="onTimelinePlaceTray"
              />
            </template>
          </Card>
        </div>

        <!-- Holding Areas -->
        <div v-if="holdingAreas.length || dispatchSettings.dispatch_show_unassigned_lane" class="holding-areas-section" style="order: 1;">
          <div class="holding-areas-header">
            <h3>Holding Areas</h3>
            <Button icon="pi pi-plus" label="Add Area" size="small" severity="secondary"
              @click="showAddAreaDialog = true" data-testid="add-holding-area" />
          </div>
          <div class="holding-areas-grid">
            <!-- Scheduled — Not Assigned (system lane, 2026-05-01).
                 Shows upcoming scheduled jobs with no tech, sourced from
                 /api/dispatch/scheduled-unassigned. Read-only — not a drop
                 target, can't be deleted. Today's scheduled-no-tech jobs
                 stay in the per-day "New Jobs to Schedule" column above. -->
            <Card v-if="dispatchSettings.dispatch_show_unassigned_lane"
              class="holding-area-col scheduled-unassigned-col"
              :style="{ borderTop: '3px solid #dc2626' }"
              data-testid="scheduled-unassigned-lane">
              <template #title>
                <div class="holding-area-title">
                  <span style="color: #dc2626">Scheduled — Not Assigned</span>
                  <Tag v-if="scheduledUnassigned.length" :value="String(scheduledUnassigned.length)" severity="danger" rounded />
                </div>
              </template>
              <template #content>
                <div v-for="job in sortedScheduledUnassigned" :key="job.id"
                  class="job-card holding-job-card scheduled-unassigned-card"
                  :class="{ 'scheduled-overdue': isOverdue(job) }"
                  :style="{ borderLeft: '3px solid ' + (isOverdue(job) ? '#7f1d1d' : '#dc2626'), cursor: 'grab' }"
                  draggable="true"
                  @dragstart="onDragStart(job, $event)"
                  @dragend="draggingJobId = null"
                  @click="openJobDrawer(job)">
                  <div class="job-customer-name">
                    {{ displayCustomer(job) === 'No customer attached (lead)' ? '—' : displayCustomer(job) }}
                    <Tag v-if="isOverdue(job)" value="OVERDUE" severity="danger" style="margin-left:0.4rem; font-size:0.65rem" />
                  </div>
                  <p class="job-line"><i class="pi pi-calendar"></i> {{ formatScheduled(job.scheduled_at) }}</p>
                  <p class="job-line muted">{{ job.title || job.job_number }}</p>
                </div>
                <p v-if="!scheduledUnassigned.length" class="empty-message">
                  <i class="pi pi-check-circle"></i> No upcoming jobs missing a tech.
                </p>
              </template>
            </Card>
            <Card v-for="area in holdingAreas" :key="area.id" class="holding-area-col"
              :style="{ borderTop: '3px solid ' + area.color }"
              @dragover.prevent="onDragOver(area.id)"
              @dragleave="dragOverTechId = null"
              @drop.prevent="moveToHoldingArea($event, area.id)"
              :class="{ 'drag-over': dragOverTechId === area.id }">
              <template #title>
                <div class="holding-area-title">
                  <span :style="{ color: area.color }">{{ area.name }}</span>
                  <Tag v-if="getHoldingAreaJobs(area.id).length" :value="String(getHoldingAreaJobs(area.id).length)" severity="info" rounded />
                  <Button icon="pi pi-trash" aria-label="Delete" text size="small" severity="danger"
                    @click="deleteHoldingArea(area.id)" />
                </div>
                <div v-if="getHoldingAreaJobs(area.id).length" class="holding-area-total" :data-testid="`holding-area-total-${area.id}`">
                  {{ formatDurationHours(sumDurationHours(getHoldingAreaJobs(area.id)).known) }} queued
                  <span v-if="sumDurationHours(getHoldingAreaJobs(area.id)).unknown" class="tech-capacity-unknown">
                    + {{ sumDurationHours(getHoldingAreaJobs(area.id)).unknown }} no-est
                  </span>
                </div>
              </template>
              <template #content>
                <div v-for="job in getHoldingAreaJobs(area.id)" :key="job.id"
                  class="job-card holding-job-card" draggable="true"
                  :style="{ borderLeft: '3px solid ' + area.color, cursor: 'pointer' }"
                  :data-testid="`holding-job-${area.id}-${job.id}`"
                  @dragstart="onDragStart(job, $event)"
                  @dragend="draggingJobId = null"
                  @click="openJobDrawer(job)">
                  <div class="job-customer-name">{{ displayCustomer(job) === 'No customer attached (lead)' ? '' : displayCustomer(job) }}</div>
                  <Tag :value="area.name" size="small" class="holding-area-tag"
                    :style="{ backgroundColor: area.color, color: readableText(area.color), borderColor: area.color }" />
                  <JobStateChip v-if="job.display_state || job.status" :job="job" :show-icon="false" />
                  <Button label="Release" icon="pi pi-arrow-right" size="small" text
                    @click.stop="releaseFromHoldingArea(job.id)" />
                </div>
                <p v-if="!getHoldingAreaJobs(area.id).length" class="empty-message drop-hint">
                  <i class="pi pi-inbox"></i> Drop jobs here
                </p>
              </template>
            </Card>
          </div>
        </div>
        <div v-else style="order: 1;">
          <Button label="Set Up Holding Areas" icon="pi pi-plus" severity="secondary" size="small"
            @click="seedHoldingAreas" data-testid="seed-holding-areas" class="mb-3" />
        </div>
      </div>

      <!-- Sprint dispatch-capacity (2026-05-20) — per-tech efficiency leaderboard -->
      <TechEfficiencyPanel v-if="viewMode === 'day'" data-testid="dispatch-efficiency-panel" />

      <!-- Week View -->
      <template v-if="viewMode === 'week'">
        <div class="week-grid">
          <Card
            v-for="day in weekDays"
            :key="day.date"
            class="week-day-col"
            :class="{ 'is-today': day.isToday }"
          >
            <template #title>
              <div class="week-day-header">
                <span class="day-name">{{ day.dayName }}</span>
                <span class="day-date">{{ day.dateLabel }}</span>
                <Badge
                  v-if="day.jobs.length"
                  :value="day.jobs.length"
                  severity="info"
                />
              </div>
            </template>
            <template #content>
              <div
                v-for="job in day.jobs"
                :key="job.id"
                class="job-card week-job-card"
              >
                <div class="job-card-header">
                  <span class="job-customer">{{ displayCustomer(job) }}</span>
                  <JobStateChip :job="job" />
                </div>
                <p class="job-line"><i class="pi pi-user"></i> {{ techName(job.technician_id) }}</p>
                <p class="job-line"><i class="pi pi-clock"></i> {{ job.time_window || 'Anytime' }} · <span class="job-duration">{{ formatDurationHours(job.effective_duration_hours) }}</span></p>
              </div>
              <p v-if="!day.jobs.length" class="empty-message">No jobs</p>
            </template>
          </Card>
        </div>
      </template>

      <!-- Add Holding Area Dialog -->
      <Dialog
        v-model:visible="showAddAreaDialog"
        header="Add holding area"
        :modal="true"
        :closable="true"
        :style="{ width: '420px' }"
        :breakpoints="{ '768px': '95vw' }"
        data-testid="add-holding-area-dialog"
      >
        <form class="add-area-form" @submit.prevent="createHoldingArea">
          <div class="form-field">
            <label for="new-area-name">Name *</label>
            <InputText
              id="new-area-name"
              v-model="newAreaName"
              placeholder="e.g. Awaiting parts"
              class="w-full"
              data-testid="new-area-name"
              autocomplete="off"
              autofocus
            />
          </div>
          <div class="form-field">
            <label for="new-area-color">Color</label>
            <input
              id="new-area-color"
              v-model="newAreaColor"
              type="color"
              class="color-picker"
              data-testid="new-area-color"
            />
          </div>
        </form>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="showAddAreaDialog = false" />
          <Button
            label="Add area"
            icon="pi pi-check"
            :disabled="!newAreaName.trim()"
            :loading="creatingHoldingArea"
            data-testid="add-holding-area-submit"
            @click="createHoldingArea"
          />
        </template>
      </Dialog>

      <!-- Sprint dispatch-capacity (2026-05-20) — duration prompt on drop -->
      <Dialog
        v-model:visible="durationPromptOpen"
        header="How long will this job take?"
        :modal="true"
        :closable="true"
        :style="{ width: '420px' }"
        data-testid="dispatch-duration-prompt"
      >
        <p style="margin-top:0;">
          <strong>{{ durationPromptJobLabel }}</strong> has no estimated time yet.
          A quick guess keeps the day's total accurate.
        </p>
        <div style="display:flex; align-items:center; gap:0.75rem;">
          <InputText
            v-model="durationPromptHours"
            type="number"
            step="0.25"
            min="0"
            placeholder="hours"
            style="max-width:140px;"
            data-testid="dispatch-duration-prompt-hours"
            autofocus
          />
          <span class="muted">hours (e.g. 1.5)</span>
        </div>
        <template #footer>
          <Button
            label="Skip — assign anyway"
            severity="secondary"
            text
            data-testid="dispatch-duration-prompt-skip"
            @click="confirmDurationPrompt(true)"
          />
          <Button
            label="Save & assign"
            icon="pi pi-check"
            :disabled="!(Number(durationPromptHours) > 0)"
            data-testid="dispatch-duration-prompt-save"
            @click="confirmDurationPrompt(false)"
          />
        </template>
      </Dialog>

      <!-- Assignment Dialog -->
      <Dialog
        v-model:visible="assignDialogVisible"
        header="Assign Job"
        :modal="true"
        :closable="true"
        class="assign-dialog"
        data-testid="assign-dialog"
      >
        <p>Assign <strong>{{ assignDialogJob?.customer_name }}</strong> to a technician:</p>
        <Select
          v-model="assignDialogTechId"
          :options="assignableTechnicians"
          optionLabel="name"
          optionValue="id"
          placeholder="Select technician"
          class="w-full"
          data-testid="assign-dialog-dropdown"
        />
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="assignDialogVisible = false" />
          <Button label="Assign" icon="pi pi-check" @click="confirmAssign" :disabled="!assignDialogTechId" />
        </template>
      </Dialog>

      <!-- Phase 2 / C4 — closeout sheet opened by Status="Complete". -->
      <MobileJobCloseoutDialog
        v-model:visible="closeoutOpen"
        :job-id="closeoutJob?.id || ''"
        :job-title="closeoutJob?.title || ''"
        :customer-name="closeoutJob?.customer_name || ''"
        @closed-out="onCloseoutDone"
      />
      <Drawer
        v-model:visible="jobDrawerVisible"
        position="right"
        :modal="false"
        :closable="false"
        class="job-detail-drawer"
        data-testid="dispatch-job-drawer"
        @hide="closeJobDrawer"
      >
        <div class="job-drawer-header">
          <h3>Job details</h3>
          <Button
            label="×"
            class="p-button-text job-drawer-close"
            size="small"
            data-testid="dispatch-job-drawer-close"
            @click="closeJobDrawer"
          />
        </div>
        <div v-if="drawerJob" class="job-drawer-content">
          <div class="job-drawer-status">
            <span class="muted">Status</span>
            <JobStateChip :job="drawerJob" />
          </div>
          <p class="job-drawer-line"><strong>Customer:</strong> {{ drawerJob.customer_name }}</p>
          <p class="job-drawer-line"><strong>Address:</strong> {{ drawerJob.address || 'N/A' }}</p>
          <p class="job-drawer-line"><strong>Job type:</strong> {{ drawerJob.job_type }}</p>
          <p class="job-drawer-line"><strong>Window:</strong> {{ drawerJob.time_window }}</p>
          <p v-if="drawerJob.technician_id" class="job-drawer-line">
            <strong>Technician:</strong> {{ techName(drawerJob.technician_id) }}
          </p>
          <p class="job-drawer-line"><strong>Scheduled:</strong> {{ drawerJob.scheduled_at || 'Not scheduled' }}</p>
          <Button
            label="Open Job"
            icon="pi pi-external-link"
            class="w-full"
            size="small"
            data-testid="dispatch-job-open"
            @click="() => goToJob(drawerJob)"
          />
        </div>
        <div v-else class="job-drawer-content">
          <p class="muted">Select a job to see details.</p>
        </div>
      </Drawer>
      <div v-if="liveTechs.length" class="card live-techs-card" data-testid="live-techs-card">
        <div class="card-header">
          <h3>Live Techs (last 30 min)</h3>
        </div>
        <table class="live-techs-table">
          <thead>
            <tr><th>Tech</th><th>Lat / Lng</th><th>Accuracy</th><th>Last Sample</th></tr>
          </thead>
          <tbody>
            <tr v-for="t in liveTechs" :key="t.id" :data-testid="`live-tech-${t.user_id}`">
              <td>{{ t.technician_id || t.user_id }}</td>
              <td>
                <a
                  :href="`https://maps.google.com/?q=${t.lat},${t.lng}`"
                  target="_blank"
                >{{ t.lat.toFixed(5) }}, {{ t.lng.toFixed(5) }}</a>
              </td>
              <td>{{ t.accuracy_m != null ? `±${Math.round(t.accuracy_m)}m` : '—' }}</td>
              <td>{{ new Date(t.recorded_at).toLocaleTimeString() }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
</template>
<script setup>
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue';
import { useRouter } from 'vue-router';
import { useApiWithToast } from '../composables/useApiWithToast';
import { useToast } from 'primevue/usetoast';
import Avatar from 'primevue/avatar';
import Badge from 'primevue/badge';
import Button from 'primevue/button';
import DatePicker from 'primevue/datepicker';
import Card from 'primevue/card';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import MobileJobCloseoutDialog from '../components/MobileJobCloseoutDialog.vue';
import Drawer from 'primevue/drawer';
import Select from 'primevue/select';
import SelectButton from 'primevue/selectbutton';
import Tag from 'primevue/tag';
import JobStateChip from '../components/JobStateChip.vue';
import TechEfficiencyPanel from '../components/TechEfficiencyPanel.vue';
import TechTimelineColumn from '../components/TechTimelineColumn.vue';

const api = useApiWithToast();
const toast = useToast();
const router = useRouter();

const jobStatuses = ['Scheduled', 'In Progress', 'Complete', 'Invoiced'];

function goToJob(job) {
  if (job?.id) router.push(`/jobs/${job.id}`);
}

async function quickStatusChange(job) {
  // Phase 2 / C4 (Doug 2026-05-10): "Complete" no longer fires a bare POST
  // /complete. It opens the closeout sheet — parts + hours + signature +
  // notes get captured in one transaction. The closeout endpoint flips
  // lifecycle to 'completed' and writes a JobCloseout snapshot row. Phase
  // 1's /complete path stays alive (legacy clients, mobile complete) but
  // dispatch's primary affordance is now the sheet.
  //
  // Other transitions (Scheduled / In Progress / Invoiced) stay on PATCH.
  try {
    if (job.status === 'Complete') {
      // Open the closeout dialog. Don't PATCH or POST anything until the
      // dialog submits. fetchJobs() runs on dialog close (success OR cancel)
      // to revert the optimistic dropdown change.
      closeoutJob.value = job;
      closeoutOpen.value = true;
    } else {
      await api.patch(`/api/jobs/${job.id}`, { status: job.status });
    }
  } catch {
    // Revert on failure — re-fetch
    await fetchJobs();
  }
}

const closeoutOpen = ref(false);
const closeoutJob = ref(null);
function onCloseoutDone() {
  // Closeout submitted successfully. The endpoint flipped lifecycle to
  // 'completed' and the job now lives in /api/jobs/ready-for-billing.
  // The success toast is already fired by the dialog itself; the watcher
  // on closeoutOpen handles the refetch.
  closeoutOpen.value = false;
}
// When the dialog closes for any reason (submit OR cancel), refetch.
// On success that's a no-op visually (the success path already calls
// fetchJobs in onCloseoutDone). On cancel it reverts the optimistic
// dropdown change by re-loading the job's real status.
watch(closeoutOpen, async (v) => {
  if (!v) {
    closeoutJob.value = null;
    await fetchJobs();
  }
});

const selectedDate = ref(new Date());
const viewMode = ref('day');
const viewOptions = [
  { label: 'Day', value: 'day' },
  { label: 'Week', value: 'week' },
];
const refreshing = ref(false);
const draggingJobId = ref(null);
const dragOverTechId = ref(null);
const jobs = ref([]);
const technicians = ref([]);
const skillFilter = ref(null);
const dateRangeFilter = ref(null);
const showCompleted = ref(false);

function isCompletedStatus(status) {
  const s = String(status || '').toLowerCase();
  return s === 'complete' || s === 'completed' || s === 'invoiced';
}
const skillOptions = ref([]);
const holdingAreas = ref([]);
const dispatchSettings = ref({
  dispatch_show_unassigned_lane: false,
});
const scheduledUnassigned = ref([]);
const showAddAreaDialog = ref(false);
const newAreaName = ref("");
const newAreaColor = ref("#6b7280");
const jobDrawerVisible = ref(false);
const drawerJob = ref(null);
const showMap = ref(false);
const mapLocations = ref([]);
const mapLoading = ref(false);
const routeOrderSummary = ref('');
const optimizerLoading = ref(false);

// Assignment dialog
const assignDialogVisible = ref(false);
const assignDialogJob = ref(null);
const assignDialogTechId = ref(null);

// Sprint dispatch-capacity (2026-05-20) — when a card without an
// estimated duration lands in a tech column, prompt the scheduler for
// hours so the column total stops being a guess. Carrying both the
// job_id and the destination tech_id so Save can commit the full patch.
const durationPromptOpen = ref(false);
const durationPromptJobId = ref(null);
const durationPromptTechId = ref(null);
const durationPromptHours = ref('');
const durationPromptJobLabel = ref('');
// Sprint dispatch-timeline (2026-05-21) — carry the timeline-drop target
// time through the prompt so confirm/skip both reach _doAssignJob with
// the slot the user clicked, not just the tech they dropped on.
const durationPromptScheduledAt = ref(null);

function toDateStr(date) {
  if (!date || !(date instanceof Date) || isNaN(date.getTime())) return '';
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

const selectedDateStr = computed(() => toDateStr(selectedDate.value));

// Reset to today if DatePicker is cleared (prevents null date crashes)
watch(selectedDate, (val) => { if (!val) selectedDate.value = new Date(); });

watch(selectedDateStr, () => {
  routeOrderSummary.value = '';
  fetchJobs();
});

const skillSelectOptions = computed(() => [
  { label: 'All skills', value: null },
  ...skillOptions.value.map((skill) => ({ label: skill, value: skill })),
]);

function goToday() {
  selectedDate.value = new Date();
}

// --- Normalization ---

function normalizeSkills(value) {
  if (!value) return [];
  if (Array.isArray(value)) {
    return value
      .map((item) => (item ? String(item).trim() : ''))
      .filter((text) => text);
  }
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) {
        return normalizeSkills(parsed);
      }
    } catch {
      // ignore
    }
    return value
      .split(',')
      .map((part) => part.trim())
      .filter(Boolean);
  }
  return [];
}

function normalizeTech(tech) {
  return {
    id: String(tech.id),
    name: tech.name || tech.user_id || `Tech ${tech.id}`,
    skills: normalizeSkills(tech.skills),
    // Sprint dispatch-capacity (2026-05-20) — effective shift values
    // (server already resolved user-override → tenant default per-field).
    effective_shift_start: tech.effective_shift_start || null,
    effective_shift_end: tech.effective_shift_end || null,
    effective_workdays: Number.isInteger(tech.effective_workdays) ? tech.effective_workdays : 31,
  };
}

function normalizeJob(rawJob) {
  const crewIds = Array.isArray(rawJob.assigned_tech_ids) && rawJob.assigned_tech_ids.length
    ? rawJob.assigned_tech_ids.map(String)
    : [];
  const primary = rawJob.technician_id || rawJob.technicianId || rawJob.assigned_to || null;
  if (!crewIds.length && primary) crewIds.push(String(primary));
  return {
    ...rawJob,
    id: rawJob.id,
    status: rawJob.status || rawJob.lifecycle_stage || 'Scheduled',
    technician_id: primary,
    assigned_tech_ids: crewIds,
    lead_tech_id: rawJob.lead_tech_id || primary || null,
    // 2026-05-01: this previously hard-coded null, which silently undid the
    // moveToHoldingArea PATCH on every refresh — leads dropped into a
    // holding area would jump back to "New Jobs to Schedule" because the
    // normalizer wiped the field before the filter saw it.
    holding_area_id: rawJob.holding_area_id || null,
    scheduled_at: rawJob.scheduled_at || rawJob.scheduledAt || null,
    address: rawJob.address || rawJob.service_address || [rawJob.city, rawJob.state].filter(Boolean).join(', '),
    customer_name: rawJob.customer_name || (typeof rawJob.customer === 'object' ? rawJob.customer?.name : rawJob.customer) || '',
    job_type: rawJob.job_type || rawJob.type || 'Service',
    time_window: rawJob.time_window || rawJob.timeWindow || 'Anytime',
    // Sprint dispatch-capacity — pass duration through. effective is the
    // server-resolved value (scheduled_duration_hours → estimate calc).
    scheduled_duration_hours: rawJob.scheduled_duration_hours != null
      ? Number(rawJob.scheduled_duration_hours) : null,
    effective_duration_hours: rawJob.effective_duration_hours != null
      ? Number(rawJob.effective_duration_hours) : null,
  };
}

// Sprint dispatch-capacity (2026-05-20) — duration + capacity helpers.
// JS day-of-week (0 = Sun) ↔ workday bitmask (Mon=1..Sun=64).
function workdayBitForDate(date) {
  const d = date instanceof Date ? date : new Date(date);
  const dow = d.getDay();
  return dow === 0 ? 64 : (1 << (dow - 1));
}
function isShiftDay(tech, date) {
  return ((tech.effective_workdays || 0) & workdayBitForDate(date)) !== 0;
}
function parseHHMM(s) {
  if (!s || typeof s !== 'string') return null;
  const m = /^(\d{1,2}):(\d{2})/.exec(s);
  if (!m) return null;
  return Number(m[1]) + Number(m[2]) / 60;
}
function techCapacityHours(tech, date) {
  if (!isShiftDay(tech, date)) return 0;
  const start = parseHHMM(tech.effective_shift_start);
  const end = parseHHMM(tech.effective_shift_end);
  if (start == null || end == null || end <= start) return 0;
  return end - start;
}
function formatDurationHours(value) {
  if (value == null || !Number.isFinite(Number(value))) return '?h';
  const n = Number(value);
  return `${(Math.round(n * 100) / 100).toString().replace(/\.?0+$/, '')}h`;
}
function sumDurationHours(jobList) {
  let known = 0;
  let unknown = 0;
  for (const j of jobList) {
    const v = Number(j.effective_duration_hours);
    if (Number.isFinite(v) && v > 0) known += v;
    else unknown += 1;
  }
  return { known: Math.round(known * 100) / 100, unknown };
}

// --- Computed ---

function matchesDate(job, dateStr) {
  if (!job.scheduled_at) return dateStr === selectedDateStr.value;
  return String(job.scheduled_at).split('T')[0] === dateStr;
}

const rangeFilteredJobs = computed(() => {
  const [start, end] = dateRangeFilter.value || [];
  const startStamp = start ? new Date(start).setHours(0, 0, 0, 0) : null;
  const endStamp = end ? new Date(end).setHours(23, 59, 59, 999) : null;
  return jobs.value.filter((job) => {
    if (!showCompleted.value && isCompletedStatus(job.status)) return false;
    if (!startStamp && !endStamp) return true;
    const timestamp = job.scheduled_at ? new Date(job.scheduled_at).getTime() : null;
    if (!timestamp) return true;
    if (startStamp && timestamp < startStamp) return false;
    if (endStamp && timestamp > endStamp) return false;
    return true;
  });
});
const dayJobs = computed(() => rangeFilteredJobs.value.filter((j) => matchesDate(j, selectedDateStr.value)));

// "New Jobs to Schedule" section: jobs with no date AND no tech AND no
// holding area — true leads waiting to be slotted. Scheduled-but-no-tech
// jobs live in the red "Scheduled — Not Assigned" lane regardless of date,
// so a salesperson penciling in an asap job can't slip past the dispatcher.
const unassignedJobs = computed(() =>
  jobs.value.filter((j) =>
    !j.scheduled_at &&
    !j.technician_id &&
    !j.holding_area_id &&
    !isCompletedStatus(j.status)
  )
);

// Drop-target list for the "Assign to tech" dropdowns. Inactive techs
// stay out so a dispatcher can't pick one — but they still get a column
// in filteredTechnicians when they have a job referenced today.
const assignableTechnicians = computed(() =>
  technicians.value.filter((tech) => tech.active !== false),
);

const filteredTechnicians = computed(() => {
  // Active techs make the default column set. Any tech (active or not)
  // referenced by a job on the visible day — primary OR crew member —
  // is unioned in so multi-tech jobs and inactive-tech assignments don't
  // silently disappear (S109).
  const referenced = new Set();
  for (const j of dayJobs.value || []) {
    for (const tid of (j.assigned_tech_ids || [])) {
      if (tid) referenced.add(String(tid));
    }
  }
  const visible = technicians.value.filter(
    (tech) => tech.active !== false || referenced.has(String(tech.id)),
  );
  if (!skillFilter.value) return visible;
  return visible.filter((tech) => (tech.skills || []).includes(skillFilter.value));
});

const technicianColumns = computed(() =>
  filteredTechnicians.value.map((tech) => {
    const techJobs = dayJobs.value.filter((j) =>
      (j.assigned_tech_ids || []).map(String).includes(String(tech.id)),
    );
    const capacity = techCapacityHours(tech, selectedDate.value);
    const totals = sumDurationHours(techJobs);
    const off = capacity === 0;
    const ratio = capacity > 0 ? Math.min(1, totals.known / capacity) : 0;
    return {
      ...tech,
      jobs: techJobs,
      capacityHours: capacity,
      scheduledKnownHours: totals.known,
      scheduledUnknownCount: totals.unknown,
      isOffToday: off,
      capacityRatio: ratio,
      isOverCapacity: capacity > 0 && totals.known > capacity,
    };
  }),
);

// Week view
const weekDays = computed(() => {
  const start = new Date(selectedDate.value);
  const dayOfWeek = start.getDay();
  start.setDate(start.getDate() - dayOfWeek); // Sunday
  const todayStr = toDateStr(new Date());
  const days = [];
  const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  for (let i = 0; i < 7; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    const ds = toDateStr(d);
      days.push({
        date: ds,
        dayName: dayNames[i],
        dateLabel: `${d.getMonth() + 1}/${d.getDate()}`,
        isToday: ds === todayStr,
        jobs: rangeFilteredJobs.value.filter((j) => matchesDate(j, ds)),
      });
  }
  return days;
});

// --- Display helpers ---

function displayCustomer(job) {
  // A Lead-stage job with no customer attached is the legitimate empty
  // state — render "No customer attached (lead)" so dispatchers know the
  // row is a lead waiting on intake, not a missing-data bug.
  if (!job.customer_name) return 'No customer attached (lead)';
  if (job.customer_name === 'Unknown Customer') return 'No customer attached (lead)';
  // Sprint customer-multi-location (2026-05-21) — when the job is bound
  // to a specific site, suffix the label so dispatchers can tell which
  // of a customer's sites is in play. list_jobs returns location_label
  // via the LEFT JOIN on customer_locations.
  if (job.location_label) {
    return `${job.customer_name} · ${job.location_label}`;
  }
  return job.customer_name;
}

function techName(techId) {
  if (!techId) return 'Unassigned';
  const t = technicians.value.find((tech) => String(tech.id) === String(techId));
  return t ? t.name : 'Unknown';
}

// Readable foreground for an arbitrary holding-area color. GDX's areas span
// dark (blue/purple) to light (amber #f59e0b) — a hardcoded white is
// unreadable on the light ones. Perceived-luminance threshold picks dark
// ink on light fills, white on dark fills.
function readableText(hex) {
  const c = String(hex || '').replace('#', '');
  if (c.length !== 6) return '#ffffff';
  const r = parseInt(c.slice(0, 2), 16);
  const g = parseInt(c.slice(2, 4), 16);
  const b = parseInt(c.slice(4, 6), 16);
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.6 ? '#1f2937' : '#ffffff';
}

function formatLatLng(lat, lng) {
  if (lat === null || lat === undefined || lng === null || lng === undefined) return '—';
  return `${Number(lat).toFixed(5)}, ${Number(lng).toFixed(5)}`;
}

function formatTimestamp(value) {
  if (!value) return '—';
  try {
    return new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
  return String(value);
}
}

// --- UI helpers & controls ---

function shiftSelectedDate(days) {
  const base = new Date(selectedDate.value);
  base.setDate(base.getDate() + days);
  selectedDate.value = base;
}

function goPrevDay() {
  shiftSelectedDate(-1);
}

function goNextDay() {
  shiftSelectedDate(1);
}

function openJobDrawer(job) {
  drawerJob.value = job;
  jobDrawerVisible.value = true;
}

function closeJobDrawer() {
  jobDrawerVisible.value = false;
  drawerJob.value = null;
}

function openReassignDialog(job) {
  assignDialogJob.value = job;
  assignDialogTechId.value = job?.technician_id || null;
  assignDialogVisible.value = true;
}

async function unassignJob(job) {
  if (!job?.id) return;
  await assignJob(job.id, null);
}

function toggleMap() {
  showMap.value = !showMap.value;
  if (showMap.value) {
    refreshMap();
  }
}

async function refreshMap() {
  mapLoading.value = true;
  try {
    const data = await api.get('/api/gps/technicians/live');
    const list = (() => {
      if (Array.isArray(data)) return data;
      if (Array.isArray(data?.locations)) return data.locations;
      if (Array.isArray(data?.tech_locations)) return data.tech_locations;
      return [];
    })();
    mapLocations.value = list;
  } finally {
    mapLoading.value = false;
  }
}

async function requestRouteOrder() {
  try {
    const data = await api.get(`/api/dispatch/optimize-route?date=${selectedDateStr.value}`);
    const stops =
      Array.isArray(data?.sorted_stop_list)
        ? data.sorted_stop_list
        : Array.isArray(data?.stops)
          ? data.stops
          : Array.isArray(data?.optimized_job_ids)
            ? data.optimized_job_ids
            : [];
    const count = stops.length;
    const message = count
      ? `Route order returned ${count} stop${count === 1 ? '' : 's'}.`
      : 'Route order completed with no stops.';
    routeOrderSummary.value = message;
    toast.add({ severity: 'success', summary: 'Route order', detail: message, life: 4000 });
  } catch (error) {
    routeOrderSummary.value = '';
  }
}

async function runOptimizer() {
  if (optimizerLoading.value) return;
  optimizerLoading.value = true;
  try {
    await api.post(
      '/api/dispatch/optimize',
      { date: selectedDateStr.value },
      { successMessage: 'Optimizer queued', suppressErrorToast: true },
    );
    await refreshBoard();
  } catch (e) {
    const status = e?.status || e?.response?.status;
    if (status === 501) {
      toast.add({ severity: 'info', summary: 'Coming soon', detail: 'Route optimization is not yet enabled for this tenant.', life: 4000 });
    } else {
      toast.add({ severity: 'error', summary: 'Optimizer failed', detail: e?.message || 'Please try again.', life: 4000 });
    }
  } finally {
    optimizerLoading.value = false;
  }
}

async function geocodeMissing() {
  try {
    await api.post(
      '/api/dispatch/geocode-missing',
      { date: selectedDateStr.value },
      { successMessage: 'Missing coordinates geocoded', suppressErrorToast: true },
    );
    await refreshBoard();
  } catch (e) {
    const status = e?.status || e?.response?.status;
    if (status === 501) {
      toast.add({ severity: 'info', summary: 'Coming soon', detail: 'Geocoding worker is not yet enabled for this tenant.', life: 4000 });
    } else {
      toast.add({ severity: 'error', summary: 'Geocode failed', detail: e?.message || 'Please try again.', life: 4000 });
    }
  }
}

// --- Drag & Drop ---

function onDragStart(job, event) {
  draggingJobId.value = job.id;
  if (event?.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', String(job.id));
  }
}

function onDragOver(techId) {
  dragOverTechId.value = techId;
}

async function handleDrop(techId, event) {
  event?.preventDefault?.();
  dragOverTechId.value = null;
  const transferId = event?.dataTransfer?.getData('text/plain');
  const dropJobId = transferId || draggingJobId.value;
  if (!dropJobId) return;
  draggingJobId.value = null;
  await assignJob(dropJobId, techId);
}

// Sprint dispatch-capacity (2026-05-21) — /audit caught this: the
// duration prompt was attached to drag-drop only; the assignment Select
// dropdown on unassigned cards bypassed it entirely, so every
// dropdown-assigned job landed with no duration and the efficiency
// leaderboard silently excluded those rows. The prompt now lives on the
// assignment service itself, so every entry path (drag-drop, dropdown,
// quick-assign Dialog) gates equally. techId=null (unassign) is exempt.
function _needsDurationPrompt(jobId, techId) {
  if (!techId) return null;
  const job = jobs.value.find((j) => String(j.id) === String(jobId))
    || scheduledUnassigned.value.find((j) => String(j.id) === String(jobId))
    || unassignedJobs.value.find((j) => String(j.id) === String(jobId));
  if (!job) return null;
  const hasDuration = Number(job.scheduled_duration_hours) > 0
    || Number(job.effective_duration_hours) > 0;
  return hasDuration ? null : job;
}

async function confirmDurationPrompt(skip = false) {
  const jobId = durationPromptJobId.value;
  const techId = durationPromptTechId.value;
  const scheduledAt = durationPromptScheduledAt.value;
  const hours = Number(durationPromptHours.value);
  durationPromptOpen.value = false;
  if (!jobId || !techId) return;
  // Skip = assign without recording a duration (card stays "?h"). Confirm =
  // PATCH duration first so the column total reflects it; the assignment
  // PATCH follows. Pass {skipPrompt:true} so we don't re-enter the prompt.
  if (!skip && Number.isFinite(hours) && hours > 0) {
    try {
      await api.patch(`/api/jobs/${jobId}`, { scheduled_duration_hours: hours });
    } catch (_e) {
      // api composable surfaces the toast; fall through and still try to assign.
    }
  }
  await _doAssignJob(jobId, techId, scheduledAt);
}

async function assignJob(jobId, techId, scheduledAt = null) {
  // Sprint dispatch-capacity — gate every assignment path through one
  // duration check. Unassign (techId=null) is exempt; the prompt only
  // fires when we're putting a job into a tech's day. Re-entry on
  // confirmDurationPrompt → _doAssignJob skips this gate by design.
  // scheduledAt (timeline drop target) threads through the prompt.
  const jobNeedingDuration = _needsDurationPrompt(jobId, techId);
  if (jobNeedingDuration) {
    durationPromptJobId.value = jobId;
    durationPromptTechId.value = techId;
    durationPromptScheduledAt.value = scheduledAt;
    durationPromptHours.value = '';
    durationPromptJobLabel.value = jobNeedingDuration.customer_name
      || jobNeedingDuration.title
      || 'Job';
    durationPromptOpen.value = true;
    return;
  }
  await _doAssignJob(jobId, techId, scheduledAt);
}

async function _doAssignJob(jobId, techId, scheduledAt = null) {
  const jobIndex = jobs.value.findIndex((j) => String(j.id) === String(jobId));
  const sourceJob = jobIndex >= 0 ? jobs.value[jobIndex] : null;
  const laneJob = scheduledUnassigned.value.find((j) => String(j.id) === String(jobId));

  // Sprint dispatch-timeline (2026-05-21) — three scheduled_at paths:
  //   (a) caller passed an explicit ISO (timeline drop on a specific slot)
  //       → use it verbatim. The block is now anchored to that time.
  //   (b) job already has a scheduled_at → leave it alone. A pure tech
  //       reassignment (dropdown, Reassign dialog) shouldn't move time.
  //   (c) no scheduled_at on either source or lane → land it on midnight
  //       of the selected date so it shows up in this day's tray. The
  //       old hard-coded 9:00am fallback masked "this hasn't actually
  //       been scheduled to a time yet" — tray is the honest UX.
  const hasDate = Boolean(sourceJob?.scheduled_at || laneJob?.scheduled_at);
  const patch = { assigned_tech_id: techId, assigned_to: techId };
  if (scheduledAt) {
    patch.scheduled_at = scheduledAt;
  } else if (!hasDate) {
    const d = new Date(selectedDate.value || new Date());
    d.setHours(0, 0, 0, 0);
    patch.scheduled_at = d.toISOString();
  }

  // Job not in the per-day list (e.g., dragged from the lane which holds
  // any-date jobs) — PATCH and refresh both lists, then toast so the user
  // sees the assignment landed even if the card moves out of view.
  if (jobIndex === -1) {
    const techName = (technicians.value.find((t) => String(t.id) === String(techId)) || {}).name || 'tech';
    try {
      await api.patch(`/api/jobs/${jobId}`, patch);
      await Promise.all([fetchJobs(), fetchScheduledUnassigned()]);
      const when = patch.scheduled_at
        ? formatScheduled(patch.scheduled_at)
        : (laneJob?.scheduled_at ? formatScheduled(laneJob.scheduled_at) : 'this date');
      toast.add({
        severity: 'success',
        summary: 'Job assigned',
        detail: `${laneJob?.customer_name || laneJob?.title || 'Job'} → ${techName} on ${when}.`,
        life: 4000,
      });
    } catch (_e) { /* api composable already toasts */ }
    return;
  }

  const previousTech = jobs.value[jobIndex].technician_id;
  const previousSchedAt = jobs.value[jobIndex].scheduled_at;
  jobs.value[jobIndex] = {
    ...jobs.value[jobIndex],
    technician_id: techId,
    ...(patch.scheduled_at ? { scheduled_at: patch.scheduled_at } : {}),
  };

  try {
    await api.patch(`/api/jobs/${jobId}`, { ...patch, technician_id: techId });
    // Refresh if we wrote a date so the row's scheduled_at reflects it
    // (and the timeline re-renders the block at the new position).
    if (patch.scheduled_at) await fetchJobs();
  } catch {
    jobs.value[jobIndex] = {
      ...jobs.value[jobIndex],
      technician_id: previousTech,
      scheduled_at: previousSchedAt,
    };
  }
}

// Sprint dispatch-timeline (2026-05-21) — TechTimelineColumn emit handlers.
// `place` = drop on a timeline slot (specific time). `place-tray` = drop
// in the unscheduled tray (date-only, no time). Both unify through the
// same assignment gate, so the duration prompt fires for jobs lacking a
// scheduled_duration_hours regardless of which target they land on.
async function onTimelinePlace({ jobId, techId, startISO }) {
  if (!jobId || !techId || !startISO) return;
  // Same-tech reschedule with existing duration → patch scheduled_at
  // only, no duration prompt (we know the height already, and the block
  // is just moving on the same tech's timeline).
  const job = jobs.value.find((j) => String(j.id) === String(jobId));
  const sameTech = job && String(job.technician_id) === String(techId);
  const hasDuration = job
    && (Number(job.scheduled_duration_hours) > 0
        || Number(job.effective_duration_hours) > 0);
  if (sameTech && hasDuration) {
    const idx = jobs.value.findIndex((j) => String(j.id) === String(jobId));
    const prev = jobs.value[idx].scheduled_at;
    jobs.value[idx] = { ...jobs.value[idx], scheduled_at: startISO };
    try {
      await api.patch(`/api/jobs/${jobId}`, { scheduled_at: startISO });
    } catch {
      jobs.value[idx] = { ...jobs.value[idx], scheduled_at: prev };
    }
    return;
  }
  await assignJob(jobId, techId, startISO);
}

async function onTimelinePlaceTray({ jobId, techId }) {
  if (!jobId || !techId) return;
  const d = new Date(selectedDate.value || new Date());
  d.setHours(0, 0, 0, 0);
  await assignJob(jobId, techId, d.toISOString());
}

function confirmAssign() {
  if (assignDialogJob.value && assignDialogTechId.value) {
    assignJob(assignDialogJob.value.id, assignDialogTechId.value);
  }
  assignDialogVisible.value = false;
  assignDialogJob.value = null;
  assignDialogTechId.value = null;
}

// --- API ---

async function fetchTechnicians() {
  try {
    const data = await api.get('/api/technicians');
    const rows = Array.isArray(data) ? data : data?.items || data?.data || [];
    // Keep every tech the API returns; the column-filtering rule lives in
    // visibleTechnicians so a job assigned to an inactive (or otherwise
    // hidden) tech still surfaces on the board instead of vanishing.
    technicians.value = rows.map(normalizeTech);
  } catch {
    technicians.value = [];
  }
}

async function loadSkillOptions() {
  try {
    const data = await api.get('/api/technicians/skills');
    const list = Array.isArray(data)
      ? data
      : Array.isArray(data?.skills)
        ? data.skills
        : [];
    skillOptions.value = Array.from(
      new Set(list.map((item) => (item ? String(item).trim() : '')).filter(Boolean)),
    );
  } catch {
    skillOptions.value = Array.from(
      new Set(
        technicians.value.flatMap((tech) => (Array.isArray(tech.skills) ? tech.skills : [])),
      ),
    );
  }

  if (skillFilter.value && !skillOptions.value.includes(skillFilter.value)) {
    skillFilter.value = null;
  }
}

async function fetchJobs() {
  try {
    const data = await api.get(`/api/jobs?date=${selectedDateStr.value}`);
    const rows = Array.isArray(data) ? data : data?.items || data?.data || [];
    jobs.value = rows.map(normalizeJob);
  } catch {
    jobs.value = [];
  }
}

async function fetchHoldingAreas() {
  try {
    const res = await api.get("/api/holding-areas");
    holdingAreas.value = Array.isArray(res) ? res : [];
  } catch { holdingAreas.value = []; }
}

async function loadDispatchSettings() {
  try {
    const f = await api.get("/api/dispatch-settings");
    if (f) dispatchSettings.value = { ...dispatchSettings.value, ...f };
  } catch (_e) { /* defaults stay off */ }
}

async function fetchScheduledUnassigned() {
  if (!dispatchSettings.value.dispatch_show_unassigned_lane) {
    scheduledUnassigned.value = [];
    return;
  }
  try {
    const res = await api.get("/api/dispatch/scheduled-unassigned");
    scheduledUnassigned.value = Array.isArray(res?.items) ? res.items : [];
  } catch { scheduledUnassigned.value = []; }
}

function isOverdue(job) {
  if (!job?.scheduled_at) return false;
  const d = new Date(job.scheduled_at);
  if (Number.isNaN(d.getTime())) return false;
  // Overdue = scheduled date has passed AND no tech yet. Anything before
  // today's start-of-day counts; today's not-yet-passed counts as urgent
  // but not overdue.
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  return d.getTime() < startOfToday.getTime();
}

const sortedScheduledUnassigned = computed(() => {
  // Overdue first (oldest at top), then upcoming chronologically.
  const arr = (scheduledUnassigned.value || []).slice();
  arr.sort((a, b) => {
    const aOver = isOverdue(a);
    const bOver = isOverdue(b);
    if (aOver !== bOver) return aOver ? -1 : 1;
    return new Date(a.scheduled_at).getTime() - new Date(b.scheduled_at).getTime();
  });
  return arr;
});

function formatScheduled(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function getHoldingAreaJobs(areaId) {
  return dayJobs.value.filter((j) => j.holding_area_id === areaId);
}

async function moveToHoldingArea(event, areaId) {
  dragOverTechId.value = null;
  const transferId = event?.dataTransfer?.getData("text/plain");
  const jobId = transferId || draggingJobId.value;
  if (!jobId) return;
  const idx = jobs.value.findIndex((j) => String(j.id) === String(jobId));
  if (idx === -1) return;
  jobs.value[idx] = { ...jobs.value[idx], holding_area_id: areaId, technician_id: null };
  draggingJobId.value = null;
  try {
    await api.patch(`/api/jobs/${jobId}`, { assigned_to: null, holding_area_id: areaId });
  } catch { await refreshBoard(); }
}

async function releaseFromHoldingArea(jobId) {
  const idx = jobs.value.findIndex((j) => String(j.id) === String(jobId));
  if (idx === -1) return;
  jobs.value[idx] = { ...jobs.value[idx], holding_area_id: null };
  try {
    await api.patch(`/api/jobs/${jobId}`, { holding_area_id: null });
  } catch { await refreshBoard(); }
}

async function deleteHoldingArea(areaId) {
  try {
    await api.del(`/api/holding-areas/${areaId}`);
    await fetchHoldingAreas();
    await refreshBoard();
  } catch (err) {
    console.error("delete_holding_area_failed", areaId, err);
    toast.add({
      severity: "error",
      summary: "Delete failed",
      detail: err?.message || "Could not delete holding area",
      life: 5000,
    });
  }
}

const creatingHoldingArea = ref(false);
async function createHoldingArea() {
  // Doug 2026-05-10: "+ add area does not work." Root cause was a UI gap —
  // the button toggled showAddAreaDialog but no Dialog template watched
  // the ref and no submit handler existed. Backend has had POST
  // /api/holding-areas all along (gdx/routers/holding_areas.py:62).
  const name = (newAreaName.value || "").trim();
  if (!name) return;
  creatingHoldingArea.value = true;
  try {
    await api.post("/api/holding-areas", {
      name,
      color: newAreaColor.value || "#6b7280",
    });
    newAreaName.value = "";
    newAreaColor.value = "#6b7280";
    showAddAreaDialog.value = false;
    await fetchHoldingAreas();
    toast.add({
      severity: "success",
      summary: "Holding area added",
      detail: `"${name}" is ready for jobs.`,
      life: 3000,
    });
  } catch (err) {
    console.error("create_holding_area_failed", err);
    toast.add({
      severity: "error",
      summary: "Could not add area",
      detail: err?.message || "Try again.",
      life: 5000,
    });
  } finally {
    creatingHoldingArea.value = false;
  }
}

async function seedHoldingAreas() {
  try {
    await api.post("/api/holding-areas/seed-defaults", {});
    await fetchHoldingAreas();
  } catch (err) {
    console.error("seed_holding_areas_failed", err);
    toast.add({
      severity: "error",
      summary: "Seed failed",
      detail: err?.message || "Could not seed holding areas",
      life: 5000,
    });
  }
}

async function refreshBoard() {
  refreshing.value = true;
  try {
    await Promise.all([fetchTechnicians(), fetchJobs(), fetchHoldingAreas(), fetchScheduledUnassigned()]);
    await loadSkillOptions();
  } finally {
    refreshing.value = false;
  }
}

// Sprint 5 / S5-C2 — live tech locations (table view; map widget is a follow-up).
const liveTechs = ref([]);
let liveTechsTimer = null;
async function refreshLiveTechs() {
  try {
    const data = await api.get('/api/dispatch/locations?minutes=30');
    liveTechs.value = Array.isArray(data) ? data : [];
  } catch {
    liveTechs.value = [];
  }
}

onMounted(async () => {
  await loadDispatchSettings();
  refreshBoard();
  refreshLiveTechs();
  liveTechsTimer = setInterval(refreshLiveTechs, 30_000);
});

onBeforeUnmount(() => { if (liveTechsTimer) clearInterval(liveTechsTimer); });

// Sprint dispatch-capacity (2026-05-21) — expose the surface the
// regression specs reach via wrapper.vm. Without defineExpose, the
// existing test_dispatch_drag.test.js + test_dispatch_capacity.test.js
// rely on Vue's dev-mode auto-expose, which a strict prod build can
// seal. Pinning the test contract explicitly is the cheapest fix.
// /audit 2026-05-21 finding 3.
defineExpose({
  // Existing dispatch surface (test_dispatch_drag.test.js)
  onDragStart,
  handleDrop,
  draggingJobId,
  // Sprint dispatch-capacity / dispatch-timeline surface
  assignJob,
  onTimelinePlace,
  onTimelinePlaceTray,
  confirmDurationPrompt,
  durationPromptOpen,
  durationPromptJobId,
  durationPromptTechId,
  durationPromptScheduledAt,
});
</script>

<style scoped>
.dispatch-view {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.dispatch-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.75rem;
}

.dispatch-title {
  margin: 0;
  font-size: 1.4rem;
  font-weight: 700;
}

.toolbar-controls {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.dispatch-range-picker {
  min-width: 220px;
}

.dispatch-calendar {
  max-width: 180px;
}

.date-controls {
  display: flex;
  align-items: center;
  gap: 0.25rem;
}

.toolbar-actions {
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.job-card-quick-actions {
  display: flex;
  gap: 0.35rem;
  flex-wrap: wrap;
  align-items: center;
}

.skill-filter-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.9rem;
  padding: 0 0.25rem;
}

.skill-filter-select {
  min-width: 200px;
  max-width: 280px;
}

.dispatch-map-panel {
  border: 1px solid var(--p-border-color);
  border-radius: var(--p-border-radius);
  background: var(--p-content-background);
  padding: 1rem;
  box-shadow: 0 1px 4px rgba(15, 23, 42, 0.08);
}

.dispatch-map-panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.5rem;
}

.dispatch-map-body {
  min-height: 80px;
}

.map-loading {
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
}

.map-table {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.map-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) repeat(2, 140px);
  gap: 0.5rem;
  padding: 0.35rem 0;
  border-bottom: 1px dashed var(--p-border-color);
  font-size: 0.85rem;
}

.map-row:last-child {
  border-bottom: none;
}

.map-tech {
  font-weight: 600;
}

.map-coords,
.map-timestamp {
  color: var(--p-text-muted-color);
}

.route-order-summary {
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
  padding: 0 0.25rem;
}

.job-detail-drawer :deep(.p-drawer-content) {
  width: 360px;
  max-width: 100%;
  padding: 1rem;
}

.job-drawer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.75rem;
}

.job-drawer-content {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.job-drawer-line {
  margin: 0;
}

.job-drawer-status {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.job-drawer-close {
  font-size: 1.2rem;
  line-height: 1;
}

/* Sections */
.board-section {
  margin-bottom: 0;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 1rem;
}

.section-icon {
  color: var(--p-orange-500);
}

/* Unassigned grid */
/* Holding Areas */
.holding-areas-section { margin: 1rem 0; }
.holding-areas-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
.holding-areas-header h3 { margin: 0; font-size: 1rem; }
.holding-areas-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 0.75rem; }
.holding-area-col { min-height: 120px; }
.holding-area-title { display: flex; align-items: center; gap: 0.5rem; }
.holding-job-card { display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; }

.unassigned-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 0.75rem;
}

/* Tech columns */
.tech-columns-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 1rem;
}

.tech-column {
  transition: box-shadow 0.15s;
}

.tech-column.drag-over {
  box-shadow: 0 0 0 2px var(--p-primary-color);
}

.tech-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.tech-avatar {
  width: 2rem;
  height: 2rem;
  font-size: 0.85rem;
}

/* Sprint dispatch-capacity (2026-05-20) — per-column day load indicator.
   Sub-line under the tech name shows "scheduled of capacity" + a thin bar. */
.tech-capacity {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  margin-top: 0.25rem;
  font-size: 0.78rem;
  color: var(--p-text-muted-color, #6b7280);
}
.tech-capacity-label { font-weight: 500; }
.tech-capacity-unknown {
  margin-left: 0.4rem;
  font-weight: 400;
  font-size: 0.72rem;
  opacity: 0.8;
}
.tech-capacity-bar {
  height: 4px;
  background: var(--p-surface-300, #e5e7eb);
  border-radius: 2px;
  overflow: hidden;
}
.tech-capacity-bar-fill {
  height: 100%;
  background: var(--p-primary-color, #2563eb);
  transition: width 0.2s ease-out;
}
.tech-capacity--over .tech-capacity-bar-fill {
  background: var(--p-red-500, #ef4444);
}
.tech-capacity--over .tech-capacity-label { color: var(--p-red-500, #ef4444); }
.tech-capacity--off { opacity: 0.55; }
.job-duration { font-weight: 500; opacity: 0.85; }
.holding-area-total {
  margin-top: 0.2rem;
  font-size: 0.75rem;
  color: var(--p-text-muted-color, #6b7280);
  font-weight: 500;
}

/* Job cards */
.job-card {
  border: 1px solid var(--p-content-border-color);
  border-radius: var(--p-border-radius);
  padding: 0.75rem;
  margin-bottom: 0.5rem;
  cursor: grab;
  transition: border-color 0.15s, transform 0.1s;
  background: var(--p-content-background);
}

.job-card:hover {
  border-color: var(--p-primary-color);
}

.job-card:active {
  cursor: grabbing;
  transform: scale(0.98);
}

.scheduled-overdue {
  background: rgba(220, 38, 38, 0.06);
}

.unassigned-card {
  border-left: 3px solid var(--p-orange-500);
}

.job-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 0.4rem;
}

.job-customer {
  font-weight: 700;
  font-size: 0.9rem;
}

.job-card-body {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.job-line {
  margin: 0;
  font-size: 0.82rem;
  color: var(--p-text-muted-color);
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.job-card-actions {
  margin-top: 0.5rem;
}

.assign-dropdown {
  width: 100%;
}

.empty-message {
  margin: 0;
  padding: 1rem;
  text-align: center;
  color: var(--p-text-muted-color);
  font-size: 0.85rem;
}

.drop-hint {
  border: 2px dashed var(--p-content-border-color);
  border-radius: var(--p-border-radius);
}

/* Week grid */
.week-grid {
  display: grid;
  grid-template-columns: repeat(7, 1fr);
  gap: 0.5rem;
}

.week-day-header {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.85rem;
}

.day-name {
  font-weight: 700;
  text-transform: uppercase;
  font-size: 0.75rem;
  color: var(--p-text-muted-color);
}

.day-date {
  font-weight: 600;
}

.week-day-col.is-today {
  border: 2px solid var(--p-primary-color);
}

.week-job-card {
  padding: 0.5rem;
  font-size: 0.8rem;
}

.assign-dialog {
  width: 400px;
}

.add-area-form {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}
.add-area-form .form-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.add-area-form .form-field label {
  font-size: 0.85rem;
  font-weight: 500;
  color: var(--p-text-muted-color, #6b7280);
}
.add-area-form .color-picker {
  width: 4rem;
  height: 2.5rem;
  padding: 0;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.5rem;
  cursor: pointer;
  background: transparent;
}

@media (max-width: 900px) {
  .week-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .tech-columns-grid {
    grid-template-columns: 1fr;
  }

  .unassigned-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 480px) {
  .dispatch-toolbar {
    flex-direction: column;
    align-items: flex-start;
  }

  .week-grid {
    grid-template-columns: 1fr;
  }
}
</style>
