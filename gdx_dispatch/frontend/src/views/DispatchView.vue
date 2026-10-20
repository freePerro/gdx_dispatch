<template>
    <section class="dispatch-view view-card">
      <!-- Toolbar -->
      <div class="dispatch-toolbar">
        <h2 class="dispatch-title">Dispatch Board</h2>
        <div class="toolbar-controls">
          <div class="date-controls">
            <Button
              v-tooltip="viewMode === 'week' ? 'Previous week' : 'Previous day'"
              icon="pi pi-angle-left"
              :aria-label="viewMode === 'week' ? 'Previous week' : 'Previous day'"
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
              v-tooltip="viewMode === 'week' ? 'Next week' : 'Next day'"
              icon="pi pi-angle-right"
              :aria-label="viewMode === 'week' ? 'Next week' : 'Next day'"
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
          <!-- Day view density (2026-10-15): shorter hours and narrower tech
               columns so more of the day and more techs fit on one screen.
               Remembered on this browser. -->
          <Button
            v-if="viewMode === 'day'"
            v-tooltip="compactBoard ? 'Roomy layout' : 'Compact layout'"
            :icon="compactBoard ? 'pi pi-window-maximize' : 'pi pi-window-minimize'"
            :aria-label="compactBoard ? 'Roomy layout' : 'Compact layout'"
            :severity="compactBoard ? 'primary' : 'secondary'"
            size="small"
            data-testid="dispatch-compact-toggle"
            @click="compactBoard = !compactBoard"
          />
          <Button
            v-tooltip="'Refresh dispatch board'"
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
            v-tooltip="'Geocode missing addresses'"
            aria-label="Geocode missing addresses"
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
            v-tooltip="'Refresh technician map'"
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

      <!-- Past their date, not closed out (2026-09-27). The board loads only
           undated jobs and the dates in view, so the day after a visit an
           open job stopped being on any screen — prod had six, the oldest
           scheduled 2026-06-02, found by SQL rather than by anyone looking.
           Same contract as the labor card below: it renders only when
           something is wrong, and dealing with the job (closing it out, or
           giving it a new date from the job page) IS the dismissal. -->
      <Card v-if="lateOpenJobs.length" class="board-section" :class="{ 'is-folded': lateOpenCollapsed }" data-testid="late-open-jobs">
        <template #title>
          <div class="section-header">
            <span class="section-icon pi pi-history" style="color: var(--p-red-500)"></span>
            <span>Past their date, not closed out</span>
            <Tag :value="String(lateOpenJobs.length)" severity="danger" rounded />
            <!-- The tech columns and intake queue must stay reachable without
                 scrolling (you cannot scroll mid-drag — see the Day View
                 note), so the list is short and can be folded away while the
                 board is open. The fold lasts for the browser tab (sessionStorage)
                 so a reload does not bring the table back mid-shift; a new
                 tab starts open again, by design. The header and count stay
                 put either way. -->
            <Button :label="lateOpenCollapsed ? 'Show' : 'Hide'" size="small" text
              style="margin-left: auto" data-testid="late-open-toggle"
              @click="lateOpenCollapsed = !lateOpenCollapsed" />
          </div>
        </template>
        <template #content>
          <DataTable v-if="!lateOpenCollapsed" responsiveLayout="scroll" :value="lateOpenJobs" :rows="3"
            :paginator="lateOpenJobs.length > 3" size="small" stripedRows dataKey="id">
            <Column header="Scheduled">
              <template #body="{ data }">
                {{ formatScheduled(data.scheduled_at) }}
                <small class="muted" :data-testid="`late-open-days-${data.id}`"> · {{ lateLabel(data.days_late) }}</small>
              </template>
            </Column>
            <Column header="Customer">
              <template #body="{ data }">{{ data.customer_name || '—' }}</template>
            </Column>
            <Column header="Job">
              <template #body="{ data }">
                <i v-if="data.is_return_visit" class="pi pi-replay return-visit-icon" v-tooltip="'Return visit'" />
                {{ data.title || data.job_number || '—' }}
              </template>
            </Column>
            <Column header="Tech">
              <template #body="{ data }">{{ data.tech_name || 'No tech' }}</template>
            </Column>
            <Column header="State">
              <template #body="{ data }"><JobStateChip :job="data" /></template>
            </Column>
            <Column header="Action">
              <template #body="{ data }">
                <div style="display: flex; gap: 0.4rem; white-space: nowrap;">
                  <Button label="Close out" icon="pi pi-check" size="small" severity="success"
                    :data-testid="`late-open-closeout-${data.id}`" @click="openCloseout(data)" />
                  <Button label="Open job" icon="pi pi-external-link" size="small" severity="secondary"
                    :data-testid="`late-open-open-${data.id}`" @click="goToJob(data)" />
                </div>
              </template>
            </Column>
          </DataTable>
        </template>
      </Card>

      <!-- Labor exceptions — shifts the office needs to correct.
           Deliberately not a report: it renders only when something is wrong,
           on the screen dispatch already has open, so nobody has to remember
           to run it and it cannot nag on a clean day. Correcting the shift IS
           the dismissal — the row drops out on the next load. -->
      <Card v-if="laborExceptions.length" class="board-section" :class="{ 'is-folded': laborCollapsed }" data-testid="labor-exceptions">
        <template #title>
          <div class="section-header">
            <span class="section-icon pi pi-exclamation-triangle" style="color: var(--p-amber-500)"></span>
            <span>Timeclock needs a look</span>
            <Tag :value="String(laborExceptions.length)" severity="warn" rounded />
            <Button :label="laborCollapsed ? 'Show' : 'Hide'" size="small" text
              style="margin-left: auto" data-testid="labor-exceptions-toggle"
              @click="laborCollapsed = !laborCollapsed" />
          </div>
        </template>
        <template #content>
          <DataTable v-if="!laborCollapsed" responsiveLayout="scroll" :value="laborExceptions" :rows="5" size="small" stripedRows>
            <Column header="Who">
              <template #body="{ data }">
                {{ data.tech_name || 'Unknown tech' }}
              </template>
            </Column>
            <Column header="Started">
              <template #body="{ data }">{{ formatDateTime(data.started_at) }}</template>
            </Column>
            <Column header="What">
              <template #body="{ data }">
                {{ data.detail }}
                <small v-if="data.hours" class="muted"> · recorded {{ data.hours }}h</small>
              </template>
            </Column>
            <Column header="Action" style="width: 10rem">
              <template #body="{ data }">
                <Button label="Fix" icon="pi pi-clock" size="small" severity="secondary"
                  @click="fixLaborException(data)" data-testid="fix-labor-exception" />
              </template>
            </Column>
          </DataTable>
        </template>
      </Card>

      <!-- Lane strip — every holding area as a compact lane directly above
           the board, day and week alike (Doug, 2026-10-16). On the day board
           the lanes used to sit under every tech column, each as tall as the
           tallest one, so a job could not be dragged from one lane to another
           or onto a tech: you cannot scroll mid-drag. Each lane is two rows of
           small cards with its own scroll. The week board also carries "To
           schedule" here, where a job goes back to; the day board has its
           New Jobs queue under the tech columns. Every card has a Move to…
           menu for the same moves without a drag. -->
      <div v-if="viewMode === 'day' || viewMode === 'week'" class="lane-strip" data-testid="lane-strip">
        <div
          v-if="viewMode === 'week'"
          class="week-queue"
          :class="{ 'drag-over': dragOverTechId === 'unassigned-queue' }"
          data-testid="week-queue"
          @dragover.prevent="onDragOver('unassigned-queue')"
          @dragleave="onWeekCellLeave"
          @drop.prevent="moveToScheduleQueue($event)"
        >
          <span class="week-queue-label">
            <i class="pi pi-inbox"></i> To schedule
            <Badge v-if="weekQueueJobs.length" :value="weekQueueJobs.length" severity="danger" />
          </span>
          <div class="week-queue-chips">
            <div
              v-for="job in weekQueueJobs"
              :key="job.card_key || job.id"
              class="week-chip week-queue-chip"
              :class="{ 'week-chip--partial': partialJobIds.has(String(job.id)) }"
              :data-testid="`week-queue-${job.card_key || job.id}`"
              draggable="true"
              v-tooltip.bottom="weekChipTooltip(job)"
              @dragstart="onDragStart(job, $event, null, 'week-queue')"
              @dragend="draggingJobId = null"
              @click="openJobDrawer(job)"
            >
              <span class="week-chip-customer">{{ displayCustomer(job) }}</span>
              <span class="week-chip-meta">{{ partialJobIds.has(String(job.id)) ? 'next day' : formatDurationHours(job.effective_duration_hours) }}</span>
              <button
                v-if="parkingHoldingAreas.length"
                type="button"
                class="lane-icon-button"
                draggable="false"
                v-tooltip.bottom="'Move to…'"
                aria-label="Move to…"
                :data-testid="`lane-job-menu-${job.id}`"
                @click.stop="openLaneMenu($event, job)"
              ><i class="pi pi-ellipsis-v"></i></button>
            </div>
            <span v-if="!weekQueueJobs.length" class="week-cell-note">Drop a job here to take it off the schedule</span>
          </div>
        </div>
        <div v-if="parkingHoldingAreas.length || showRedLane" class="lane-areas">
          <!-- Scheduled — Not Assigned (system lane, 2026-05-01): upcoming
               dated jobs with no tech, from /api/dispatch/scheduled-unassigned.
               Read-only — not a drop target, can't be deleted. Today's dated
               jobs with no tech stay in the day's "New Jobs to Schedule". -->
          <div v-if="showRedLane" class="week-queue lane-area lane-area--alert" data-testid="scheduled-unassigned-lane">
            <div class="lane-head">
              <span class="week-queue-label">
                <span class="lane-dot" style="background: var(--p-red-500)"></span>
                Scheduled — Not Assigned
                <Badge v-if="scheduledUnassigned.length" :value="scheduledUnassigned.length" severity="danger" />
              </span>
            </div>
            <div class="week-queue-chips">
              <div
                v-for="job in sortedScheduledUnassigned"
                :key="job.id"
                class="week-chip week-queue-chip lane-chip scheduled-unassigned-card"
                :class="{ 'scheduled-overdue': isOverdue(job) }"
                :data-testid="`scheduled-unassigned-job-${job.id}`"
                draggable="true"
                v-tooltip.bottom="weekChipTooltip(job)"
                @dragstart="onDragStart(job, $event)"
                @dragend="draggingJobId = null"
                @click="openJobDrawer(job)"
              >
                <span class="week-chip-customer">{{ displayCustomer(job) === 'No customer attached (lead)' ? '—' : displayCustomer(job) }}</span>
                <span class="week-chip-meta">{{ isOverdue(job) ? 'OVERDUE · ' : '' }}{{ formatScheduled(job.scheduled_at) }}</span>
              </div>
              <span v-if="!scheduledUnassigned.length" class="week-cell-note">No upcoming jobs missing a tech.</span>
            </div>
          </div>
          <div
            v-for="area in parkingHoldingAreas"
            :key="area.id"
            class="week-queue lane-area"
            :class="{ 'drag-over': dragOverTechId === area.id }"
            :style="{ borderLeft: '3px solid ' + area.color }"
            :data-testid="`lane-${area.id}`"
            @dragover.prevent="onDragOver(area.id)"
            @dragleave="onWeekCellLeave"
            @drop.prevent="moveToHoldingArea($event, area.id)"
          >
            <div class="lane-head">
              <span class="week-queue-label">
                <span class="lane-dot" :style="{ background: area.color }"></span>
                {{ area.name }}
                <Badge v-if="getHoldingAreaJobs(area.id).length" :value="getHoldingAreaJobs(area.id).length" severity="info" />
              </span>
              <span v-if="getHoldingAreaJobs(area.id).length" class="lane-total" :data-testid="`lane-total-${area.id}`">
                {{ formatDurationHours(sumDurationHours(getHoldingAreaJobs(area.id)).known) }} queued
                <span v-if="sumDurationHours(getHoldingAreaJobs(area.id)).unknown" class="tech-capacity-unknown">
                  + {{ sumDurationHours(getHoldingAreaJobs(area.id)).unknown }} no-est
                </span>
              </span>
              <button
                v-if="viewMode === 'day'"
                type="button"
                class="lane-icon-button lane-delete"
                v-tooltip.bottom="'Delete this area'"
                :aria-label="`Delete ${area.name}`"
                :data-testid="`lane-delete-${area.id}`"
                @click="deleteHoldingArea(area)"
              ><i class="pi pi-trash"></i></button>
            </div>
            <div class="week-queue-chips">
              <div
                v-for="job in getHoldingAreaJobs(area.id)"
                :key="job.card_key || job.id"
                class="week-chip week-queue-chip lane-chip"
                :style="{ borderLeftColor: area.color }"
                :data-testid="`lane-job-${area.id}-${job.id}`"
                :draggable="job.visit_id && !job.visit_movable ? 'false' : 'true'"
                v-tooltip.bottom="weekChipTooltip(job)"
                @dragstart="onDragStart(job, $event, null, `area:${area.id}`)"
                @dragend="draggingJobId = null"
                @click="openJobDrawer(job)"
              >
                <span class="week-chip-customer">{{ displayCustomer(job) === 'No customer attached (lead)' ? '—' : displayCustomer(job) }}</span>
                <span v-if="displayTitle(job)" class="lane-chip-title">{{ displayTitle(job) }}</span>
                <span class="week-chip-meta">{{ formatDurationHours(job.effective_duration_hours) }}</span>
                <button
                  type="button"
                  class="lane-icon-button"
                  draggable="false"
                  v-tooltip.bottom="'Move to…'"
                  aria-label="Move to…"
                  :data-testid="`lane-job-menu-${job.id}`"
                  @click.stop="openLaneMenu($event, job)"
                ><i class="pi pi-ellipsis-v"></i></button>
              </div>
              <span v-if="!getHoldingAreaJobs(area.id).length" class="week-cell-note">Drop jobs here</span>
            </div>
          </div>
          <Button v-if="viewMode === 'day'" icon="pi pi-plus" label="Add Area" size="small" severity="secondary" text
            class="lane-add" @click="showAddAreaDialog = true" data-testid="add-holding-area" />
        </div>
        <div v-else-if="viewMode === 'day'">
          <Button label="Set Up Holding Areas" icon="pi pi-plus" severity="secondary" size="small"
            @click="seedHoldingAreas" data-testid="seed-holding-areas" />
        </div>
      </div>

      <!-- Day View -->
      <div v-if="viewMode === 'day'" style="display: flex; flex-direction: column;">
        <!-- Unassigned Jobs — SECOND, directly under the tech grid.
             History, because this has now been wrong in both directions
             (Doug, 2026-08-10): it originally sat at order:2 below the techs
             AND the holding areas, so a freshly created job landed below the
             fold — "my new job never showed up on dispatch". The fix for that
             moved it to order:1, which pushed the tech columns to the bottom
             and took the assign-to-tech drop targets below the fold instead —
             you cannot scroll mid-drag, so assigning by drag became
             impossible. Both lanes have to be reachable without scrolling, so
             the two the dispatcher actually works between are adjacent: techs
             first (they are the point of the board), this queue immediately
             under it, and the genuine parking lots last. Don't reorder these
             without dragging a job from this queue onto a tech first.
             2026-10-05 (multi-day jobs D11): Partial Jobs sits between the
             techs and this queue, at order:2, so both drop sources stay one
             short reach from the tech columns; this queue is order:3 and the
             parking lots order:4. Hidden when empty, so on most days the
             layout is the one described above. -->
        <!-- Partial Jobs — a day of the job was worked and no further day is
             booked (GET /api/dispatch/partial-jobs). Without this the job
             drops off the board the day after it was worked: it has a past
             date, a tech, and no visit coming. Dragging a card onto a tech is
             the board's ordinary drop — PATCH /api/jobs with that tech and
             the day — which books the next visit. -->
        <Card v-if="visiblePartialJobs.length" class="board-section" data-testid="partial-jobs" style="order: 2;">
          <template #title>
            <div class="section-header">
              <span class="section-icon pi pi-step-forward" style="color: var(--p-amber-500)"></span>
              Partial Jobs — Need to Schedule
              <Badge :value="visiblePartialJobs.length" severity="warn" />
            </div>
          </template>
          <template #content>
            <div class="unassigned-grid">
              <div
                v-for="job in visiblePartialJobs"
                :key="job.id"
                class="job-card unassigned-card"
                :data-testid="`partial-job-${job.id}`"
                draggable="true"
                style="cursor:pointer"
                @dragstart="onDragStart(job, $event)"
                @dragend="draggingJobId = null"
                @click="goToJob(job)"
              >
                <div class="job-card-header">
                  <span class="job-customer">
                    <i v-if="job.is_return_visit" class="pi pi-replay return-visit-icon" v-tooltip="'Return visit'" />
                    {{ job.customer_name || 'No customer' }}
                  </span>
                  <JobStateChip :job="job" />
                </div>
                <div class="job-card-body">
                  <p class="job-line job-title-line">{{ job.title || job.job_number || 'Job' }}</p>
                  <p class="job-line job-line-dated" :data-testid="`partial-job-worked-${job.id}`">
                    <i class="pi pi-calendar"></i> {{ partialWorkedLabel(job) }}
                  </p>
                </div>
              </div>
            </div>
          </template>
        </Card>

        <Card class="board-section unassigned-section-card" data-testid="unassigned-section" style="order: 3;"
          :class="{ 'drag-over': dragOverTechId === 'unassigned-queue' }"
          @dragover.prevent="onDragOver('unassigned-queue')"
          @dragleave="dragOverTechId = null"
          @drop.prevent="moveToScheduleQueue($event)">
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
                :key="job.card_key || job.id"
                class="job-card unassigned-card"
                :data-testid="`unassigned-job-${job.card_key || job.id}`"
                draggable="true"
                style="cursor:pointer"
                @dragstart="onDragStart(job, $event, null, 'day-queue')"
                @dragend="draggingJobId = null"
                @click="openJobDrawer(job)"
              >
                <div class="job-card-header">
                  <span class="job-customer">
                    <i v-if="job.is_return_visit" class="pi pi-replay return-visit-icon" v-tooltip="'Return visit'" data-testid="return-visit-icon" />
                    {{ displayCustomer(job) }}
                  </span>
                  <JobStateChip :job="job" data-testid="job-status-tag" />
                </div>
                <div class="job-card-body">
                  <p v-if="displayTitle(job)" class="job-line job-title-line" :data-testid="`unassigned-title-${job.id}`">{{ displayTitle(job) }}</p>
                  <p v-if="visitDayLabel(job)" class="job-line" :data-testid="`day-badge-${job.card_key}`"><i class="pi pi-calendar-clock"></i> {{ visitDayLabel(job) }}</p>
                  <p class="job-line"><i class="pi pi-briefcase"></i> {{ job.job_type || 'Service' }}</p>
                  <!-- This section now also carries dated-but-untechhed jobs
                       (see unassignedJobs), so the card has to say which it
                       is — "needs a date" and "needs a tech" are different
                       jobs for the dispatcher. -->
                  <p v-if="job.scheduled_at" class="job-line job-line-dated" :data-testid="`unassigned-date-${job.id}`">
                    <i class="pi pi-calendar"></i> {{ formatScheduled(job.scheduled_at) }} · needs a tech
                  </p>
                  <p v-else class="job-line"><i class="pi pi-clock"></i> {{ job.time_window || 'Anytime' }} · <span class="job-duration">{{ formatDurationHours(job.effective_duration_hours) }}</span></p>
                  <!-- Self-assigned at create (mobile "Assign to me"): the
                       tech is known, the date isn't — the opposite gap from
                       "needs a tech" above. -->
                  <p v-if="!job.scheduled_at && job.technician_id" class="job-line job-line-dated" :data-testid="`unassigned-tech-${job.id}`">
                    <i class="pi pi-user"></i> {{ techName(job.technician_id) }} · needs a date
                  </p>
                  <p v-if="job.address" class="job-line"><i class="pi pi-map-marker"></i> {{ job.address }}</p>
                </div>
                <div class="job-card-actions" @click.stop>
                  <Select
                    :key="`assign-${queuePickerKey}`"
                    :options="assignableTechnicians"
                    optionLabel="name"
                    optionValue="id"
                    placeholder="Assign to tech..."
                    class="assign-dropdown"
                    :data-testid="`assign-dropdown-${job.card_key || job.id}`"
                    @change="job.visit_id ? assignVisitTech(job, $event.value) : assignFromPicker(job.id, $event.value)"
                  />
                  <!-- Assigning a tech dates the job to the selected day
                       (_doAssignJobInner case c). When the dispatcher needs a
                       specific date/time instead, send them to the job's
                       schedule dialog — the one place that writes
                       Job.scheduled_at properly. -->
                  <!-- Not on one day of a multi-day job: the job's schedule
                       dialog acts on its first open day. That day's Move is
                       on the job's Visits card. -->
                  <Button
                    v-if="!job.visit_id"
                    icon="pi pi-calendar-plus"
                    severity="secondary"
                    outlined
                    size="small"
                    v-tooltip="'Pick a date and time'"
                    aria-label="Pick a date and time"
                    :data-testid="`schedule-job-${job.id}`"
                    @click="openScheduleDialog(job.id)"
                  />
                  <Button
                    v-if="parkingHoldingAreas.length"
                    icon="pi pi-ellipsis-v"
                    severity="secondary"
                    text
                    size="small"
                    v-tooltip="'Move to…'"
                    aria-label="Move to…"
                    :data-testid="`lane-job-menu-${job.id}`"
                    @click="openLaneMenu($event, job)"
                  />
                </div>
              </div>
              <p v-if="!unassignedJobs.length" class="empty-message drop-hint">
                <i class="pi pi-check-circle"></i> Nothing waiting to be scheduled — drop a job here to queue it.
              </p>
            </div>
          </template>
        </Card>

        <!-- Hide this row entirely when no tech has any skill data — renders
             cleaner than a permanently-disabled control. NOTHING but the skill
             filter belongs in here: a "Show Completed Jobs" button once shared
             the row and inherited this guard, which made it unreachable on
             every tenant whose techs have no skills (removed 2026-08-28 —
             completed jobs now always show; see rangeFilteredJobs). -->
        <div class="skill-filter-row" style="order: 0;" v-if="skillOptions.length">
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
        </div>

        <!-- Technician Columns — FIRST in the day view, so they sit directly
             under the "Timeclock needs a look" card and are visible without
             scrolling. These carry the assign-to-tech drop targets (the
             TechTimelineColumn body/tray), which is the board's primary
             action; anything above them pushes those targets off screen. -->
        <div class="tech-columns-grid" :class="{ 'is-compact': compactBoard }" style="order: 1;">
          <Card
            v-for="tech in technicianColumns"
            :key="tech.id"
            class="tech-column"
            :class="{ 'drag-over': dragOverTechId === tech.id && !tech.timeOff, 'tech-column--time-off': tech.timeOff }"
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
                <span v-if="tech.timeOff" class="tech-capacity-label">Time off</span>
                <span v-else-if="tech.isOffToday" class="tech-capacity-label">Off today</span>
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
              <div
                v-if="tech.timeOff"
                class="time-off-block"
                role="note"
                :data-testid="`tech-time-off-${tech.id}`"
              >
                <i class="pi pi-calendar-minus" aria-hidden="true" />
                <span class="time-off-block-title">Time off · {{ tech.timeOff.label }}</span>
                <span class="time-off-block-meta">{{ timeOffRange(tech.timeOff) }} · approved, takes no new work</span>
              </div>
              <!-- Sprint dispatch-timeline (2026-05-21) — vertical hour-axis
                   layout. The TechTimelineColumn owns drops + tray + block
                   positioning. Card-wrapper @drop above is now a no-op for
                   timeline-area drops; the timeline body intercepts them. -->
              <TechTimelineColumn
                :tech="tech"
                :jobs="tech.jobs"
                :selected-date="selectedDate"
                :px-per-hour="compactBoard ? 30 : 48"
                @open-drawer="openJobDrawer"
                @job-drag-start="(job, e) => onDragStart(job, e, tech.id)"
                @job-drag-end="draggingJobId = null"
                @place="onTimelinePlace"
                @place-tray="onTimelinePlaceTray"
              />
            </template>
          </Card>
        </div>

      </div>

      <!-- Sprint dispatch-capacity (2026-05-20) — per-tech efficiency leaderboard -->
      <!-- Tier-7: the panel's endpoints require dispatch.read — sales/
           accounting can open this page but the panel 403'd inside it. -->
      <TechEfficiencyPanel v-if="viewMode === 'day' && hasPermission('dispatch.read')" data-testid="dispatch-efficiency-panel" />

      <!-- Week View — technicians down the side, days across the top
           (2026-10-15, Doug: "everything takes up too much space" and the
           week view could not take a drop). Every cell is a drop target: a
           card dropped on a tech's day lands on that day, at the time it
           already had, with that tech. The arrows step a whole week here.
           Same shape as ServiceTitan's Weekly Dispatch Board. -->
      <template v-if="viewMode === 'week'">
        <div class="week-board-scroll">
          <div class="week-board" data-testid="week-board">
            <div class="week-corner">Tech</div>
            <button
              v-for="day in weekDays"
              :key="`h-${day.date}`"
              type="button"
              class="week-day-head"
              :class="{ 'is-today': day.isToday }"
              :data-testid="`week-day-head-${day.date}`"
              v-tooltip.bottom="'Open this day'"
              @click="openDay(day.date)"
            >
              <span class="day-name">{{ day.dayName }}</span>
              <span class="day-date">{{ day.dateLabel }}</span>
              <Badge v-if="day.jobs.length" :value="day.jobs.length" severity="info" />
            </button>

            <template v-for="row in weekRows" :key="row.id">
              <div class="week-tech" :class="{ 'week-tech--unassigned': row.unassigned }">
                <span class="week-tech-name">{{ row.name }}</span>
                <span v-if="!row.unassigned" class="week-tech-hours">{{ formatDurationHours(row.weekHours) }} this week</span>
              </div>
              <div
                v-for="cell in row.cells"
                :key="`${row.id}-${cell.date}`"
                class="week-cell"
                :class="{
                  'is-today': cell.isToday,
                  'is-off': cell.isOff,
                  'is-time-off': cell.timeOff,
                  'is-over': cell.isOver,
                  'drag-over': dragOverTechId === `${row.id}|${cell.date}` && !cell.timeOff,
                }"
                :data-testid="`week-cell-${row.id}-${cell.date}`"
                @dragover.prevent="onDragOver(`${row.id}|${cell.date}`)"
                @dragleave="onWeekCellLeave"
                @drop.prevent="onWeekCellDrop($event, row.unassigned ? null : row.id, cell.date)"
              >
                <span
                  v-if="cell.timeOff"
                  class="week-time-off"
                  v-tooltip.bottom="`${cell.timeOff.label}, ${timeOffRange(cell.timeOff)} (approved)`"
                  :data-testid="`week-time-off-${row.id}-${cell.date}`"
                >Time off · {{ cell.timeOff.label }}</span>
                <div
                  v-for="job in cell.jobs"
                  :key="job.card_key || job.id"
                  class="week-chip"
                  :class="{ 'week-chip--locked': job.visit_id && !job.visit_movable }"
                  :data-testid="`week-job-${cell.date}-${job.card_key || job.id}`"
                  :draggable="job.visit_id && !job.visit_movable ? 'false' : 'true'"
                  v-tooltip.bottom="weekChipTooltip(job)"
                  @dragstart="onDragStart(job, $event, row.unassigned ? null : row.id)"
                  @dragend="draggingJobId = null"
                  @click="openJobDrawer(job)"
                >
                  <span class="week-chip-time">{{ weekChipTime(job) }}</span>
                  <span class="week-chip-customer">
                    <i v-if="job.is_return_visit" class="pi pi-replay return-visit-icon" />
                    {{ displayCustomer(job) }}
                  </span>
                  <span v-if="visitDayLabel(job)" class="week-chip-meta" :data-testid="`week-day-badge-${job.card_key}`">{{ visitDayLabel(job) }}</span>
                </div>
                <span v-if="cell.isOff && !cell.timeOff && !cell.jobs.length" class="week-cell-note">off</span>
                <span v-else-if="cell.hoursLabel" class="week-cell-note" :class="{ 'is-over': cell.isOver }">{{ cell.hoursLabel }}</span>
              </div>
            </template>
          </div>
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

      <!-- Week board: a job with no time dropped on a day that already has
           work asks where it goes in that day's order. An empty day needs no
           question — it starts at the tech's shift start. -->
      <Dialog
        v-model:visible="slotPromptOpen"
        :header="slotPromptHeader"
        :modal="true"
        :closable="true"
        :style="{ width: '440px' }"
        data-testid="week-slot-prompt"
        @hide="resolveSlotPrompt(null)"
      >
        <p style="margin-top:0;">
          Where does <strong>{{ slotPromptJobLabel }}</strong> go?
        </p>
        <ol class="week-slot-list">
          <li v-for="item in slotPromptItems" :key="item.key">
            <button
              v-if="item.slot"
              type="button"
              class="week-slot-option"
              :class="{ 'is-selected': slotIsChosen(item) }"
              :data-testid="`week-slot-${item.index}`"
              @click="chooseSlot(item)"
            >
              <span class="pi" :class="slotIsChosen(item) ? 'pi-circle-fill' : 'pi-circle'" />
              Put it here — starts {{ clockLabel(item.startMin) }}
            </button>
            <div v-else class="week-slot-job">
              <span class="week-slot-job-time">{{ item.rangeLabel }}</span>
              <span>{{ item.label }}</span>
            </div>
          </li>
        </ol>
        <div style="display:flex; align-items:center; gap:0.75rem; margin-top:0.75rem;">
          <label for="week-slot-time">Start time</label>
          <InputText
            id="week-slot-time"
            v-model="slotPromptTime"
            type="time"
            step="900"
            style="max-width:140px;"
            data-testid="week-slot-time"
          />
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text data-testid="week-slot-cancel" @click="resolveSlotPrompt(null)" />
          <Button
            label="Place job"
            icon="pi pi-check"
            :disabled="!/^\d{2}:\d{2}$/.test(slotPromptTime || '')"
            data-testid="week-slot-confirm"
            @click="resolveSlotPrompt(slotPromptTime)"
          />
        </template>
      </Dialog>

      <!-- A crew job dropped on a tech outside its crew: swap one member or
           add the tech. Nothing changes the crew without this answer. -->
      <CrewChangeDialog :prompt="crewPrompt" @answer="answerCrewPrompt" />

      <!-- A job with no date dropped onto a tech: hours and the whole crew
           before anything is written (Doug, 2026-10-16). -->
      <ScheduleDropDialog :prompt="schedulePrompt" :technicians="assignableTechnicians" :all-technicians="technicians" @answer="answerSchedulePrompt" />
      <DayOverflowDialog :prompt="overflowPrompt" :technicians="assignableTechnicians" :all-technicians="technicians" @answer="answerOverflowPrompt" />

      <!-- Sprint dispatch-capacity (2026-05-20) — duration prompt on drop -->
      <Dialog
        v-model:visible="durationPromptOpen"
        header="How long will this job take?"
        :modal="true"
        :closable="true"
        :style="{ width: '420px' }"
        data-testid="dispatch-duration-prompt"
        @hide="onDurationPromptHide"
      >
        <p v-if="durationPromptEstimateNote" style="margin-top:0;" data-testid="dispatch-duration-prompt-from-estimate">
          <strong>{{ durationPromptJobLabel }}</strong> has no estimated time yet.
          The {{ durationPromptEstimateNote.accepted ? 'accepted' : 'not yet accepted' }}
          estimate has {{ durationPromptEstimateNote.manHours }} man-hours; the hours below
          are that over a crew of {{ durationPromptEstimateNote.crew }}.
          Change them if you know better.
        </p>
        <p v-else style="margin-top:0;">
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

      <!-- Phase 2 / C4 — closeout sheet, opened by the drawer's Close out. -->
      <MobileJobCloseoutDialog
        v-model:visible="closeoutOpen"
        :job-id="closeoutJob?.id || ''"
        :job-title="closeoutJob?.title || ''"
        :job-type="closeoutJob?.job_type || ''"
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
          <p class="job-drawer-line"><strong>Job:</strong> {{ drawerJob.title || drawerJob.job_number || '—' }}</p>
          <p class="job-drawer-line"><strong>Customer:</strong> {{ drawerJob.customer_name }}</p>
          <p class="job-drawer-line"><strong>Address:</strong> {{ drawerJob.address || 'N/A' }}</p>
          <p class="job-drawer-line"><strong>Job type:</strong> {{ drawerJob.job_type }}</p>
          <p class="job-drawer-line"><strong>Window:</strong> {{ drawerJob.time_window }}</p>
          <p v-if="drawerJob.technician_id" class="job-drawer-line">
            <strong>Technician:</strong> {{ techName(drawerJob.technician_id) }}
          </p>
          <p class="job-drawer-line"><strong>Scheduled:</strong> {{ drawerJob.scheduled_at ? formatScheduled(drawerJob.scheduled_at) : 'Not scheduled' }}</p>
          <p v-if="visitDayLabel(drawerJob)" class="job-drawer-line" data-testid="dispatch-job-drawer-day">
            <strong>Visit:</strong> {{ visitDayLabel(drawerJob) }}
          </p>
          <p v-if="drawerLaneName" class="job-drawer-line" data-testid="dispatch-job-drawer-lane">
            <strong>Holding area:</strong> {{ drawerLaneName }}
          </p>
          <Button
            v-if="inOrderDoorsLane(drawerJob) && !isTerminalJob(drawerJob)"
            label="Doors ordered"
            icon="pi pi-check-square"
            severity="secondary"
            class="w-full"
            size="small"
            :loading="markingDoorsJobId === String(drawerJob.id)"
            data-testid="dispatch-job-doors-ordered"
            @click="() => markDoorsOrdered(drawerJob)"
          />
          <Button
            v-if="parkingHoldingAreas.length && !isTerminalJob(drawerJob) && canPark(drawerJob)"
            label="Move to…"
            icon="pi pi-arrow-right-arrow-left"
            severity="secondary"
            outlined
            class="w-full"
            size="small"
            data-testid="dispatch-job-move"
            @click="(e) => openLaneMenu(e, drawerJob)"
          />
          <!-- The board's ONE way to complete a job (2026-08-28, #526). Prod:
               the owner submitted 8 of the last 19 closeouts — the office
               DOES close jobs out, and the backend already handles a non-tech
               closer (the synthetic time entry carries no payable user_id). -->
          <Button
            v-if="!isTerminalJob(drawerJob)"
            label="Close out"
            icon="pi pi-check"
            severity="success"
            class="w-full"
            size="small"
            data-testid="dispatch-job-closeout"
            @click="() => openCloseout(drawerJob)"
          />
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
      <!-- One menu for every card's Move to… (lanes, and Release). -->
      <Menu ref="laneMenu" :model="laneMenuItems" :popup="true" data-testid="lane-menu" />
      <!-- The last lane move or Release, for a few seconds: one click puts
           the job back where it was. -->
      <div v-if="lastMove" class="board-undo-bar" role="status" data-testid="board-undo">
        <span>{{ lastMove.text }}</span>
        <Button label="Undo" size="small" text data-testid="board-undo-button" @click="undoLastMove" />
        <Button icon="pi pi-times" size="small" text severity="secondary" aria-label="Dismiss"
          data-testid="board-undo-dismiss" @click="dismissUndo" />
      </div>
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
              <td>{{ formatTime(t.recorded_at, { options: { hour: 'numeric', minute: '2-digit', second: '2-digit' } }) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
</template>
<script setup>
import { computed, onMounted, onBeforeUnmount, ref, watch } from 'vue';
import { usePollingRefresh } from '../composables/usePollingRefresh';
import { useRouter } from 'vue-router';
import { useApiWithToast } from '../composables/useApiWithToast';
import { shopWallClockToIso, toShopWallClock, useTenantTimezone } from '../composables/useTenantTimezone';
import { formatTime, formatDateTime } from '../composables/useFormatters';
import { useToast } from 'primevue/usetoast';
import Avatar from 'primevue/avatar';
import Badge from 'primevue/badge';
import Button from 'primevue/button';
import DatePicker from 'primevue/datepicker';
import Card from 'primevue/card';
import Dialog from 'primevue/dialog';
import CrewChangeDialog from '../components/CrewChangeDialog.vue';
import { crewOf, crewPatch, useCrewChangePrompt } from '../composables/useCrewChange';
import ScheduleDropDialog from '../components/ScheduleDropDialog.vue';
import DayOverflowDialog from '../components/DayOverflowDialog.vue';
import {
  estimateHoursOver, hoursLeftInDay, scheduleDropPatch, shiftHours, useDayOverflowPrompt, useScheduleDropPrompt,
} from '../composables/useScheduleDrop';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
import InputText from 'primevue/inputtext';
import MobileJobCloseoutDialog from '../components/MobileJobCloseoutDialog.vue';
import Drawer from 'primevue/drawer';
import Menu from 'primevue/menu';
import Select from 'primevue/select';
import SelectButton from 'primevue/selectbutton';
import Tag from 'primevue/tag';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import JobStateChip from '../components/JobStateChip.vue';
import TechEfficiencyPanel from '../components/TechEfficiencyPanel.vue';
import { usePermission } from '../composables/usePermission';
import TechTimelineColumn from '../components/TechTimelineColumn.vue';
import { formatDurationHours } from '../utils/hours';
import { formatShopDay, refusalOf } from '../utils/visitRefusals';

const api = useApiWithToast();
const toast = useToast();
const router = useRouter();
// Office display timezone — the board buckets jobs into day columns and renders
// card times in THIS zone, not UTC or the viewer's browser zone.
const { tenantTimezone, zonedDateKey } = useTenantTimezone();
const { hasPermission, permissionsLoaded } = usePermission();

function goToJob(job) {
  if (job?.id) router.push(`/jobs/${job.id}`);
}

// Shifts the office needs to correct: still clocked in long past the cap, or
// closed with a duration nothing can vouch for. Empty on a healthy shop, and
// the card does not render at all — this must never become wallpaper.
const laborExceptions = ref([]);

async function loadLaborExceptions() {
  try {
    const rows = await api.get('/api/timeclock/exceptions');
    laborExceptions.value = Array.isArray(rows) ? rows : [];
  } catch (e) {
    // Techs get a 403 here by design; never break the board over it.
    console.warn('labor_exceptions_failed', e);
    laborExceptions.value = [];
  }
}

// Open jobs whose scheduled day has passed (GET /api/dispatch/late-open).
// Office-only (jobs.read_all): the board is also opened by roles without it,
// so don't ask rather than eat a 403 on every poll.
const lateOpenJobs = ref([]);
// Either alert card starts folded to its one-line header and count (Doug,
// 2026-10-16: open, the two tables pushed the tech columns off the first
// screen). Opening or folding one holds for this browser session
// (2026-10-15), so it is not undone by a date change or a reload.
function sessionFlag(key) {
  const flag = ref(true);
  try {
    const saved = window.sessionStorage.getItem(key);
    if (saved !== null) flag.value = saved === '1';
  } catch { /* storage blocked */ }
  watch(flag, (v) => {
    try { window.sessionStorage.setItem(key, v ? '1' : '0'); } catch { /* storage blocked */ }
  });
  return flag;
}
const lateOpenCollapsed = sessionFlag('gdx.dispatch.lateOpenFolded');
const laborCollapsed = sessionFlag('gdx.dispatch.laborFolded');

async function loadLateOpen({ keepOnError = false } = {}) {
  if (!hasPermission('jobs.read_all')) {
    lateOpenJobs.value = [];
    return;
  }
  try {
    const res = await api.get('/api/dispatch/late-open');
    lateOpenJobs.value = Array.isArray(res?.items) ? res.items : [];
  } catch (e) {
    console.warn('late_open_jobs_failed', e);
    if (!keepOnError) lateOpenJobs.value = [];
  }
}

// Permissions resolve asynchronously; the first board load can run before
// they arrive and would skip this card until the next 45 s poll.
watch(permissionsLoaded, (loaded) => { if (loaded) loadLateOpen({ keepOnError: true }); });

// Partial Jobs (multi-day jobs plan §5.3a, D10-D12): a day was worked and
// nothing further is booked. Same gate and same poll as late-open — the
// server keeps the two lists disjoint (one predicate, partial_clause).
const partialJobs = ref([]);
const partialJobIds = computed(() => new Set(partialJobs.value.map((j) => String(j.id))));
// Normalized like the day list, for the holding-area lanes and the drawer.
const normalizedPartialJobs = computed(() => partialJobs.value.map(normalizeJob));
// Reads in flight of the lists the drawer opens from (see repointDrawer).
let drawerListReads = 0;
let drawerCloseDeferred = false;

// Returns whether the list was read: a kept list is not evidence of anything.
async function loadPartialJobs({ keepOnError = false } = {}) {
  if (!hasPermission('jobs.read_all')) {
    partialJobs.value = [];
    return false;
  }
  let ok = false;
  drawerListReads += 1;
  try {
    const res = await api.get('/api/dispatch/partial-jobs');
    partialJobs.value = Array.isArray(res?.items) ? res.items : [];
    ok = true;
    return true;
  } catch (e) {
    console.warn('partial_jobs_failed', e);
    if (!keepOnError) partialJobs.value = [];
    return false;
  } finally {
    settleDrawerRead(ok);
  }
}

watch(permissionsLoaded, (loaded) => { if (loaded) loadPartialJobs({ keepOnError: true }); });

function partialWorkedLabel(job) {
  const who = (job.worked_by || []).map((w) => w.name).filter(Boolean).join(', ');
  const day = job.last_worked_day ? formatShopDay(job.last_worked_day) : 'an earlier day';
  return who ? `Last worked ${day} by ${who}` : `Last worked ${day}`;
}

// Drop of a partial card onto a tech: the board's ordinary assignment, with
// the date always sent. _doAssignJobInner sends none when the job already
// has a date (a reassignment must not move time) — but a partial job's date
// is the day it was worked, so without one nothing would be booked. With no
// Current visit the server books this tech on this day (E4) and the crew
// becomes [this tech]. If that tech already holds a visit of the job that
// day, the server books nothing (E5) and answers 200 — so the list is
// re-read and a card still there is said out loud, never shown as booked.
async function placePartialJob(jobId, techId, scheduledAt, dayKey = zonedDateKey(scheduledAt)) {
  const job = partialJobs.value.find((j) => String(j.id) === String(jobId));
  const techLabel = (technicians.value.find((t) => String(t.id) === String(techId)) || {}).name || 'that tech';
  const label = job?.customer_name || job?.title || 'Job';
  // The next day of work is today or later: a drop on a past day's board
  // would book a visit that is late the moment it exists.
  if (dayKey && dayKey < zonedDateKey(new Date())) {
    toast.add({
      severity: 'warn',
      summary: 'Nothing was booked',
      detail: `${formatBoardDay(scheduledAt)} has passed. Pick today or a later day.`,
      life: 6000,
    });
    return;
  }
  // E5: this tech closed a visit of the job that day, so the server would
  // book nothing — yet still write the date and the crew (a time fix on a
  // closed day is allowed on purpose). Say so instead of sending it.
  if (dayKey && (job?.closed_days?.[dayKey] || []).some((t) => String(t) === String(techId))) {
    toast.add({
      severity: 'warn',
      summary: 'Nothing was booked',
      detail: `${techLabel} already worked ${label} on ${formatBoardDay(scheduledAt)}. Pick another day or tech.`,
      life: 6000,
    });
    return;
  }
  // The server reads a date equal to the stored one (to the minute) as no
  // date change: it would book nothing yet still swap the crew. A partial
  // job's date is its last visit, so this is a tray drop on the day it was
  // worked — ask for a time instead of sending it.
  const minute = (v) => Math.floor(new Date(v).getTime() / 60000);
  if (job?.scheduled_at && minute(job.scheduled_at) === minute(scheduledAt)) {
    toast.add({
      severity: 'warn',
      summary: 'Nothing was booked',
      detail: `${label} was already worked at that time on ${formatBoardDay(scheduledAt)}. Drop it on a time in ${techLabel}'s timeline.`,
      life: 6000,
    });
    return;
  }
  await withBoardWrite(async () => {
    try {
      await api.patch(`/api/jobs/${jobId}`, {
        assigned_tech_id: techId, assigned_to: techId, scheduled_at: scheduledAt,
        ...leaveLanePatch(job, { partial: true }),
      });
    } catch {
      // useApi has already shown the server's refusal; the card stays.
      await loadPartialJobs({ keepOnError: true });
      return;
    }
    const [, , reread] = await Promise.all([fetchJobs(), fetchScheduledUnassigned(), loadPartialJobs({ keepOnError: true })]);
    if (!reread) {
      toast.add({
        severity: 'info',
        summary: 'Drop sent',
        detail: `Partial Jobs could not be re-read, so whether ${label} was booked for ${techLabel} is not known yet. Check the job's Visits.`,
        life: 6000,
      });
    } else if (partialJobIds.value.has(String(jobId))) {
      toast.add({
        severity: 'warn',
        summary: 'Nothing was booked',
        detail: `No visit of ${label} was booked for ${techLabel}, but the job's date now reads ${formatBoardDay(scheduledAt)}. Pick another day or tech.`,
        life: 6000,
      });
    } else {
      toast.add({
        severity: 'success',
        summary: 'Next day booked',
        detail: `${label} → ${techLabel} on ${formatBoardDay(scheduledAt)}.`,
        life: 4000,
      });
    }
  });
}

function lateLabel(days) {
  if (days == null) return 'date passed';
  return days === 1 ? '1 day ago' : `${days} days ago`;
}

function fixLaborException(row) {
  // 2026-08-10: this used to push /timeclock?entry=<id>, which was a dead end
  // in two ways — TimeclockView never read `query.entry`, and it is the
  // SELF-SERVICE page (its entries fetch defaults to the caller's own tech id),
  // so the office landed on their own read-only timecard instead of the tech's
  // broken shift. /timesheets is the office view: it opens this exact entry's
  // correction dialog. `on` anchors the date range on the shift's own week —
  // an open shift can be weeks old, and this card is the only thing that knows
  // which one is wrong. Correcting the times still IS the dismissal: the row
  // drops out of /exceptions on the next load.
  router.push({
    path: '/timesheets',
    query: {
      entry: row.entry_id,
      tech: row.technician_id,
      on: String(row.started_at || '').slice(0, 10),
    },
  });
}

// Phase 2 / C4 (Doug 2026-05-10): completing a job from dispatch is a
// closeout transaction — parts + hours + signature + notes in one POST to
// /api/jobs/{id}/closeout, which flips lifecycle to 'completed' and writes
// the JobCloseout snapshot. Nothing is PATCHed or POSTed until the sheet
// submits; the watcher on closeoutOpen refetches on close either way.
//
// 2026-08-28 (#526): the status select that used to reach this was replaced
// by the read-only JobStateChip and the handler kept living with nothing
// pointing at it — the board had NO way to complete a job since the initial
// public release. The drawer's "Close out" button is that way.
function isTerminalJob(job) {
  const stage = String(job?.lifecycle_stage || '').toLowerCase();
  return stage === 'completed' || stage === 'cancelled' || isCompletedStatus(job?.status);
}
// The jobs list sends the stage as a label ("In Progress"), not the enum.
function jobStage(job) {
  return String(job?.lifecycle_stage || '').trim().toLowerCase().replace(/[\s-]+/g, '_');
}
function openCloseout(job) {
  if (!job || isTerminalJob(job)) return;
  closeoutJob.value = job;
  closeoutOpen.value = true;
}

const closeoutOpen = ref(false);
const closeoutJob = ref(null);
function onCloseoutDone() {
  // Closeout submitted successfully. The endpoint flipped lifecycle to
  // 'completed' and the job now lives in /api/jobs/ready-for-billing.
  // The success toast is already fired by the dialog itself; the watcher
  // on closeoutOpen handles the refetch. The drawer was describing a job
  // that just left the board — close it too.
  closeoutOpen.value = false;
  closeJobDrawer();
}
// When the dialog closes for any reason (submit OR cancel), refetch.
// On success that's a no-op visually (the success path already calls
// fetchJobs in onCloseoutDone). On cancel it reverts the optimistic
// dropdown change by re-loading the job's real status.
watch(closeoutOpen, async (v) => {
  if (!v) {
    closeoutJob.value = null;
    await Promise.all([fetchJobs(), loadLateOpen({ keepOnError: true }), loadPartialJobs({ keepOnError: true })]);
  }
});

const selectedDate = ref(new Date());
// Day or Week survives a reload, as the compact toggle does (COMPACT_KEY).
const VIEW_MODE_KEY = 'gdx.dispatch.viewMode';
function readViewMode() {
  try {
    const v = window.localStorage.getItem(VIEW_MODE_KEY);
    return v === 'week' ? 'week' : 'day';
  } catch { return 'day'; }
}
const viewMode = ref(readViewMode());
watch(viewMode, (v) => {
  try { window.localStorage.setItem(VIEW_MODE_KEY, v); } catch { /* private mode */ }
});
const viewOptions = [
  { label: 'Day', value: 'day' },
  { label: 'Week', value: 'week' },
];
const refreshing = ref(false);
// Count of in-flight board-mutating PATCHes (optimistic assigns/moves). The
// drag flag is cleared the instant the card is dropped, but the optimistic
// write + PATCH happen *after* that — and several paths don't refetch. Auto-
// refresh polling must stay paused across that whole window or a poll lands
// stale server rows on top of the user's move and visibly reverts it. Every
// optimistic mutation routes through withBoardWrite so the guard is uniform
// (drag-assign, holding-area move/release, timeline reschedule, duration+assign).
// /audit (feat/daily-ux-improvements) caught the drag-assign case AND that the
// first fix missed the other four mutation paths.
const pendingWrites = ref(0);
async function withBoardWrite(fn) {
  pendingWrites.value += 1;
  try {
    return await fn();
  } finally {
    pendingWrites.value -= 1;
  }
}
const draggingJobId = ref(null);
const dragOverTechId = ref(null);
const jobs = ref([]);
const technicians = ref([]);
const skillFilter = ref(null);
const dateRangeFilter = ref(null);

// Still used by the intake queue (unassignedJobs) — finished work does not
// belong in a list of jobs that need scheduling. It no longer gates what the
// day/week/tech columns render.
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
const optimizerLoading = ref(false);

// Sprint dispatch-capacity (2026-05-20) — when a card without an
// estimated duration lands in a tech column, prompt the scheduler for
// hours so the column total stops being a guess. Carrying both the
// job_id and the destination tech_id so Save can commit the full patch.
const durationPromptOpen = ref(false);
const durationPromptJobId = ref(null);
const durationPromptTechId = ref(null);
const durationPromptHours = ref('');
const durationPromptJobLabel = ref('');
// Set when the hours were prefilled from the job's estimate:
// { manHours, crew, accepted, hours }. null = blank prompt.
const durationPromptEstimate = ref(null);
// The note explains the prefilled figure, so it shows only while the box
// still holds that figure; once the scheduler changes it, the blank
// prompt's wording is the true one.
const durationPromptEstimateNote = computed(() => {
  const e = durationPromptEstimate.value;
  return e && Number(durationPromptHours.value) === e.hours ? e : null;
});
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
  fetchJobs();
});

// The server scopes the fetch to the requested day/range (±1-day widened),
// so switching to Week view or applying a custom range needs a re-fetch —
// the day-scoped payload only covers selectedDate±1.
watch([viewMode, dateRangeFilter], () => {
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
    // assignableTechnicians and the column/row filters read this; dropping it
    // made every inactive tech pickable (GDXA-1255).
    active: tech.active !== false,
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
    // site_full_address: /api/jobs' effective jobsite, street and town (GDXA-502).
    address: rawJob.address || rawJob.site_full_address || rawJob.service_address || [rawJob.city, rawJob.state].filter(Boolean).join(', '),
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

// Visit cards (multi-day jobs plan §5.3, PR 2b). A job its job row cannot
// draw — more than one day, or a day whose visit was moved off the row's
// time or tech — comes from GET /api/dispatch/visits as one item per visit
// in the window, and is drawn from those instead of its job row. Each card
// is the job with the visit's tech, start and length laid over it, so the
// columns, tray, capacity totals and drawer read it like any other card.
const boardVisits = ref([]);
const visitJobIds = computed(() => new Set(boardVisits.value.map((v) => String(v.id))));

// Approved time off in the window, from GET /api/dispatch/time-off
// (GDXA-1829). The approved TimeOffRequest is the one record of it: a tech
// who is off that day has no capacity, shows a "Time off" block in the day
// column and the week cell, and takes no drop there. Pending, denied and
// revoked requests never come back from the route.
const boardTimeOff = ref([]);
let timeOffUnread = false;

function timeOffOn(techId, dateStr) {
  if (!techId || !dateStr) return null;
  return boardTimeOff.value.find((o) => String(o.technician_id) === String(techId)
    && o.start_date <= dateStr && dateStr <= o.end_date) || null;
}

function timeOffRange(off) {
  const fmt = (iso) => {
    const [y, m, d] = iso.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: 'short', month: 'numeric', day: 'numeric' });
  };
  return off.start_date === off.end_date ? fmt(off.start_date) : `${fmt(off.start_date)} – ${fmt(off.end_date)}`;
}

function refuseTimeOffDrop(techId, dayStr) {
  const off = timeOffOn(techId, dayStr);
  if (!off) return false;
  toast.add({
    severity: 'warn',
    summary: 'Nothing was moved',
    detail: `${techName(techId)} has approved time off (${off.label}) that day. Pick another tech or day, or revoke the time off first.`,
    life: 6000,
  });
  return true;
}

function hoursBetween(startIso, endIso) {
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  return Number.isFinite(ms) && ms > 0 ? Math.round((ms / 3_600_000) * 100) / 100 : null;
}

function toVisitCard(item) {
  const job = normalizeJob(item);
  const tech = item.visit_tech_id ? String(item.visit_tech_id) : null;
  const hours = item.visit_end ? hoursBetween(item.visit_start, item.visit_end) : null;
  return {
    ...job,
    scheduled_at: item.visit_start,
    technician_id: tech,
    assigned_to: tech,
    lead_tech_id: tech,
    assigned_tech_ids: tech ? [tech] : [],
    scheduled_duration_hours: hours,
    effective_duration_hours: hours,
    // The whole job's hours, for a lane that lists the job once.
    job_duration_hours: job.effective_duration_hours,
    visit_id: String(item.visit_id),
    visit_state: item.visit_state,
    // Only an open visit of a live job moves; the server refuses the rest.
    visit_movable: item.visit_state === 'open' && !isTerminalJob(item),
    visit_day: item.visit_day,
    day_index: item.day_index,
    day_count: item.day_count,
    job_has_crew: Boolean(item.job_has_crew),
    card_key: `${job.id}:${item.visit_id}`,
  };
}

// The board's cards: job rows for jobs their row draws, visit cards for the rest.
const boardJobs = computed(() => [
  ...jobs.value.filter((j) => !visitJobIds.value.has(String(j.id))),
  ...boardVisits.value.map(toVisitCard),
]);

const VISIT_STATE_LABEL = { on_site: 'on site', closed: 'done for the day' };
// A job of one day gets no day label: "Day 1 of 1" says nothing.
function visitDayLabel(job) {
  if (!job?.visit_id || !(job.day_index && job.day_count >= 2)) return '';
  const day = `Day ${job.day_index} of ${job.day_count}`;
  const note = VISIT_STATE_LABEL[job.visit_state];
  return note ? `${day} · ${note}` : day;
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
  if (timeOffOn(tech.id, toDateStr(date instanceof Date ? date : new Date(date)))) return 0;
  if (!isShiftDay(tech, date)) return 0;
  const start = parseHHMM(tech.effective_shift_start);
  const end = parseHHMM(tech.effective_shift_end);
  if (start == null || end == null || end <= start) return 0;
  return end - start;
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
  // An undated job is intake and waits on whatever day is open — unless it
  // is finished. A finished job never scheduled sits on the day it was
  // completed, or on no day: matching every day drew each one in its tech's
  // column and capacity on every date (2026-10-15, 16 such jobs on prod).
  if (!job.scheduled_at && isTerminalJob(job)) {
    return Boolean(job.completed_at) && zonedDateKey(job.completed_at) === dateStr;
  }
  if (!job.scheduled_at) return dateStr === selectedDateStr.value;
  // Bucket by the OFFICE-local calendar day (tenant timezone), not the raw UTC
  // date. Slicing the UTC ISO string put an evening job into the NEXT day's
  // column: for a negative offset like US-Central, "Jul 10 8:00 PM" serializes
  // to "2026-07-11T01:00:00Z", so .split('T')[0] matched the Jul 11 column.
  // zonedDateKey resolves the timestamp in the tenant zone so the column and
  // the card time (also rendered in the tenant zone) always agree.
  return zonedDateKey(job.scheduled_at) === dateStr;
}

const rangeFilteredJobs = computed(() => {
  const [start, end] = dateRangeFilter.value || [];
  const startStamp = start ? new Date(start).setHours(0, 0, 0, 0) : null;
  const endStamp = end ? new Date(end).setHours(23, 59, 59, 999) : null;
  // 2026-08-28 (Doug): finished work STAYS ON THE BOARD. A completed job used
  // to vanish from its tech's column the moment the closeout submitted, and
  // the only way back was a "Show Completed Jobs" button that lived inside
  // `v-if="skillOptions.length"` — a guard meant for the skill Select next to
  // it. Every technician on this tenant has skills = NULL, so that row never
  // rendered and the button was unreachable from the day it shipped: the board
  // filtered completed jobs out with no way to turn it off. The toggle is gone
  // and the filter with it — a day's column shows the day's work, done or not.
  //
  // Deliberately NOT applied to unassignedJobs ("New Jobs to Schedule") below,
  // which stays an action queue: 189 of this tenant's completed jobs have no
  // date, and matchesDate() treats an undated job as matching WHATEVER day is
  // selected, so unfiltering there would redraw all of them above the tech
  // columns on every date. The Jobs page's "Completed" tab is that history.
  return boardJobs.value.filter((job) => {
    if (!startStamp && !endStamp) return true;
    const timestamp = job.scheduled_at ? new Date(job.scheduled_at).getTime() : null;
    if (!timestamp) return true;
    if (startStamp && timestamp < startStamp) return false;
    if (endStamp && timestamp > endStamp) return false;
    return true;
  });
});
const dayJobs = computed(() => rangeFilteredJobs.value.filter((j) => matchesDate(j, selectedDateStr.value)));

// "New Jobs to Schedule" section: jobs still missing a date or a tech (or
// both) and not parked in a genuine holding area — work a dispatcher still
// owes a decision. Scheduled-but-no-tech
// jobs live in the red "Scheduled — Not Assigned" lane regardless of date,
// so a salesperson penciling in an asap job can't slip past the dispatcher.
// "Ready to Schedule" is not a parking lot — it is the INTAKE QUEUE. Every
// job created without a date is auto-routed into it server-side
// (routers/jobs.create_job: derived_holding_area → _holding_area_id_by_name
// "Ready to Schedule"). Because this filter excluded anything with a
// holding_area_id, that meant EVERY newly created job was excluded from the
// section called "New Jobs to Schedule" — which then rendered "All jobs
// assigned for this date" while hundreds of unscheduled jobs sat in a
// side column. That is the "I made a job and it never showed up on dispatch"
// report (2026-08-10). Jobs in this one area belong in the queue; the other
// areas (Needs Parts, Waiting on doors, …) are genuine parking and stay out.
const READY_TO_SCHEDULE = 'ready to schedule';
const readyToScheduleAreaId = computed(() => {
  const area = (holdingAreas.value || []).find(
    (a) => String(a.name || '').trim().toLowerCase() === READY_TO_SCHEDULE,
  );
  return area ? String(area.id) : null;
});

// 2026-08-10: the "regardless of date" promise above only held when the red
// lane was actually rendered, and dispatch_show_unassigned_lane defaults to
// FALSE. With it off, a job that had a date but no tech appeared in NO
// section: excluded here for having a date, excluded from the tech columns
// (those key off assigned_tech_ids), and its lane not drawn. So when the lane
// is off we fold those jobs in here instead — nothing that needs a dispatcher
// is allowed to be invisible on the dispatch board.
// Areas the board can actually draw a column for. A job stamped with an area
// that isn't in this set (renamed, soft-deleted — note jobs._holding_area_id_by_name
// has no deleted_at filter, so create_job can still route into a dead area)
// has no column to appear in. Those jobs belong in the queue rather than
// nowhere; "invisible" is the failure mode this whole change exists to kill.
const knownHoldingAreaIds = computed(
  () => new Set((holdingAreas.value || []).map((a) => String(a.id))),
);

// Parked = in a holding area the board draws a lane for, other than the
// intake queue ("Ready to Schedule" is the queue, not a parking lot).
function isParked(job) {
  if (!job?.holding_area_id) return false;
  const areaId = String(job.holding_area_id);
  return areaId !== readyToScheduleAreaId.value && knownHoldingAreaIds.value.has(areaId);
}

// A partial job parked in a holding area (Needs Parts, …) waits there, not
// in this queue — the same rule "New Jobs" applies below.
const visiblePartialJobs = computed(() =>
  partialJobs.value.filter((j) => {
    if (!j.holding_area_id) return true;
    const areaId = String(j.holding_area_id);
    return areaId === readyToScheduleAreaId.value || !knownHoldingAreaIds.value.has(areaId);
  }),
);

const unassignedJobs = computed(() =>
  boardJobs.value.filter((j) => {
    // A cancelled job needs no tech and no date (GDXA-375): it left the
    // queue when it was cancelled, by stage or by its display status.
    if (isTerminalJob(j) || String(j.status || '').toLowerCase() === 'cancelled') return false;
    // One job, one queue: a partial job is in Partial Jobs above.
    if (partialJobIds.value.has(String(j.id))) return false;
    // 2026-08-17 ("Assign to me"): techs can self-assign at create, so a
    // tech on the job no longer implies dispatch is done with it. A job
    // with a tech but NO date still needs scheduling and stays in this
    // queue — the card names who has it — otherwise every self-assigned
    // job would skip the intake queue and sit dateless in the tech's
    // column on every day, which is the 2026-08-10 invisibility bug
    // wearing a new hat. Only a job with BOTH a tech and a date has
    // actually left the dispatcher's inbox.
    if (j.technician_id && j.scheduled_at) return false;
    if (j.holding_area_id) {
      const areaId = String(j.holding_area_id);
      const parked = areaId !== readyToScheduleAreaId.value && knownHoldingAreaIds.value.has(areaId);
      if (parked) return false;
    }
    if (!j.scheduled_at) return true;
    // A dated job only belongs in THIS day's queue — unlike undated intake,
    // which is dateless by definition, dated rows come back for the whole
    // fetched range (±1 day, or the week/custom range) and would otherwise
    // pile next month's work into today's column.
    if (!matchesDate(j, selectedDateStr.value)) return false;
    // An unassigned day of a job that has a crew is in no red-lane row (that
    // lane lists jobs with no crew at all), so it waits here either way.
    if (j.visit_id && j.job_has_crew) return true;
    return !dispatchSettings.value.dispatch_show_unassigned_lane;
  })
);

// The intake queue renders at the top of the board, so its column in the
// holding-areas row would show the same cards twice. Park the other areas
// only.
const parkingHoldingAreas = computed(() =>
  (holdingAreas.value || []).filter((a) => String(a.id) !== readyToScheduleAreaId.value),
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
      timeOff: timeOffOn(tech.id, selectedDateStr.value),
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
        dateObj: d,
        // Dated work only: an undated job is in the "To schedule" strip, not
        // on whichever day of the week happens to be selected.
        jobs: rangeFilteredJobs.value
          .filter((j) => (j.scheduled_at || isTerminalJob(j)) && matchesDate(j, ds))
          .sort((a, b) => String(a.scheduled_at || '').localeCompare(String(b.scheduled_at || ''))),
      });
  }
  return days;
});

// One row per tech: the active techs, plus any tech (inactive included) who
// holds a job this week, so no card goes missing — the same rule the day
// view's columns follow. A last row carries dated jobs with no tech.
const weekRows = computed(() => {
  const referenced = new Set();
  for (const day of weekDays.value) {
    for (const j of day.jobs) for (const tid of (j.assigned_tech_ids || [])) if (tid) referenced.add(String(tid));
  }
  let techs = technicians.value.filter((t) => t.active !== false || referenced.has(String(t.id)));
  if (skillFilter.value) techs = techs.filter((t) => (t.skills || []).includes(skillFilter.value));
  const rows = techs.map((tech) => {
    let weekHours = 0;
    const cells = weekDays.value.map((day) => {
      const cellJobs = day.jobs.filter((j) => (j.assigned_tech_ids || []).map(String).includes(String(tech.id)));
      const capacity = techCapacityHours(tech, day.dateObj);
      const totals = sumDurationHours(cellJobs);
      weekHours += totals.known;
      return {
        date: day.date,
        isToday: day.isToday,
        jobs: cellJobs,
        isOff: capacity === 0,
        timeOff: timeOffOn(tech.id, day.date),
        isOver: capacity > 0 && totals.known > capacity,
        hoursLabel: cellJobs.length
          ? `${formatDurationHours(totals.known)}${capacity > 0 ? ` / ${formatDurationHours(capacity)}` : ''}${totals.unknown ? ` +${totals.unknown}?` : ''}`
          : '',
      };
    });
    return { id: String(tech.id), name: tech.name, unassigned: false, cells, weekHours: Math.round(weekHours * 100) / 100 };
  });
  rows.push({
    id: 'unassigned',
    name: 'No tech',
    unassigned: true,
    weekHours: 0,
    cells: weekDays.value.map((day) => ({
      date: day.date,
      isToday: day.isToday,
      // A job parked in a holding area is in that area's lane above, as on
      // the day board — not here as well.
      jobs: day.jobs.filter((j) => !(j.assigned_tech_ids || []).some(Boolean) && !isParked(j)),
      isOff: false,
      isOver: false,
      hoursLabel: '',
    })),
  });
  return rows;
});

// The week's drag sources: intake with no date yet, and partial jobs whose
// next day is not booked. Dated-but-untechhed work is on the "No tech" row.
const weekQueueJobs = computed(() => [
  ...visiblePartialJobs.value,
  ...unassignedJobs.value.filter((j) => !j.scheduled_at && !partialJobIds.value.has(String(j.id))),
]);

function weekChipTime(job) {
  const wall = job.scheduled_at ? toShopWallClock(job.scheduled_at, tenantTimezone.value) : null;
  if (!wall || (wall.getHours() === 0 && wall.getMinutes() === 0)) return '';
  const h = wall.getHours();
  const m = wall.getMinutes();
  const h12 = h % 12 || 12;
  return `${h12}${m ? `:${String(m).padStart(2, '0')}` : ''}${h < 12 ? 'a' : 'p'}`;
}

function weekChipTooltip(job) {
  return [
    displayCustomer(job),
    displayTitle(job),
    visitDayLabel(job),
    job.technician_id ? techName(job.technician_id) : '',
    `${job.time_window || 'Anytime'} · ${formatDurationHours(job.effective_duration_hours)}`,
    job.address || '',
  ].filter(Boolean).join('\n');
}

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

// Same guard as TechTimelineColumn/MobileDispatchView: render the title only
// when it isn't already the string standing in for the customer. This view's
// displayCustomer never falls back to the title today, but the three views
// share cards conceptually — keep the dedupe policy uniform so harmonizing
// displayCustomer later can't introduce double-printed titles here.
function displayTitle(job) {
  const t = (job?.title || '').trim();
  return t && t !== displayCustomer(job) ? t : '';
}

function techName(techId) {
  if (!techId) return 'Unassigned';
  const t = technicians.value.find((tech) => String(tech.id) === String(techId));
  return t ? t.name : 'Unknown';
}

function formatLatLng(lat, lng) {
  if (lat === null || lat === undefined || lng === null || lng === undefined) return '—';
  return `${Number(lat).toFixed(5)}, ${Number(lng).toFixed(5)}`;
}

function formatTimestamp(value) {
  return formatTime(value, { options: { hour: '2-digit', minute: '2-digit' } });
}

// --- UI helpers & controls ---

function shiftSelectedDate(days) {
  const base = new Date(selectedDate.value);
  base.setDate(base.getDate() + days);
  selectedDate.value = base;
}

// In week view the arrows step a whole week — stepping a day re-drew the
// same week shifted by one column, and nothing could be dropped meanwhile.
function goPrevDay() {
  shiftSelectedDate(viewMode.value === 'week' ? -7 : -1);
}

function goNextDay() {
  shiftSelectedDate(viewMode.value === 'week' ? 7 : 1);
}

function openDay(dateStr) {
  const [y, m, d] = dateStr.split('-').map(Number);
  selectedDate.value = new Date(y, m - 1, d);
  viewMode.value = 'day';
}

const COMPACT_KEY = 'gdx.dispatch.compact';
function readCompact() {
  try { return window.localStorage.getItem(COMPACT_KEY) === '1'; } catch { return false; }
}
const compactBoard = ref(readCompact());
watch(compactBoard, (v) => {
  try { window.localStorage.setItem(COMPACT_KEY, v ? '1' : '0'); } catch { /* private mode */ }
});

function openJobDrawer(job) {
  drawerJob.value = job;
  jobDrawerVisible.value = true;
}

function closeJobDrawer() {
  jobDrawerVisible.value = false;
  drawerJob.value = null;
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

// The visit card being dragged: a drop moves that visit, never its job row.
const draggingCard = ref(null);
// The tech row or column the card was picked up from, when it came from one:
// a crew job dropped on a tech outside the crew offers to swap this tech first.
let draggingFromTechId = null;
// The week strip or lane the card was picked up from ('week-queue',
// 'area:<id>'): let go over that same one, the drag was cancelled.
let draggingFromLane = null;

function onDragStart(job, event, fromTechId = null, fromLane = null) {
  // A day that has started or closed stays where it was worked.
  if (job?.visit_id && !job.visit_movable) {
    event?.preventDefault?.();
    return;
  }
  draggingCard.value = job?.visit_id ? job : null;
  draggingJobId.value = job.id;
  draggingFromTechId = fromTechId ? String(fromTechId) : null;
  draggingFromLane = fromLane;
  if (event?.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', String(job.id));
  }
}

function onDragOver(techId) {
  dragOverTechId.value = techId;
}

// The dragged visit card, if this drop carries one (the drop names the job;
// the card is remembered from the drag start). Cleared once read.
function takeDraggedVisit(jobId) {
  const card = draggingCard.value;
  draggingCard.value = null;
  return card && String(card.id) === String(jobId) ? card : null;
}

// A day of a multi-day job is moved on the board or on the job's Visits
// card; parking and un-scheduling act on the whole job, so they are not a
// drop of one day.
function refuseWholeJobDrop(card, where) {
  toast.add({
    severity: 'warn',
    summary: 'Nothing was moved',
    detail: `That card is one day of ${card.customer_name || card.title || 'a job'}. ${where} acts on the whole job: use the Visits card on the job page.`,
    life: 6000,
  });
}

// Move one visit (PATCH /api/jobs/{id}/visits/{visit_id}). The drop instant
// is turned into the shop day and wall-clock time the route takes; the
// visit keeps its length. A refusal is the server's own sentence, and the
// board is re-read either way so the card sits where the server put it.
function shopDayAndTime(iso) {
  const wall = toShopWallClock(iso, tenantTimezone.value);
  if (!wall) return null;
  const pad = (n) => String(n).padStart(2, '0');
  return {
    day: `${wall.getFullYear()}-${pad(wall.getMonth() + 1)}-${pad(wall.getDate())}`,
    time: `${pad(wall.getHours())}:${pad(wall.getMinutes())}`,
  };
}

async function moveVisitCard(card, techId, startISO) {
  const at = shopDayAndTime(startISO);
  if (at) await sendVisitMove(card, techId, at.day, at.time);
}

async function sendVisitMove(card, techId, day, time) {
  const body = { day, start_time: time };
  const sameTech = String(techId ?? '') === String(card.technician_id ?? '');
  if (!sameTech) body.tech_id = techId;
  const now = shopDayAndTime(card.scheduled_at);
  if (sameTech && now && now.day === day && now.time === time) return;
  await withBoardWrite(async () => {
    try {
      await api.patch(`/api/jobs/${card.id}/visits/${card.visit_id}`, body, { suppressErrorToast: true });
    } catch (e) {
      toast.add({
        severity: 'warn',
        summary: 'Visit not moved',
        detail: refusalOf(e)?.detail || e?.message || 'The visit could not be moved.',
        life: 7000,
      });
    }
    await fetchJobs();
  });
}

// The New Jobs dropdown on an unassigned day: that day gets the tech, at its time.
async function assignVisitTech(card, techId) {
  if (!techId) return;
  if (refuseTimeOffDrop(techId, zonedDateKey(card.scheduled_at))) return;
  await moveVisitCard(card, techId, card.scheduled_at);
}

// Crossing a chip inside the cell fires dragleave on the cell; only a pointer
// that has left the cell clears the drop highlight.
function onWeekCellLeave(event) {
  if (event?.currentTarget?.contains?.(event.relatedTarget)) return;
  dragOverTechId.value = null;
}

// --- Week board: where a job with no time goes (Doug, 2026-10-15) ---
// An empty day starts it at the tech's shift start. A day with work asks
// where it goes in that day's order, and suggests a start from the
// neighbours; the time stays editable, and Cancel moves nothing.
const slotPromptOpen = ref(false);
const slotPromptHeader = ref('');
const slotPromptJobLabel = ref('');
const slotPromptItems = ref([]);
const slotPromptChoice = ref(null);
const slotPromptTime = ref('');
let slotPromptResolve = null;

const ASSUMED_HOURS = 1; // only to suggest a start; nothing is written from it

function clockLabel(min) {
  const h = Math.floor(min / 60) % 24;
  const m = min % 60;
  return `${h % 12 || 12}${m ? `:${String(m).padStart(2, '0')}` : ''}${h < 12 ? 'a' : 'p'}`;
}
function toHHMM(min) {
  const c = Math.min(Math.max(min, 0), 23 * 60 + 45);
  return `${String(Math.floor(c / 60)).padStart(2, '0')}:${String(c % 60).padStart(2, '0')}`;
}
const roundUp15 = (min) => Math.ceil(min / 15) * 15;
const roundDown15 = (min) => Math.floor(min / 15) * 15;

function jobHours(job) {
  const v = Number(job?.effective_duration_hours) || Number(job?.scheduled_duration_hours);
  return Number.isFinite(v) && v > 0 ? v : null;
}

// The tech's shift start in minutes. The "No tech" row has no tech, so it
// uses the earliest shift start on the board; 8:00 only when no tech has one.
function dayStartMin(techId) {
  const own = techId ? technicians.value.find((t) => String(t.id) === String(techId)) : null;
  const parsed = parseHHMM(own?.effective_shift_start);
  if (parsed != null) return Math.round(parsed * 60);
  const starts = technicians.value.map((t) => parseHHMM(t.effective_shift_start)).filter((v) => v != null);
  return starts.length ? Math.round(Math.min(...starts) * 60) : 8 * 60;
}

function timedJobsInCell(techId, dayStr, excludeId) {
  const row = weekRows.value.find((r) => (techId ? r.id === String(techId) : r.unassigned));
  const cell = row?.cells.find((c) => c.date === dayStr);
  return (cell?.jobs || [])
    .filter((j) => String(j.id) !== String(excludeId))
    .map((j) => {
      const wall = j.scheduled_at ? toShopWallClock(j.scheduled_at, tenantTimezone.value) : null;
      if (!wall || (wall.getHours() === 0 && wall.getMinutes() === 0)) return null;
      const startMin = wall.getHours() * 60 + wall.getMinutes();
      const hours = jobHours(j);
      return { job: j, startMin, endMin: startMin + Math.round((hours ?? ASSUMED_HOURS) * 60), knownEnd: hours != null };
    })
    .filter(Boolean)
    .sort((a, b) => a.startMin - b.startMin);
}

// Resolves "HH:MM" (shop wall clock), or null when the person cancels.
function pickWeekStart(source, techId, dayStr) {
  const shiftStart = dayStartMin(techId);
  const others = timedJobsInCell(techId, dayStr, source?.id);
  if (!others.length) return Promise.resolve(toHHMM(shiftStart));

  const ownMin = Math.round((jobHours(source) ?? ASSUMED_HOURS) * 60);
  const items = [];
  others.forEach((o, i) => {
    const startMin = i === 0
      ? (shiftStart + ownMin <= o.startMin ? shiftStart : roundDown15(Math.max(o.startMin - ownMin, 0)))
      : roundUp15(others[i - 1].endMin);
    items.push({ key: `slot-${i}`, slot: true, index: i, startMin });
    items.push({
      key: `job-${o.job.card_key || o.job.id}`,
      slot: false,
      label: displayCustomer(o.job),
      rangeLabel: `${clockLabel(o.startMin)}–${o.knownEnd ? clockLabel(o.endMin) : '?'}`,
    });
  });
  items.push({ key: `slot-${others.length}`, slot: true, index: others.length, startMin: roundUp15(others[others.length - 1].endMin) });

  // Preselect the first gap the job fits in; the end of the day otherwise.
  const slots = items.filter((it) => it.slot);
  const fits = slots.find((s) => s.index < others.length && s.startMin + ownMin <= others[s.index].startMin
    && (s.index === 0 || s.startMin >= others[s.index - 1].endMin));
  const pick = fits || slots[slots.length - 1];

  const [y, m, d] = dayStr.split('-').map(Number);
  const dayLabel = new Date(y, m - 1, d).toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  slotPromptHeader.value = `${techId ? techName(techId) : 'No tech'} · ${dayLabel}`;
  slotPromptJobLabel.value = displayCustomer(source) || 'this job';
  slotPromptItems.value = items;
  slotPromptChoice.value = pick.index;
  slotPromptTime.value = toHHMM(pick.startMin);
  slotPromptOpen.value = true;
  return new Promise((resolve) => { slotPromptResolve = resolve; });
}

function chooseSlot(item) {
  slotPromptChoice.value = item.index;
  slotPromptTime.value = toHHMM(item.startMin);
}

// Only the typed time is saved, so a slot shows as chosen only while the
// time box still says that slot's start.
function slotIsChosen(item) {
  return slotPromptChoice.value === item.index && slotPromptTime.value === toHHMM(item.startMin);
}

function resolveSlotPrompt(value) {
  const resolve = slotPromptResolve;
  slotPromptResolve = null;
  slotPromptOpen.value = false;
  if (resolve) resolve(value);
}

// A drop on a week cell: that tech, that day, at the time the card already
// had (shop wall clock). A card with no time yet takes one from
// pickWeekStart — shift start on an empty day, or a slot the person picks
// among the day's jobs. Every path
// below is one the day view already uses, so the duration prompt, partial-job
// guards and the server's visit refusals all still apply.
async function onWeekCellDrop(event, techId, dayStr) {
  dragOverTechId.value = null;
  const transferId = event?.dataTransfer?.getData('text/plain');
  const jobId = transferId || draggingJobId.value;
  if (!jobId) return;
  draggingJobId.value = null;
  const card = takeDraggedVisit(jobId);
  const from = takeDragOrigin();
  if (techId && refuseTimeOffDrop(techId, dayStr)) return;
  const source = card
    || jobs.value.find((j) => String(j.id) === String(jobId))
    || partialJobs.value.find((j) => String(j.id) === String(jobId))
    || scheduledUnassigned.value.find((j) => String(j.id) === String(jobId));
  const isPartial = partialJobIds.value.has(String(jobId));
  const wall = source?.scheduled_at && !isPartial ? toShopWallClock(source.scheduled_at, tenantTimezone.value) : null;
  let hh = wall ? wall.getHours() : 0;
  let mm = wall ? wall.getMinutes() : 0;
  const pad = (n) => String(n).padStart(2, '0');

  // A visit card and a partial job carry their own refusals (the server's).
  if (!card && !isPartial && refuseFinishedOrStartedDrop(source, techId)) return;
  if (!techId) {
    // The "No tech" row moves a date only; taking a tech off is the job page's call.
    if (card || isPartial || (source?.assigned_tech_ids || []).some(Boolean) || source?.technician_id) {
      toast.add({
        severity: 'warn',
        summary: 'Nothing was moved',
        detail: 'The "No tech" row only re-dates jobs that have no tech. Drop it on a tech\'s row, or change the crew on the job page.',
        life: 6000,
      });
      return;
    }
  }
  if (hh === 0 && mm === 0) {
    const start = await pickWeekStart(source, techId, dayStr);
    if (!start) return; // cancelled: nothing moves
    [hh, mm] = start.split(':').map(Number);
  }
  if (card) {
    await sendVisitMove(card, techId, dayStr, `${pad(hh)}:${pad(mm)}`);
    return;
  }
  const [y, m, d] = dayStr.split('-').map(Number);
  const iso = shopWallClockToIso(new Date(y, m - 1, d, hh, mm), tenantTimezone.value);
  if (isPartial) {
    await placePartialJob(jobId, techId, iso, dayStr);
    return;
  }
  if (await scheduleUndatedDrop(jobId, techId, iso, source)) return;
  // A job with a crew is drawn in every crew member's row, so a drop in any
  // of those rows is a date move. It must not go through assignJob: one
  // assigned_tech_id replaces the whole crew on the server, which took the
  // lead off a two-tech job dragged along the helper's row (/audit, 2026-10-15).
  const crew = (source?.assigned_tech_ids || []).map(String).filter(Boolean);
  if (source?.technician_id && !crew.includes(String(source.technician_id))) crew.push(String(source.technician_id));
  const onCrew = techId ? crew.includes(String(techId)) : crew.length === 0;
  if (source && onCrew) {
    // An undated job dropped in a tech's row was booked above, hours asked
    // there (GDXA-1261). What reaches here is a dated job's move, which is
    // checked against that tech's shift, or the "No tech" row, which only
    // dates: it has no capacity to count, as _needsDurationPrompt exempts
    // techId=null.
    const crewNow = crewOf(source);
    const fit = await fitDatedPlacement(source, techId, iso, crewNow, crewNow[0]);
    if (!fit) return; // cancelled: nothing moves
    await withBoardWrite(async () => {
      let moved = false;
      try {
        // The "No tech" row only dates: a parked job with no tech stays parked.
        await api.patch(`/api/jobs/${jobId}`, { scheduled_at: iso, ...(techId ? leaveLanePatch(source) : {}), ...fit.patch });
        moved = true;
      } catch { /* api composable already toasts */ }
      if (moved && fit.booking) await bookLaterDays(jobId, source, fit.booking);
      await fetchJobs();
    });
    return;
  }
  if (await placeCrewJob(jobId, techId, iso, from, source)) return;
  await assignJob(jobId, techId, iso);
}

// --- Crew-safe placement (Doug, 2026-10-15) ---
// One assigned_tech_id replaces the whole crew on the server, so a job with a
// crew of two or more never goes through assignJob. Dropped on a crew member
// it moves the time only; dropped on anyone else it asks whether to swap one
// member for that tech or add them, and Cancel moves nothing.
function findBoardJob(jobId) {
  const id = String(jobId);
  return jobs.value.find((j) => String(j.id) === id)
    || scheduledUnassigned.value.find((j) => String(j.id) === id)
    || unassignedJobs.value.find((j) => String(j.id) === id)
    || null;
}

// A drop on a tech's row writes the job's date and crew, so it gets the same
// stage check a drop into a holding area or the queue does (GDXA-1377). A
// finished job keeps both. A job under way keeps its crew: a drop on one of
// its own techs (or, with nobody on it, on the "No tech" row) moves only its
// time; a drop on anyone else would replace the people working it. True when
// refused: the toast is shown and the caller writes nothing.
function refuseFinishedOrStartedDrop(job, techId) {
  if (!job) return false;
  let detail = null;
  if (isTerminalJob(job)) {
    detail = 'A finished job keeps its date and crew. Change it on the job page.';
  } else if (jobStage(job) === 'in_progress') {
    const crew = crewOf(job);
    const onCrew = techId ? crew.includes(String(techId)) : crew.length === 0;
    if (!onCrew) {
      detail = crew.length
        ? 'A job that is under way keeps its crew. Move its time on a crew member\'s row, or change the crew on the job page.'
        : 'A job that is under way keeps its crew. Change it on the job page.';
    }
  }
  if (!detail) return false;
  toast.add({ severity: 'warn', summary: 'Nothing was moved', detail, life: 6000 });
  return true;
}

function takeDragOrigin() {
  const from = draggingFromTechId;
  draggingFromTechId = null;
  return from;
}

const { prompt: crewPrompt, ask: askCrew, answer: answerCrewPrompt } = useCrewChangePrompt((id) => techName(id));
const { prompt: schedulePrompt, ask: askSchedule, answer: answerSchedulePrompt } = useScheduleDropPrompt();
const { prompt: overflowPrompt, ask: askOverflow, answer: answerOverflowPrompt } = useDayOverflowPrompt();

// A job with no date dropped onto a tech is being booked, not moved: ask the
// hours and the whole crew first, then write date, crew and hours in one
// PATCH, as the job page's Schedule dialog does (Doug, 2026-10-16). True when
// handled (booked or cancelled); false sends a dated job on to the move paths.
// Callers have already routed visit cards and partial jobs elsewhere.
async function scheduleUndatedDrop(jobId, techId, scheduledAt, knownJob = null) {
  if (!techId || !scheduledAt) return false;
  const job = knownJob || findBoardJob(jobId);
  if (!job || job.scheduled_at) return false;
  // A job under way keeps its crew, and the dialog would let it change: it
  // goes on to the move paths, which date it on its own crew (GDXA-1377).
  if (jobStage(job) === 'in_progress') return false;
  const wall = toShopWallClock(scheduledAt, tenantTimezone.value);
  const timed = wall.getHours() !== 0 || wall.getMinutes() !== 0;
  const answer = await askSchedule({
    jobLabel: displayCustomer(job),
    whenLabel: timed
      ? formatScheduled(scheduledAt)
      : `${formatShopDay(`${wall.getFullYear()}-${String(wall.getMonth() + 1).padStart(2, '0')}-${String(wall.getDate()).padStart(2, '0')}`)}, no set time`,
    techId,
    crew: crewOf(job),
    hours: Number(job.scheduled_duration_hours) > 0 ? job.scheduled_duration_hours : job.effective_duration_hours,
    // No hours of its own: the dialog prefills from the estimate, over the
    // crew picked in it, as the hours prompt does (GDXA-1262).
    suggest: jobHasDuration(job) ? null : () => suggestedEstimate(job.id),
  });
  if (!answer) return true; // cancelled: nothing moves
  const booking = await fitBookingInDay(job, answer, techId, scheduledAt);
  if (!booking) return true; // cancelled: nothing moves
  const patch = { ...scheduleDropPatch(booking.answer, scheduledAt), ...leaveLanePatch(job) };
  if (booking.days) patch.visit_duration_minutes = Math.round(booking.days[0].hours * 60);
  await withBoardWrite(async () => {
    let booked = false;
    try {
      await api.patch(`/api/jobs/${jobId}`, patch);
      booked = true;
    } catch { /* api composable already toasts */ }
    if (booked && booking.days) await bookLaterDays(jobId, job, booking);
    await Promise.all([fetchJobs(), fetchScheduledUnassigned()]);
  });
  return true;
}

// A booking longer than what is left of the dropped-on tech's shift asks
// first (Doug, 2026-10-18): add a tech, book the rest on that tech's next
// workdays, or keep it all on the day. Resolves { answer, days, shift } —
// days null for one day — or null on Cancel. A job with no hours, or a tech
// with no shift, has nothing to measure and books as it did.
async function fitBookingInDay(job, answer, techId, scheduledAt) {
  const saved = Number(job.scheduled_duration_hours) > 0 ? job.scheduled_duration_hours : job.effective_duration_hours;
  const hours = Number(answer.hours ?? saved);
  const tech = technicians.value.find((t) => String(t.id) === String(techId));
  const shift = tech ? shiftHours(tech.effective_shift_start, tech.effective_shift_end) : null;
  const wall = toShopWallClock(scheduledAt, tenantTimezone.value);
  const timed = wall.getHours() !== 0 || wall.getMinutes() !== 0;
  const left = hoursLeftInDay(timed ? wall.getHours() + wall.getMinutes() / 60 : null, shift);
  if (!(hours > 0) || left == null || hours <= left) return { answer, days: null, shift };
  const pad = (n) => String(n).padStart(2, '0');
  const choice = await askOverflow({
    jobLabel: displayCustomer(job),
    techName: tech.name,
    day: `${wall.getFullYear()}-${pad(wall.getMonth() + 1)}-${pad(wall.getDate())}`,
    shift,
    mask: tech.effective_workdays,
    hours,
    hoursLeft: left,
    techIds: answer.tech_ids.map(String),
    leadId: String(answer.lead),
    droppedId: String(techId),
  });
  if (!choice) return null;
  // Hours the first dialog would not have re-sent stay unsent, unless a
  // bigger crew changed them here.
  const send = answer.hours != null || choice.hours !== hours;
  return {
    answer: { tech_ids: choice.tech_ids, lead: choice.lead, hours: send ? choice.hours : null },
    days: choice.days,
    shift,
  };
}

// A job that already has a date, put in a tech's day, gets the same check
// (Doug, 2026-10-18): an 8 h block moved to 3:45 PM used to land "+6.75h
// over" with nothing asked. `when` is the new start, else the job's own;
// techIds and lead are the crew the write leaves, and hours any the caller
// is about to write. Resolves { patch, booking } — what to add to the
// caller's PATCH, and the split for bookLaterDays (null for one day) — or
// null on Cancel, when the caller writes nothing. Day one's length goes as
// visit_duration_minutes, which the server sets on the job's first-day
// visits (E7): a moved visit otherwise keeps its old length. A job under
// way keeps its crew and the dialog could change it, so it is not asked.
async function fitDatedPlacement(job, techId, when, techIds, lead, hours = null) {
  const none = { patch: {}, booking: null };
  const start = when || job?.scheduled_at;
  if (!job || !techId || !start || jobStage(job) === 'in_progress') return none;
  const asked = { tech_ids: techIds.map(String), lead: String(lead ?? techIds[0]), hours };
  const booking = await fitBookingInDay(job, asked, techId, start);
  if (!booking) return null;
  if (booking.answer === asked) return none; // it fits: nothing was asked
  const { answer, days } = booking;
  const patch = {};
  if (answer.tech_ids.join() !== asked.tech_ids.join() || String(answer.lead) !== asked.lead) {
    Object.assign(patch, crewPatch(answer));
  }
  if (answer.hours != null) patch.scheduled_duration_hours = answer.hours;
  // A visit is 15 min to one day (the PATCH's bounds); hours kept on one
  // day outside them leave the visit its own length rather than fail the save.
  const firstDay = Math.round((days ? days[0].hours : answer.hours) * 60);
  if (firstDay >= 15 && firstDay <= 24 * 60) patch.visit_duration_minutes = firstDay;
  return { patch, booking: days ? booking : null };
}

// The days after the first of a split booking, through the job's Visits
// route: each starts at shift start, for the whole crew. Days of the same
// length go in one request. A refusal is the server's own sentence; the
// first day stays booked, and the rest can be added from the Visits card.
async function bookLaterDays(jobId, job, { answer, days, shift }) {
  const later = days.slice(1);
  const groups = [];
  for (const d of later) {
    const last = groups[groups.length - 1];
    if (last && last.hours === d.hours) last.days.push(d.day);
    else groups.push({ hours: d.hours, days: [d.day] });
  }
  try {
    for (const g of groups) {
      await api.post(`/api/jobs/${jobId}/visits`, {
        days: g.days,
        start_time: toHHMM(Math.round(shift.start * 60)),
        duration_minutes: Math.round(g.hours * 60),
        tech_ids: answer.tech_ids,
      }, { suppressErrorToast: true });
    }
  } catch (e) {
    toast.add({
      severity: 'warn',
      summary: `Only part of ${displayCustomer(job)} was booked`,
      detail: `${refusalOf(e)?.detail || e?.message || 'The other days could not be booked.'} Add them from the Visits card on the job page.`,
      life: 9000,
    });
    return;
  }
  toast.add({
    severity: 'success',
    summary: 'Booked over several days',
    detail: `${displayCustomer(job)}: ${days.length} workdays.`,
    life: 4000,
  });
}

// The day a column drop with no slot lands on: shop midnight of the board
// day, the tray (the same day _doAssignJobInner gives an undated job).
function boardDayStartIso() {
  const d = new Date(selectedDate.value || new Date());
  d.setHours(0, 0, 0, 0);
  return d.toISOString();
}
const { confirmAsync } = useDestructiveConfirm();

// Dropping a crew job into the queue or a holding area takes every tech off
// it (the PATCH clears the crew), so a crew of two or more asks first. A
// one-tech drop is the plain unassign the drag is for.
async function okToClearCrew(job) {
  const crew = crewOf(job);
  if (crew.length < 2) return true;
  return confirmAsync({
    header: "Remove every technician?",
    message: `${crew.map((id) => techName(id)).join(", ")} will be taken off this job, and it goes back to unassigned.`,
    acceptLabel: "Remove",
  });
}

// A job put in a tech's day leaves its lane: it is booked, not waiting, and
// a lane lists by holding_area_id alone, so a stale one draws the job in the
// lane and in the tech's column at once (GDXA-1378). The job page's Schedule
// dialog clears it when it books a date. A job under way keeps its lane,
// parked with its crew on (GDXA-1370); a partial job being booked its next
// day is no longer waiting either way.
function leaveLanePatch(job, { partial = false } = {}) {
  if (!job?.holding_area_id) return {};
  if (!partial && jobStage(job) === 'in_progress') return {};
  return { holding_area_id: null };
}

// True when the drop was a crew job and is handled here (moved, re-crewed
// or cancelled); false sends the caller on to assignJob.
async function placeCrewJob(jobId, techId, scheduledAt, fromTechId, knownJob = null) {
  if (!techId) return false;
  const job = knownJob || findBoardJob(jobId);
  const crew = crewOf(job);
  if (crew.length < 2) return false;
  const moved = scheduledAt
    && new Date(scheduledAt).getTime() !== new Date(job.scheduled_at || 0).getTime();
  if (crew.includes(String(techId))) {
    if (!moved) return true;
    const datePatch = { scheduled_at: scheduledAt, ...leaveLanePatch(job) };
    // An undated job landing in its crew's day for the first time gets the
    // hours question a one-tech pick gets; a time move of a dated job doesn't.
    if (!job.scheduled_at && !jobHasDuration(job)) {
      const hours = await askDurationFor(job, crew.length);
      if (hours === null) return true;
      if (hours > 0) datePatch.scheduled_duration_hours = hours;
    }
    const fit = await fitDatedPlacement(job, techId, scheduledAt, crew, crew[0], datePatch.scheduled_duration_hours ?? null);
    if (!fit) return true; // cancelled: nothing moves
    Object.assign(datePatch, fit.patch);
    await withBoardWrite(async () => {
      let moved = false;
      try {
        await api.patch(`/api/jobs/${jobId}`, datePatch);
        moved = true;
      } catch { /* api composable already toasts */ }
      if (moved && fit.booking) await bookLaterDays(jobId, job, fit.booking);
      await fetchJobs();
    });
    return true;
  }
  const choice = await askCrew({
    crew, techId, jobLabel: displayCustomer(job), firstId: fromTechId,
  });
  if (!choice) return true; // cancelled: nothing moves
  const patch = { ...crewPatch(choice), ...leaveLanePatch(job) };
  if (moved) patch.scheduled_at = scheduledAt;
  // A swap or add puts the job in a new tech's day, so it gets the same
  // duration question a one-tech drop does (assignJob); closing it cancels.
  if (!jobHasDuration(job)) {
    const hours = await askDurationFor(job, choice.tech_ids?.length);
    if (hours === null) return true;
    if (hours > 0) patch.scheduled_duration_hours = hours;
  }
  const fit = await fitDatedPlacement(
    job, techId, scheduledAt, choice.tech_ids, choice.lead, patch.scheduled_duration_hours ?? null,
  );
  if (!fit) return true; // cancelled: nothing moves
  Object.assign(patch, fit.patch);
  await withBoardWrite(async () => {
    let placed = false;
    try {
      await api.patch(`/api/jobs/${jobId}`, patch);
      placed = true;
    } catch { /* api composable already toasts */ }
    if (placed && fit.booking) await bookLaterDays(jobId, job, fit.booking);
    await Promise.all([fetchJobs(), fetchScheduledUnassigned()]);
  });
  return true;
}

async function handleDrop(techId, event) {
  event?.preventDefault?.();
  dragOverTechId.value = null;
  const transferId = event?.dataTransfer?.getData('text/plain');
  const dropJobId = transferId || draggingJobId.value;
  if (!dropJobId) return;
  draggingJobId.value = null;
  const card = takeDraggedVisit(dropJobId);
  const from = takeDragOrigin();
  if (card) {
    await moveVisitCard(card, techId, card.scheduled_at);
    return;
  }
  const isPartial = partialJobIds.value.has(String(dropJobId));
  const job = findBoardJob(dropJobId);
  if (!isPartial && refuseFinishedOrStartedDrop(job, techId)) return;
  if (!isPartial && await scheduleUndatedDrop(dropJobId, techId, boardDayStartIso())) return;
  // An undated job that got here (one under way) lands on the board day, as
  // a one-tech job does in _doAssignJobInner; a dated one keeps its time.
  const day = job && !job.scheduled_at ? boardDayStartIso() : null;
  if (await placeCrewJob(dropJobId, techId, day, from)) return;
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
  return jobHasDuration(job) ? null : job;
}

function jobHasDuration(job) {
  return Number(job?.scheduled_duration_hours) > 0 || Number(job?.effective_duration_hours) > 0;
}

// The crew path (placeCrewJob) asks the same question but sends the hours in
// its own crew PATCH: it waits on this instead of the prompt assigning.
// Resolves the hours, 0 for Skip, or null when the dialog is closed.
let durationPromptAfter = null;
// crewSize = how many techs the job will have once this drop saves.
function askDurationFor(job, crewSize) {
  if (durationPromptAfter) durationPromptAfter(null);
  durationPromptJobId.value = job.id;
  durationPromptTechId.value = null;
  durationPromptScheduledAt.value = null;
  durationPromptHours.value = '';
  durationPromptJobLabel.value = job.customer_name || job.title || 'Job';
  durationPromptOpen.value = true;
  prefillDurationFromEstimate(job.id, crewSize);
  return new Promise((resolve) => { durationPromptAfter = resolve; });
}

// The list endpoint leaves estimate man-hours out (N+1 on the board's hot
// path), so ask for this one job when its prompt opens. A suggestion only:
// nothing is written until Save. The hours are the estimate's man-hours over
// the crew this drop creates, not the endpoint's duration_minutes: the prompt
// opens before the assignment saves, so the server would divide by the crew
// as it stands (or the labor row's default crew), not by who is being placed.
// A failed or empty answer leaves the prompt blank, and a late answer never
// overwrites what the scheduler typed or a prompt opened since.
// The "Schedule this job" dialog asks the same through suggestedEstimate.
let durationPrefillSeq = 0;
async function prefillDurationFromEstimate(jobId, crewSize) {
  const seq = ++durationPrefillSeq;
  durationPromptEstimate.value = null;
  const est = await suggestedEstimate(jobId);
  if (!est) return;
  if (seq !== durationPrefillSeq || !durationPromptOpen.value) return;
  if (durationPromptHours.value !== '') return;
  const crew = Math.max(1, Number(crewSize) || 1);
  const hours = estimateHoursOver(est.manHours, crew);
  durationPromptHours.value = String(hours);
  durationPromptEstimate.value = { ...est, crew, hours };
}

// One job's estimate man-hours, { manHours, accepted }, or null when it has
// none or the request fails (no toast: the prompt just stays blank).
async function suggestedEstimate(jobId) {
  let s;
  try {
    s = await api.get(`/api/jobs/${encodeURIComponent(jobId)}/suggested-duration`,
      { suppressErrorToast: true });
  } catch (_e) {
    return null;
  }
  const manHours = Number(s?.man_hours);
  if (!(manHours > 0)) return null;
  return { manHours, accepted: s.estimate_status === 'accepted' };
}

function onDurationPromptHide() {
  if (!durationPromptAfter) return;
  const after = durationPromptAfter;
  durationPromptAfter = null;
  after(null);
}

async function confirmDurationPrompt(skip = false) {
  if (durationPromptAfter) {
    const after = durationPromptAfter;
    durationPromptAfter = null;
    const picked = Number(durationPromptHours.value);
    durationPromptOpen.value = false;
    after(!skip && Number.isFinite(picked) && picked > 0 ? picked : 0);
    return;
  }
  const jobId = durationPromptJobId.value;
  const techId = durationPromptTechId.value;
  const scheduledAt = durationPromptScheduledAt.value;
  const hours = Number(durationPromptHours.value);
  durationPromptOpen.value = false;
  if (!jobId || !techId) return;
  const picked = !skip && Number.isFinite(hours) && hours > 0;
  // The hours just typed are measured against the tech's day before either
  // PATCH, so Cancel on the overflow question writes nothing at all.
  const fit = picked ? await fitOneTech(jobId, techId, scheduledAt, hours) : { patch: {}, booking: null };
  if (!fit) return;
  // Skip = assign without recording a duration (card stays "?h"). Confirm =
  // PATCH duration first so the column total reflects it; the assignment
  // PATCH follows. Pass {skipPrompt:true} so we don't re-enter the prompt.
  await withBoardWrite(async () => {
    if (picked) {
      try {
        await api.patch(`/api/jobs/${jobId}`, { scheduled_duration_hours: hours });
      } catch (_e) {
        // api composable surfaces the toast; fall through and still try to assign.
      }
    }
    // _doAssignJob nests withBoardWrite (counter, so nesting is fine) — keeps
    // the veto held across the duration PATCH + the assign PATCH as one unit.
    await _doAssignJob(jobId, techId, scheduledAt, fit);
  });
}

// Hand off to the job page's schedule dialog (the only surface that writes
// Job.scheduled_at + crew together, which is what the appointments mirror and
// the lifecycle stage both key off).
function openScheduleDialog(jobId) {
  router.push(`/jobs/${encodeURIComponent(jobId)}?schedule=1`);
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
    // A one-tech assignment: the dropped tech does the hours alone.
    prefillDurationFromEstimate(jobId, 1);
    return;
  }
  const fit = await fitOneTech(jobId, techId, scheduledAt);
  if (!fit) return; // cancelled: nothing moves
  await _doAssignJob(jobId, techId, scheduledAt, fit);
}

// The overflow check for a one-tech assignment of a dated job. A pick of
// the tech it already has at the time it already has re-saves nothing, so
// it is not asked; an undated job lands in the tray (the whole shift).
async function fitOneTech(jobId, techId, scheduledAt, hours = null) {
  const job = findBoardJob(jobId);
  if (!job) return { patch: {}, booking: null };
  const sameTech = String(job.technician_id ?? job.assigned_to ?? '') === String(techId);
  if (sameTech && !scheduledAt && hours == null) return { patch: {}, booking: null };
  return fitDatedPlacement(job, techId, scheduledAt || job.scheduled_at || boardDayStartIso(), [techId], techId, hours);
}

async function _doAssignJob(jobId, techId, scheduledAt = null, fit = null) {
  // Hold the poll veto for the entire optimistic-write + PATCH lifecycle so a
  // background refresh can't overwrite the move before the server confirms.
  return withBoardWrite(() => _doAssignJobInner(jobId, techId, scheduledAt, fit));
}

// `fit` is fitDatedPlacement's answer: its fields join the PATCH, and a
// split books its later days once the PATCH lands.
async function _doAssignJobInner(jobId, techId, scheduledAt = null, fit = null) {
  const jobIndex = jobs.value.findIndex((j) => String(j.id) === String(jobId));
  const sourceJob = jobIndex >= 0 ? jobs.value[jobIndex] : null;
  const laneJob = scheduledUnassigned.value.find((j) => String(j.id) === String(jobId));

  // Sprint dispatch-timeline (2026-05-21) — three scheduled_at paths:
  //   (a) caller passed an explicit ISO (timeline drop on a specific slot)
  //       → use it verbatim. The block is now anchored to that time.
  //   (b) job already has a scheduled_at → leave it alone. A pure tech
  //       reassignment (the card's dropdown) shouldn't move time.
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
  // Only a drop that gives the job this tech or a date takes it out of its
  // lane: one let go on the tech it already has changes nothing.
  const laneSource = sourceJob || findBoardJob(jobId);
  if (techId && (patch.scheduled_at || String(laneSource?.technician_id ?? '') !== String(techId))) {
    Object.assign(patch, leaveLanePatch(laneSource));
  }
  Object.assign(patch, fit?.patch || {});
  const bookRest = fit?.booking ? () => bookLaterDays(jobId, laneSource, fit.booking) : null;

  // Job not in the per-day list (e.g., dragged from the lane which holds
  // any-date jobs) — PATCH and refresh both lists, then toast so the user
  // sees the assignment landed even if the card moves out of view.
  if (jobIndex === -1) {
    const techName = (technicians.value.find((t) => String(t.id) === String(techId)) || {}).name || 'tech';
    try {
      await api.patch(`/api/jobs/${jobId}`, patch);
      if (bookRest) await bookRest();
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
  const previousArea = jobs.value[jobIndex].holding_area_id;
  jobs.value[jobIndex] = {
    ...jobs.value[jobIndex],
    technician_id: techId,
    ...(patch.scheduled_at ? { scheduled_at: patch.scheduled_at } : {}),
    ...('holding_area_id' in patch ? { holding_area_id: null } : {}),
  };

  try {
    await api.patch(`/api/jobs/${jobId}`, { ...patch, technician_id: techId });
    if (bookRest) await bookRest();
    // Refresh if we wrote a date or a crew so the row reflects it (and the
    // timeline re-renders the block at the new position).
    if (patch.scheduled_at || bookRest || patch.assigned_tech_ids) await fetchJobs();
  } catch {
    jobs.value[jobIndex] = {
      ...jobs.value[jobIndex],
      technician_id: previousTech,
      scheduled_at: previousSchedAt,
      holding_area_id: previousArea,
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
  const card = takeDraggedVisit(jobId);
  const from = takeDragOrigin();
  if (refuseTimeOffDrop(techId, selectedDateStr.value)) return;
  if (card) {
    await moveVisitCard(card, techId, startISO);
    return;
  }
  if (partialJobIds.value.has(String(jobId))) {
    await placePartialJob(jobId, techId, startISO);
    return;
  }
  if (refuseFinishedOrStartedDrop(findBoardJob(jobId), techId)) return;
  if (await scheduleUndatedDrop(jobId, techId, startISO)) return;
  // A crew job moved along a helper's column was checked against the lead
  // only, fell through to assignJob and lost its crew (Doug, 2026-10-15).
  if (await placeCrewJob(jobId, techId, startISO, from)) return;
  // Same-tech reschedule with existing duration → patch scheduled_at
  // only, no duration prompt (we know the height already, and the block
  // is just moving on the same tech's timeline).
  const job = jobs.value.find((j) => String(j.id) === String(jobId));
  const sameTech = job && String(job.technician_id) === String(techId);
  const hasDuration = job
    && (Number(job.scheduled_duration_hours) > 0
        || Number(job.effective_duration_hours) > 0);
  if (sameTech && hasDuration) {
    // The move is measured against what is left of the tech's day first.
    const fit = await fitDatedPlacement(job, techId, startISO, [techId], techId);
    if (!fit) return; // cancelled: nothing moves
    const idx = jobs.value.findIndex((j) => String(j.id) === String(jobId));
    const prev = jobs.value[idx].scheduled_at;
    const prevArea = jobs.value[idx].holding_area_id;
    const lane = leaveLanePatch(job);
    await withBoardWrite(async () => {
      jobs.value[idx] = { ...jobs.value[idx], scheduled_at: startISO, ...lane };
      try {
        await api.patch(`/api/jobs/${jobId}`, { scheduled_at: startISO, ...lane, ...fit.patch });
      } catch {
        jobs.value[idx] = { ...jobs.value[idx], scheduled_at: prev, holding_area_id: prevArea };
        return;
      }
      if (fit.booking) await bookLaterDays(jobId, job, fit.booking);
      if (fit.booking || Object.keys(fit.patch).length) await fetchJobs();
    });
    return;
  }
  await assignJob(jobId, techId, startISO);
}

async function onTimelinePlaceTray({ jobId, techId }) {
  if (!jobId || !techId) return;
  const card = takeDraggedVisit(jobId);
  const from = takeDragOrigin();
  if (refuseTimeOffDrop(techId, selectedDateStr.value)) return;
  if (card) {
    // The tray is the day with no time: shop midnight of the board day.
    await sendVisitMove(card, techId, selectedDateStr.value, '00:00');
    return;
  }
  const d = new Date(selectedDate.value || new Date());
  d.setHours(0, 0, 0, 0);
  if (partialJobIds.value.has(String(jobId))) {
    const pad = (n) => String(n).padStart(2, '0');
    const boardDay = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    await placePartialJob(jobId, techId, d.toISOString(), boardDay);
    return;
  }
  if (refuseFinishedOrStartedDrop(findBoardJob(jobId), techId)) return;
  if (await scheduleUndatedDrop(jobId, techId, d.toISOString())) return;
  if (await placeCrewJob(jobId, techId, d.toISOString(), from)) return;
  await assignJob(jobId, techId, d.toISOString());
}

const queuePickerKey = ref(0);

// A tech picked from the queue card's dropdown. An undated job
// is being booked onto the selected day, so it asks hours and crew like a
// drop does; a dated crew job goes through placeCrewJob.
async function assignFromPicker(jobId, techId) {
  const job = findBoardJob(jobId);
  let day = null;
  if (job && !job.scheduled_at) {
    const d = new Date(selectedDate.value || new Date());
    d.setHours(0, 0, 0, 0);
    day = d.toISOString();
  }
  try {
    if (refuseFinishedOrStartedDrop(job, techId)) return;
    const dayStr = job?.scheduled_at ? zonedDateKey(job.scheduled_at) : selectedDateStr.value;
    if (refuseTimeOffDrop(techId, dayStr)) return;
    if (day && !partialJobIds.value.has(String(jobId))
      && await scheduleUndatedDrop(jobId, techId, day, job)) return;
    if (await placeCrewJob(jobId, techId, day, null)) return;
    await assignJob(jobId, techId);
  } finally {
    // The queue card's Select keeps its pick internally; remount it so a
    // cancelled pick doesn't leave a tech's name on a card nobody has.
    queuePickerKey.value += 1;
  }
}

// --- API ---

async function fetchTechnicians({ keepOnError = false } = {}) {
  try {
    const data = await api.get('/api/technicians');
    const rows = Array.isArray(data) ? data : data?.items || data?.data || [];
    // Keep every tech the API returns; the column-filtering rule lives in
    // visibleTechnicians so a job assigned to an inactive (or otherwise
    // hidden) tech still surfaces on the board instead of vanishing.
    technicians.value = rows.map(normalizeTech);
  } catch {
    if (!keepOnError) technicians.value = [];
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

// Which slice of the calendar the current view actually renders. The server
// widens any date scope by ±1 day (UTC) and lifts the page cap for scoped
// requests; matchesDate/zonedDateKey stays the precise tenant-zone cut.
// Week view and the custom range filter render multiple days from one fetch,
// so they must ask for the full range — a day-scoped fetch only carries
// selectedDate±1 and would leave the other week columns structurally empty.
function jobsDateQuery() {
  const [start, end] = dateRangeFilter.value || [];
  if (start && end) {
    return `date_from=${toDateStr(new Date(start))}&date_to=${toDateStr(new Date(end))}`;
  }
  if (viewMode.value === 'week') {
    const s = new Date(selectedDate.value);
    s.setDate(s.getDate() - s.getDay()); // Sunday of the selected week
    const e = new Date(s);
    e.setDate(s.getDate() + 6); // Saturday
    return `date_from=${toDateStr(s)}&date_to=${toDateStr(e)}`;
  }
  return `date=${selectedDateStr.value}`;
}

// Only the latest call may write: a date change, a poll and a post-write
// re-read can overlap, and an older answer landing last would redraw the
// wrong day.
let fetchSeq = 0;
const VISIT_WINDOW_MAX_DAYS = 366;

function visitsReadable(query) {
  if (!hasPermission('jobs.read_all')) return false;
  const from = /date_from=([\d-]+)/.exec(query);
  const to = /date_to=([\d-]+)/.exec(query);
  if (!from || !to) return true;
  const span = (new Date(to[1]) - new Date(from[1])) / 86_400_000;
  return span >= 0 && span < VISIT_WINDOW_MAX_DAYS;
}

async function fetchJobs(opts) {
  let ok = false;
  drawerListReads += 1;
  try {
    ok = await readJobs(opts);
  } finally {
    settleDrawerRead(ok);
  }
}

// Returns whether the jobs list was replaced.
async function readJobs({ keepOnError = false } = {}) {
  const seq = ++fetchSeq;
  const query = jobsDateQuery();
  const settle = (p) => p.then((d) => ({ ok: true, d }), (e) => ({ ok: false, e }));
  const boardReadable = visitsReadable(query);
  const [jobsRes, visitsRes, timeOffRes] = await Promise.all([
    settle(api.get(`/api/jobs?${query}`)),
    boardReadable ? settle(api.get(`/api/dispatch/visits?${query}`)) : Promise.resolve(null),
    boardReadable ? settle(api.get(`/api/dispatch/time-off?${query}`)) : Promise.resolve(null),
  ]);
  if (seq !== fetchSeq) return false;
  if (timeOffRes === null) {
    boardTimeOff.value = [];
    timeOffUnread = false;
  } else if (timeOffRes.ok) {
    boardTimeOff.value = Array.isArray(timeOffRes.d?.items) ? timeOffRes.d.items : [];
    timeOffUnread = false;
  } else {
    console.warn('board_time_off_failed', timeOffRes.e);
    if (!keepOnError) boardTimeOff.value = [];
    // A board that silently shows nobody off would take a drop onto
    // someone's vacation; say so once per outage, not on every poll.
    if (!timeOffUnread) {
      toast.add({
        severity: 'warn',
        summary: 'Time off could not be loaded',
        detail: 'The board may not show who is off. Check the time-off list before booking.',
        life: 8000,
      });
    }
    timeOffUnread = true;
  }
  if (visitsRes === null) {
    boardVisits.value = [];
  } else if (visitsRes.ok) {
    boardVisits.value = Array.isArray(visitsRes.d?.items) ? visitsRes.d.items : [];
  } else {
    console.warn('board_visits_failed', visitsRes.e);
    if (!keepOnError) boardVisits.value = [];
  }
  if (!jobsRes.ok) {
    if (!keepOnError) jobs.value = [];
    return false;
  }
  const data = jobsRes.d;
  const rows = Array.isArray(data) ? data : data?.items || data?.data || [];
  jobs.value = rows.map(normalizeJob);
  return true;
}

// The drawer must describe the job as it IS, not as it was when opened.
// The board polls every 45 s and replaces its lists, but drawerJob pointed at
// the old object — so after the tech closed out from the truck, the drawer
// still said "In Progress" and still offered Close out, and a second closeout
// would supersede the tech's attestation (audit 2026-08-28, #526). Re-point
// it; if the job left every list the drawer opens from, close the drawer
// rather than keep describing a ghost. A visit card is found by its card key
// first, so the drawer keeps describing the same day.
// It runs after every read of any of those lists, not the jobs read alone
// (GDXA-1989). A poll reads them all at once and they answer in any order, so
// a job moving between lists (booked from the partial list onto the board) is
// briefly on none: re-pointing is safe on partial evidence, closing is not, so
// the close waits until no read is in flight. A failed read keeps the drawer
// as it was, unless a close is waiting on it.
function repointDrawer() {
  if (!drawerJob.value) { drawerCloseDeferred = false; return; }
  const cards = drawerSourceJobs();
  const key = drawerJob.value.card_key;
  const fresh = (key && cards.find((j) => j.card_key === key))
    || cards.find((j) => String(j.id) === String(drawerJob.value.id));
  drawerCloseDeferred = false;
  if (fresh) drawerJob.value = fresh;
  else if (drawerListReads > 0) drawerCloseDeferred = true;
  else closeJobDrawer();
}

function settleDrawerRead(ok) {
  drawerListReads -= 1;
  if (ok || drawerCloseDeferred) repointDrawer();
}

// The visits read is gated on jobs.read_all, which may resolve after the
// first board load; read again once it has.
watch(permissionsLoaded, (loaded) => { if (loaded) fetchJobs({ keepOnError: true }); });

async function fetchHoldingAreas({ keepOnError = false } = {}) {
  try {
    const res = await api.get("/api/holding-areas");
    holdingAreas.value = Array.isArray(res) ? res : [];
  } catch { if (!keepOnError) holdingAreas.value = []; }
}

async function loadDispatchSettings() {
  try {
    const f = await api.get("/api/dispatch-settings");
    if (f) dispatchSettings.value = { ...dispatchSettings.value, ...f };
  } catch (_e) { /* defaults stay off */ }
}

async function fetchScheduledUnassigned({ keepOnError = false } = {}) {
  if (!dispatchSettings.value.dispatch_show_unassigned_lane) {
    scheduledUnassigned.value = [];
    return;
  }
  let ok = false;
  drawerListReads += 1;
  try {
    const res = await api.get("/api/dispatch/scheduled-unassigned");
    scheduledUnassigned.value = Array.isArray(res?.items) ? res.items : [];
    ok = true;
  } catch {
    if (!keepOnError) scheduledUnassigned.value = [];
  } finally {
    settleDrawerRead(ok);
  }
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
  const options = { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' };
  // Render in the office zone so the card time matches the day column it sits
  // in. Omitted until the tz loads → browser-local, same as before.
  if (tenantTimezone.value) options.timeZone = tenantTimezone.value;
  return formatDateTime(iso, { options });
}

// The day alone: a tray drop is midnight, and "12:00 AM" is not a time
// anyone booked.
function formatBoardDay(iso) {
  if (!iso) return '';
  const options = { weekday: 'short', month: 'short', day: 'numeric' };
  if (tenantTimezone.value) options.timeZone = tenantTimezone.value;
  return formatDateTime(iso, { options });
}

// The week board's lanes hold the whole week's parked work, not one day's.
// Computed, so the per-job timezone date check runs once per data change and
// not on every re-render a drag-over causes (/audit, 2026-10-16).
const holdingAreaSourceJobs = computed(() => (viewMode.value === 'week'
  ? rangeFilteredJobs.value.filter((j) => weekDays.value.some((d) => matchesDate(j, d.date)))
  : dayJobs.value));

function getHoldingAreaJobs(areaId) {
  // A lane lists a job once: a multi-day job parked here has a card per day
  // on the board, and the lane total is the job's hours, not one day's.
  const seenJobs = new Set();
  const here = holdingAreaSourceJobs.value.filter((j) => {
    if (j.holding_area_id !== areaId || seenJobs.has(String(j.id))) return false;
    seenJobs.add(String(j.id));
    return true;
  }).map((j) => (j.visit_id ? { ...j, effective_duration_hours: j.job_duration_hours } : j));
  // A parked partial job is dated the day it was last worked, so the day's
  // job list never loads it; without this it would be on no screen at all.
  // Normalized like the day list, so the lane total and the drawer read
  // the same fields.
  const seen = new Set(here.map((j) => String(j.id)));
  const parked = normalizedPartialJobs.value.filter(
    (j) => j.holding_area_id && String(j.holding_area_id) === String(areaId) && !seen.has(String(j.id)),
  );
  return [...here, ...parked];
}

// Every list the drawer is opened from, board cards first, each normalized
// like the day list. repointDrawer reads this; re-pointing from the board
// alone closed a drawer opened on a parked partial job or a Scheduled — Not
// Assigned card dated outside the board's window, neither of which the jobs
// read loads (GDXA-1989).
function drawerSourceJobs() {
  return [...boardJobs.value, ...normalizedPartialJobs.value, ...scheduledUnassigned.value.map(normalizeJob)];
}

// Park or release a partial job: only the holding area changes (no crew or
// date edit) and the partial list is re-read. A partial job last worked on
// the board day is in `jobs` too, so that copy follows the server.
async function setPartialHoldingArea(jobId, areaId) {
  let ok = false;
  await withBoardWrite(async () => {
    try {
      await api.patch(`/api/jobs/${jobId}`, { holding_area_id: areaId });
      const idx = jobs.value.findIndex((j) => String(j.id) === String(jobId));
      if (idx !== -1) jobs.value[idx] = { ...jobs.value[idx], holding_area_id: areaId };
      ok = true;
    } catch {
      // useApi has already shown the server's refusal.
    }
    await loadPartialJobs({ keepOnError: true });
  });
  return ok;
}

async function moveToHoldingArea(event, areaId) {
  dragOverTechId.value = null;
  const transferId = event?.dataTransfer?.getData("text/plain");
  const jobId = transferId || draggingJobId.value;
  if (!jobId) return;
  // Let go over the lane it came from: a cancelled drag, not an edit. Keyed
  // on where the drag started, not on the lane listing the job — a parked
  // job with a tech is in its tech's row too, and dragging it from there
  // onto its lane is how its tech comes off (/audit, 2026-10-16).
  const fromLane = draggingFromLane;
  draggingFromLane = null;
  if (fromLane === `area:${areaId}`) {
    draggingJobId.value = null;
    draggingCard.value = null;
    return;
  }
  const card = takeDraggedVisit(jobId);
  draggingJobId.value = null;
  await moveJobToArea(jobId, areaId, card);
}

// Park a job in a holding area. A drop and the Move to… menu both land
// here; the tech comes off, the date stays.
function refuseOffScreenPark(job) {
  const day = job.scheduled_at ? formatBoardDay(job.scheduled_at) : 'another day';
  toast.add({
    severity: 'warn',
    summary: 'Nothing was moved',
    detail: `${job.customer_name || job.title || 'That job'} is dated ${day}, and a holding area lists the jobs of the day on screen. Open that day to park it, or give it a tech.`,
    life: 6000,
  });
}

// The jobs a lane would draw once parked: dated (or undated, so waiting) on
// a day on screen, or partial. Loaded is not enough: a day's read also
// brings the days either side, which no lane on this screen lists.
function canPark(job) {
  if (!job) return false;
  const id = String(job.id);
  return partialJobIds.value.has(id) || holdingAreaSourceJobs.value.some((j) => String(j.id) === id);
}

async function moveJobToArea(jobId, areaId, card = null, row = null) {
  if (card?.visit_id) {
    refuseWholeJobDrop(card, 'A holding area');
    return;
  }
  const moved = `Moved to ${areaName(areaId)}`;
  if (partialJobIds.value.has(String(jobId))) {
    const before = partialJobs.value.find((j) => String(j.id) === String(jobId));
    if (await setPartialHoldingArea(jobId, areaId)) {
      offerUndo(moved, jobId, { holding_area_id: before?.holding_area_id ?? null });
    }
    return;
  }
  const job = jobs.value.find((j) => String(j.id) === String(jobId));
  if (!job || !canPark(job)) {
    // A lane lists the jobs dated on screen, so a job dated another day (the
    // red lane's) would be parked out of sight: say so, write nothing.
    const other = job || findBoardJob(jobId) || row;
    if (other) refuseOffScreenPark(other);
    return;
  }
  if (isTerminalJob(job)) {
    toast.add({
      severity: 'warn',
      summary: 'Nothing was moved',
      detail: 'A finished job is not parked. Change it on the job page.',
      life: 6000,
    });
    return;
  }
  // A job under way can be parked but keeps its crew: the techs are still
  // on it (Doug, 2026-10-17). Only the lane is written.
  const keepCrew = jobStage(job) === 'in_progress';
  // Already in this lane: the drag from its tech's row would take the tech
  // off, which a job under way does not allow, so there is nothing to do.
  if (keepCrew && String(job.holding_area_id || '') === String(areaId)) {
    toast.add({
      severity: 'warn',
      summary: 'Nothing was moved',
      detail: 'A job that is under way keeps its crew. Change it on the job page.',
      life: 6000,
    });
    return;
  }
  if (!keepCrew && !(await okToClearCrew(job))) return;
  await withBoardWrite(async () => {
    // Looked up again: the board can refresh while the confirm is open.
    const idx = jobs.value.findIndex((j) => String(j.id) === String(jobId));
    if (idx === -1) return;
    // Undo puts back what this move changed: the lane, and the crew only
    // when the move took it off.
    const undo = restorePatch(jobs.value[idx], { crew: !keepCrew });
    jobs.value[idx] = keepCrew
      ? { ...jobs.value[idx], holding_area_id: areaId }
      : { ...jobs.value[idx], holding_area_id: areaId, technician_id: null, assigned_tech_ids: [] };
    const patch = keepCrew ? { holding_area_id: areaId } : { assigned_to: null, holding_area_id: areaId };
    try {
      await api.patch(`/api/jobs/${jobId}`, patch);
      offerUndo(moved, jobId, undo);
    } catch { await refreshBoard(); }
  });
}

// Drop onto the "New Jobs to Schedule" queue. The old way to do this was to
// drag onto the "Ready to Schedule" holding-area column; that column no longer
// renders separately (its jobs ARE this queue), so the queue takes the drop.
// Routes the job back to the intake area and clears the tech, mirroring
// moveToHoldingArea. A drop here takes the job off the schedule, so it also
// clears the date: left dated, it stayed booked on the day it was dragged off
// with nobody on it — on the week board's "No tech" row and in this queue as
// "needs a tech" (Doug, 2026-10-16).
async function moveToScheduleQueue(event) {
  dragOverTechId.value = null;
  const transferId = event?.dataTransfer?.getData("text/plain");
  const jobId = transferId || draggingJobId.value;
  if (!jobId) return;
  // Let go over the queue it came from: a cancelled drag, not an edit — it
  // would otherwise clear the date of a queued "needs a tech" job.
  const fromLane = draggingFromLane;
  draggingFromLane = null;
  if (fromLane === 'week-queue' || fromLane === 'day-queue') {
    draggingJobId.value = null;
    draggingCard.value = null;
    return;
  }
  const areaId = readyToScheduleAreaId.value || null;
  const card = takeDraggedVisit(jobId);
  if (card) {
    draggingJobId.value = null;
    refuseWholeJobDrop(card, 'The New Jobs queue');
    return;
  }
  // A partial job waits in Partial Jobs, not here: the drop only un-parks
  // it, keeping its crew for the next day.
  if (partialJobIds.value.has(String(jobId))) {
    draggingJobId.value = null;
    const before = partialJobs.value.find((j) => String(j.id) === String(jobId));
    if (await setPartialHoldingArea(jobId, areaId)) {
      offerUndo('Moved to New Jobs to Schedule', jobId, { holding_area_id: before?.holding_area_id ?? null });
    }
    return;
  }
  const job = jobs.value.find((j) => String(j.id) === String(jobId));
  if (!job) return;
  draggingJobId.value = null;
  // Work that is finished or under way keeps the day it was worked.
  if (isTerminalJob(job) || jobStage(job) === 'in_progress') {
    toast.add({
      severity: 'warn',
      summary: 'Nothing was moved',
      detail: 'A job that is finished or under way keeps its date and crew. Change it on the job page.',
      life: 6000,
    });
    return;
  }
  if (!(await okToClearCrew(job))) return;
  await withBoardWrite(async () => {
    // Looked up again: the board can refresh while the confirm is open.
    const idx = jobs.value.findIndex((j) => String(j.id) === String(jobId));
    if (idx === -1) return;
    const patch = { assigned_to: null, holding_area_id: areaId, scheduled_at: null };
    const undo = restorePatch(jobs.value[idx], { crew: true, date: true });
    jobs.value[idx] = {
      ...jobs.value[idx], holding_area_id: areaId, technician_id: null, assigned_tech_ids: [], scheduled_at: null,
    };
    try {
      await api.patch(`/api/jobs/${jobId}`, patch);
      offerUndo('Taken off the schedule', jobId, undo);
    } catch { await refreshBoard(); }
  });
}

async function releaseFromHoldingArea(jobId, card = null) {
  const job = card || findBoardJob(jobId) || partialJobs.value.find((j) => String(j.id) === String(jobId));
  const fromArea = job?.holding_area_id ?? null;
  const released = `Released from ${areaName(fromArea)}`;
  if (partialJobIds.value.has(String(jobId))) {
    const ok = await setPartialHoldingArea(jobId, null);
    // The lane's card may be a visit card, which only the visits read redraws.
    if (card?.visit_id) await fetchJobs();
    if (ok) offerUndo(released, jobId, { holding_area_id: fromArea });
    return;
  }
  // A multi-day job is drawn from its visits, and a job dated off the screen
  // is not loaded, so neither has a job row to edit in place: write the area
  // and re-read.
  const idx = jobs.value.findIndex((j) => String(j.id) === String(jobId));
  if (card?.visit_id || idx === -1) {
    await withBoardWrite(async () => {
      try {
        await api.patch(`/api/jobs/${jobId}`, { holding_area_id: null });
        offerUndo(released, jobId, { holding_area_id: fromArea });
      } catch {
        // useApi has already shown the server's refusal.
      }
      await refreshBoard();
    });
    return;
  }
  await withBoardWrite(async () => {
    jobs.value[idx] = { ...jobs.value[idx], holding_area_id: null };
    try {
      await api.patch(`/api/jobs/${jobId}`, { holding_area_id: null });
      offerUndo(released, jobId, { holding_area_id: fromArea });
    } catch { await refreshBoard(); }
  });
}

// --- Move to… and Undo (Doug, 2026-10-16) ---
// Every move a lane takes by drag is also on each card's Move to… menu, so
// nothing needs a drag across the screen; and the last move or Release can
// be put back with one click for a few seconds after.
const laneMenu = ref(null);
const laneMenuJob = ref(null);

function areaName(areaId) {
  const area = (holdingAreas.value || []).find((a) => String(a.id) === String(areaId));
  return area?.name || 'the holding area';
}

const drawerLaneName = computed(() => (isParked(drawerJob.value) ? areaName(drawerJob.value.holding_area_id) : ''));

const showRedLane = computed(() => viewMode.value === 'day' && !!dispatchSettings.value.dispatch_show_unassigned_lane);

function openLaneMenu(event, job) {
  laneMenuJob.value = job;
  laneMenu.value?.toggle(event);
}

const laneMenuItems = computed(() => {
  const job = laneMenuJob.value;
  if (!job) return [];
  const here = isParked(job) ? String(job.holding_area_id) : null;
  // One day of a multi-day job: a lane holds the whole job, so a move would
  // only be refused. Say where it moves instead of offering it.
  const items = [];
  // A job waiting on its doors to be ordered: one click says they are.
  if (inOrderDoorsLane(job) && !isTerminalJob(job)) {
    items.push({ label: 'Doors ordered', icon: 'pi pi-check-square', command: () => markDoorsOrdered(job) });
    items.push({ separator: true });
  }
  items.push({
    label: 'Move to',
    items: job.visit_id
      ? [{ label: 'Multi-day job: move it on its Visits card', icon: 'pi pi-info-circle', disabled: true }]
      : parkingHoldingAreas.value
        .filter((a) => String(a.id) !== here)
        .map((a) => ({ label: a.name, icon: 'pi pi-arrow-right', command: () => moveJobToArea(job.id, a.id, null, job) })),
  });
  if (here) {
    items.push({ separator: true });
    items.push({ label: `Release from ${areaName(here)}`, icon: 'pi pi-sign-out', command: () => releaseFromHoldingArea(job.id, job) });
  }
  return items;
});

// "Order Doors" is where estimate conversion parks a sold install; "Need to
// order" is the office's twin of it (vendor_orders/hubx.py ORDER_AREAS).
const ORDER_DOORS_LANES = ['order doors', 'need to order'];

function inOrderDoorsLane(job) {
  if (!isParked(job)) return false;
  const area = (holdingAreas.value || []).find((a) => String(a.id) === String(job.holding_area_id));
  return ORDER_DOORS_LANES.includes((area?.name || '').trim().toLowerCase());
}

// The doors went in: the server marks the job's doors still needed as
// ordered and moves it to "Waiting on doors", signed by whoever clicked. No
// Undo — it changes the parts too, which a lane move cannot put back; the
// job's parts list is where a door goes back to needed.
const markingDoorsJobId = ref(null);

async function markDoorsOrdered(job) {
  if (!job || markingDoorsJobId.value) return;
  markingDoorsJobId.value = String(job.id);
  try {
    await withBoardWrite(async () => {
      try {
        const res = await api.post(`/api/jobs/${job.id}/doors-ordered`, {});
        const doors = res?.parts_marked
          ? `${res.parts_marked} door${res.parts_marked === 1 ? '' : 's'} marked ordered`
          : 'Doors ordered';
        toast.add({
          severity: 'success',
          summary: res?.moved_to ? `${doors}, moved to ${res.moved_to}` : doors,
          life: 4000,
        });
      } catch {
        // useApi has already shown the server's refusal.
      }
      await refreshBoard();
    });
  } finally {
    markingDoorsJobId.value = null;
  }
}

// What puts a job back as it was before a lane move: its lane, and the crew
// or the date the move took off it.
function restorePatch(job, { crew = false, date = false } = {}) {
  const patch = { holding_area_id: job?.holding_area_id ?? null };
  if (crew) {
    const before = crewOf(job);
    if (before.length) {
      patch.assigned_tech_ids = before;
      patch.lead_tech_id = before[0];
    }
  }
  if (date && job?.scheduled_at) patch.scheduled_at = job.scheduled_at;
  return patch;
}

const UNDO_MS = 10000;
const lastMove = ref(null);
let lastMoveTimer = null;

function offerUndo(text, jobId, patch) {
  clearTimeout(lastMoveTimer);
  lastMove.value = { text, jobId: String(jobId), patch };
  lastMoveTimer = setTimeout(() => { lastMove.value = null; }, UNDO_MS);
}

function dismissUndo() {
  clearTimeout(lastMoveTimer);
  lastMove.value = null;
}

async function undoLastMove() {
  const move = lastMove.value;
  if (!move) return;
  dismissUndo();
  await withBoardWrite(async () => {
    try {
      await api.patch(`/api/jobs/${move.jobId}`, move.patch);
    } catch {
      // useApi has already shown the server's refusal.
    }
    await refreshBoard();
  });
}

// The trash sits in a lane's one-line header now, a short reach from the
// cards, so it asks first.
async function deleteHoldingArea(area) {
  const count = getHoldingAreaJobs(area.id).length;
  const ok = await confirmAsync({
    header: `Delete "${area.name}"?`,
    message: count
      ? `The ${count} job${count === 1 ? '' : 's'} in it leave the lane; nothing else about them changes.`
      : 'The lane is empty.',
    acceptLabel: 'Delete',
  });
  if (!ok) return;
  const areaId = area.id;
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
    await Promise.all([fetchTechnicians(), fetchJobs(), fetchHoldingAreas(), fetchScheduledUnassigned(), loadLateOpen(), loadPartialJobs()]);
    await loadSkillOptions();
  } finally {
    refreshing.value = false;
  }
}

// Background board auto-refresh so the dispatcher sees new/changed jobs and
// tech assignments without clicking Refresh. Deliberately does NOT toggle
// `refreshing` (which drives the manual button's spinner) — otherwise the
// button would flash a spinner every poll. Skips loadSkillOptions (static
// within a session) and guards its own overlap. keepOnError keeps the last
// good board on a transient API blip instead of blanking it unattended.
let autoPolling = false;
async function pollBoard() {
  if (autoPolling) return;
  autoPolling = true;
  try {
    await Promise.all([
      fetchTechnicians({ keepOnError: true }),
      fetchJobs({ keepOnError: true }),
      fetchHoldingAreas({ keepOnError: true }),
      fetchScheduledUnassigned({ keepOnError: true }),
      loadLateOpen({ keepOnError: true }),
      loadPartialJobs({ keepOnError: true }),
    ]);
  } finally {
    autoPolling = false;
  }
}

// Pause polling mid-drag AND across the optimistic-assign PATCH window
// (pendingWrites), plus while a manual refresh is in flight. usePollingRefresh
// also skips ticks when the tab is hidden and refreshes on tab re-focus.
usePollingRefresh(pollBoard, {
  intervalMs: 45_000,
  isPaused: () => refreshing.value || draggingJobId.value != null || pendingWrites.value > 0,
});

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

// Unmount during the awaited settings load and the continuation would start
// a 30 s live-tech poll nobody clears. Same shape as useOfflineSync, 2026-08-31.
let disposed = false;

onMounted(async () => {
  await loadDispatchSettings();
  if (disposed) return;
  refreshBoard();
  refreshLiveTechs();
  loadLaborExceptions();
  liveTechsTimer = setInterval(refreshLiveTechs, 30_000);
});

onBeforeUnmount(() => {
  clearTimeout(lastMoveTimer);
  disposed = true;
  if (liveTechsTimer) clearInterval(liveTechsTimer);
});

// Sprint dispatch-capacity (2026-05-21) — expose the surface the
// regression specs reach via wrapper.vm. Without defineExpose, the
// existing test_dispatch_drag.test.js + test_dispatch_capacity.test.js
// rely on Vue's dev-mode auto-expose, which a strict prod build can
// seal. Pinning the test contract explicitly is the cheapest fix.
// /audit 2026-05-21 finding 3.
defineExpose({
  // Job drawer + closeout (DispatchDrawerCloseout.spec.js, #526)
  openJobDrawer,
  closeJobDrawer,
  fetchJobs,
  // Existing dispatch surface (test_dispatch_drag.test.js)
  onDragStart,
  handleDrop,
  draggingJobId,
  boardJobs,
  moveToHoldingArea,
  moveToScheduleQueue,
  releaseFromHoldingArea,
  getHoldingAreaJobs,
  // Sprint dispatch-capacity / dispatch-timeline surface
  assignJob,
  onTimelinePlace,
  onTimelinePlaceTray,
  confirmDurationPrompt,
  durationPromptOpen,
  durationPromptJobId,
  durationPromptTechId,
  durationPromptScheduledAt,
  durationPromptHours,
  durationPromptEstimate,
});
</script>

<style scoped>
.week-slot-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.week-slot-option {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0.6rem;
  border: 1px dashed var(--p-content-border-color);
  border-radius: 6px;
  background: transparent;
  color: var(--p-text-muted-color);
  font: inherit;
  cursor: pointer;
  text-align: left;
}
.week-slot-option:hover,
.week-slot-option.is-selected {
  border-style: solid;
  border-color: var(--p-primary-color);
  color: var(--p-text-color);
}
.week-slot-job {
  display: flex;
  gap: 0.6rem;
  padding: 0.35rem 0.6rem;
  border-radius: 6px;
  background: var(--p-content-hover-background);
  color: var(--p-text-color);
}
.week-slot-job-time {
  font-weight: 600;
  min-width: 6.5rem;
}
.board-section.is-folded :deep(.p-card-body) {
  padding-top: 0.5rem;
  padding-bottom: 0.5rem;
}
.board-section.is-folded :deep(.p-card-content) {
  display: none;
}

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
  width: 200px;
}

.dispatch-calendar {
  max-width: 150px;
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
  background: var(--p-content-border-color);
  border-radius: 2px;
  overflow: hidden;
}
.tech-capacity-bar-fill {
  height: 100%;
  background: var(--p-primary-color);
  transition: width 0.2s ease-out;
}
.tech-capacity--over .tech-capacity-bar-fill {
  background: var(--p-red-500, #ef4444);
}
.tech-capacity--over .tech-capacity-label { color: var(--p-red-500, #ef4444); }
.tech-capacity--off { opacity: 0.55; }
.time-off-block {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.25rem 0.5rem;
  margin-bottom: 0.5rem;
  padding: 0.5rem 0.65rem;
  border: 1px dashed var(--p-orange-400, #fb923c);
  border-radius: var(--p-border-radius);
  background: color-mix(in srgb, var(--p-orange-400, #fb923c) 12%, transparent);
  color: var(--p-text-color);
  font-size: 0.8rem;
}
.time-off-block-title { font-weight: 600; }
.time-off-block-meta { color: var(--p-text-muted-color); font-size: 0.74rem; }
.job-duration { font-weight: 500; opacity: 0.85; }
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

.return-visit-icon { color: var(--color-warning-500, #d97706); font-size: 0.8em; margin-right: 0.25rem; }
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
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.assign-dropdown {
  /* flex-1 rather than 100% now that the schedule button shares the row */
  flex: 1 1 auto;
  min-width: 0;
}

/* What the job IS — the discriminator when one customer has several jobs
   on the board, so it reads stronger than the muted meta lines. */
.job-title-line {
  color: var(--p-text-color);
  font-weight: 500;
}
/* The holding card is a wrapping flex row of chips; the title takes a full
   row of its own so it doesn't interleave with the tags. */
.holding-job-title { flex-basis: 100%; }

/* A dated job in this section is waiting on a TECH, not on a date — call
   that out rather than letting it read like the undated cards. */
.job-line-dated {
  color: var(--p-orange-500, #f59e0b);
  font-weight: 600;
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

/* Week board — techs × days */
.week-queue {
  display: flex;
  align-items: flex-start;
  gap: 0.35rem;
  margin-bottom: 0.5rem;
  padding: 0.4rem 0.5rem;
  border: 1px dashed var(--p-content-border-color);
  border-radius: var(--p-border-radius);
}
.week-queue-label {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.8rem;
  font-weight: 600;
  margin-right: 0.25rem;
}
.week-queue-chip { max-width: 16rem; }
.lane-strip .week-queue.drag-over {
  background: color-mix(in srgb, var(--p-primary-color) 18%, transparent);
  box-shadow: inset 0 0 0 2px var(--p-primary-color);
}
/* Holding areas sit side by side so every lane and the board stay on one
   screen: a drag can reach any of them without a scroll. */
.lane-areas {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 0.5rem;
}
.lane-area {
  flex: 1 1 15rem;
  min-width: 0;
  margin-bottom: 0;
  flex-direction: column;
  gap: 0.25rem;
}
.lane-area--alert { border-left: 3px solid var(--p-red-500); }
.lane-head {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  width: 100%;
  min-width: 0;
}
.lane-dot {
  width: 0.6rem;
  height: 0.6rem;
  border-radius: 50%;
  flex: none;
}
.lane-total {
  font-size: 0.7rem;
  color: var(--p-text-muted-color);
  white-space: nowrap;
}
.lane-head .lane-delete { margin-left: auto; }
.lane-area .week-queue-chips { width: 100%; }
.lane-chip { max-width: 100%; align-items: center; }
.lane-chip-title {
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
  color: var(--p-text-muted-color);
}
.lane-icon-button {
  flex: none;
  border: 0;
  background: transparent;
  color: var(--p-text-muted-color);
  padding: 0 0.2rem;
  font-size: 0.7rem;
  line-height: 1;
  cursor: pointer;
  border-radius: 3px;
}
.lane-icon-button:hover,
.lane-icon-button:focus-visible { color: var(--p-text-color); background: var(--p-content-hover-background); }
.lane-add { align-self: center; }
.scheduled-overdue .week-chip-meta { color: var(--p-red-500); font-weight: 600; }
.board-undo-bar {
  position: fixed;
  left: 50%;
  bottom: 1.25rem;
  transform: translateX(-50%);
  z-index: 1100;
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0.6rem 0.4rem 1rem;
  border-radius: var(--p-border-radius);
  background: var(--p-content-background);
  color: var(--p-text-color);
  border: 1px solid var(--p-content-border-color);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.2);
}
/* A long intake list scrolls inside two rows instead of pushing the grid
   down (scroll the strip first, then drag). */
.week-queue-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  flex: 1;
  min-width: 0;
  max-height: 3.4rem;
  overflow-y: auto;
}

.week-board-scroll {
  overflow-x: auto;
}
.week-board {
  display: grid;
  grid-template-columns: 8.5rem repeat(7, minmax(7.5rem, 1fr));
  min-width: 61rem;
  border-top: 1px solid var(--p-content-border-color);
  border-left: 1px solid var(--p-content-border-color);
}
.week-corner,
.week-day-head,
.week-tech,
.week-cell {
  border-right: 1px solid var(--p-content-border-color);
  border-bottom: 1px solid var(--p-content-border-color);
}
.week-corner,
.week-day-head {
  position: sticky;
  top: 0;
  z-index: 2;
  background: var(--p-content-background);
}
.week-corner {
  left: 0;
  z-index: 3;
  padding: 0.35rem 0.5rem;
  font-size: 0.75rem;
  font-weight: 700;
  text-transform: uppercase;
  color: var(--p-text-muted-color);
}
.week-day-head {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  padding: 0.35rem 0.5rem;
  font: inherit;
  font-size: 0.85rem;
  color: inherit;
  border-top: 0;
  border-left: 0;
  cursor: pointer;
  text-align: left;
}
.week-day-head:hover { background: var(--p-content-hover-background); }
.week-day-head.is-today { box-shadow: inset 0 -3px 0 var(--p-primary-color); }

.day-name {
  font-weight: 700;
  text-transform: uppercase;
  font-size: 0.75rem;
  color: var(--p-text-muted-color);
}
.day-date { font-weight: 600; }

.week-tech {
  position: sticky;
  left: 0;
  z-index: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  padding: 0.3rem 0.5rem;
  background: var(--p-content-background);
}
.week-tech-name {
  font-size: 0.85rem;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.week-tech-hours {
  font-size: 0.7rem;
  color: var(--p-text-muted-color);
}
.week-tech--unassigned .week-tech-name { color: var(--p-text-muted-color); font-style: italic; }

.week-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-height: 2.6rem;
  padding: 3px;
  transition: background 0.1s;
}
.week-cell.is-today { background: color-mix(in srgb, var(--p-primary-color) 6%, transparent); }
.week-cell.is-off.is-time-off { background: color-mix(in srgb, var(--p-orange-400, #fb923c) 12%, transparent); }
.week-time-off {
  display: block;
  padding: 0.1rem 0.35rem;
  border: 1px dashed var(--p-orange-400, #fb923c);
  border-radius: var(--p-border-radius);
  color: var(--p-text-color);
  font-size: 0.72rem;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.week-cell.is-off {
  background: repeating-linear-gradient(
    135deg, transparent, transparent 6px,
    color-mix(in srgb, var(--p-text-muted-color) 10%, transparent) 6px,
    color-mix(in srgb, var(--p-text-muted-color) 10%, transparent) 8px);
}
.week-cell.drag-over {
  background: color-mix(in srgb, var(--p-primary-color) 18%, transparent);
  box-shadow: inset 0 0 0 2px var(--p-primary-color);
}
.week-cell-note {
  margin-top: auto;
  font-size: 0.68rem;
  color: var(--p-text-muted-color);
  text-align: right;
}
.week-cell-note.is-over { color: var(--p-red-500, #ef4444); font-weight: 600; }

.week-chip {
  display: flex;
  align-items: baseline;
  gap: 0.3rem;
  padding: 1px 5px;
  font-size: 0.75rem;
  line-height: 1.35;
  border: 1px solid var(--p-content-border-color);
  border-left: 3px solid var(--p-primary-color);
  border-radius: 4px;
  background: var(--p-content-background);
  cursor: grab;
  overflow: hidden;
  white-space: nowrap;
}
.week-chip:hover { border-color: var(--p-primary-color); }
.week-chip--partial { border-left-color: var(--p-amber-500, #f59e0b); }
.week-chip--locked { cursor: pointer; opacity: 0.75; border-left-color: var(--p-text-muted-color); }
.week-chip-time { font-weight: 700; flex: none; }
.week-chip-customer { overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.week-chip-meta { flex: none; font-size: 0.68rem; color: var(--p-text-muted-color); }

/* Compact day view: narrower tech columns (hours shrink via pxPerHour). */
.tech-columns-grid.is-compact {
  grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
  gap: 0.5rem;
}
.tech-columns-grid.is-compact :deep(.p-card-body) { padding: 0.6rem; gap: 0.4rem; }

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
  .tech-columns-grid {
    grid-template-columns: 1fr;
  }

  .unassigned-grid {
    grid-template-columns: 1fr;
  }
}

/* On a phone the lanes stacked one per row run ~850px before the first tech;
   one row that swipes sideways keeps the board in reach. */
@media (max-width: 640px) {
  .lane-areas {
    flex-wrap: nowrap;
    overflow-x: auto;
    scroll-snap-type: x proximity;
    padding-bottom: 0.25rem;
  }
  .lane-area {
    flex: 0 0 80%;
    scroll-snap-align: start;
  }
}

@media (max-width: 480px) {
  .dispatch-toolbar {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
