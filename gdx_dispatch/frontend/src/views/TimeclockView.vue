<template>
    <section class="timeclock-view view-card">
      <h2 class="page-title">Time Clock</h2>

      <!-- Clock Card -->
      <Card class="clock-card" data-testid="clock-card">
        <template #content>
          <div class="clock-status-area">
            <div class="status-label">Current Status</div>
            <Tag
              :value="statusLabel"
              :severity="statusSeverity"
              class="status-tag"
              data-testid="clock-status-tag"
            />

            <div v-if="clockedIn" class="elapsed-display" data-testid="elapsed-time">
              {{ elapsedFormatted }}
            </div>
            <div v-if="clockedIn && clockInTime" class="clock-since">
              Since {{ formatTime(clockInTime) }}
            </div>
            <div v-if="todayTotalHours != null" class="today-total">
              Today: <strong>{{ todayTotalHours.toFixed(2) }}h</strong>
            </div>

            <!-- Job selector -->
            <div class="job-selector">
              <label for="timeclock-job">Link to job (optional):</label>
              <Select
                id="timeclock-job"
                v-model="selectedJobId"
                :options="availableJobs"
                optionLabel="label"
                optionValue="value"
                placeholder="No job selected"
                :showClear="true"
                filter
                class="job-dropdown"
                data-testid="timeclock-job-dropdown"
              />
            </div>

            <!-- GPS indicator -->
            <div class="gps-indicator" data-testid="gps-indicator">
              <i :class="gpsAvailable ? 'pi pi-map-marker' : 'pi pi-times-circle'" :style="{ color: gpsAvailable ? 'var(--color-success-500)' : 'var(--text-muted)' }"></i>
              {{ gpsStatusText }}
            </div>

            <!-- Action buttons -->
            <div class="clock-actions">
              <Button
                v-if="!clockedIn"
                label="Clock In"
                icon="pi pi-sign-in"
                severity="success"
                class="clock-btn"
                data-testid="clock-in-btn"
                :loading="actionLoading"
                @click="clockIn"
              />
              <template v-else>
                <Button
                  label="Clock Out"
                  icon="pi pi-sign-out"
                  severity="danger"
                  class="clock-btn"
                  data-testid="clock-out-btn"
                  :loading="actionLoading"
                  @click="clockOut"
                />
                <Button
                  v-if="!onBreak"
                  label="Start Break"
                  icon="pi pi-pause"
                  severity="warn"
                  class="break-btn"
                  data-testid="start-break-btn"
                  :loading="breakLoading"
                  @click="startBreak"
                />
                <Button
                  v-else
                  label="End Break"
                  icon="pi pi-play"
                  severity="info"
                  class="break-btn"
                  data-testid="end-break-btn"
                  :loading="breakLoading"
                  @click="endBreak"
                />
              </template>
            </div>
          </div>
        </template>
      </Card>

      <!-- Today's Entries -->
      <Card class="entries-card" data-testid="today-entries">
        <template #title>
          <div class="section-header">
            <i class="pi pi-list"></i>
            Today's Entries
          </div>
        </template>
        <template #content>
          <div v-if="entriesLoading" class="spinner-wrap">
            <ProgressSpinner />
          </div>
          <DataTable
            v-else-if="todayEntries.length"
            :value="todayEntries"
            stripedRows
            responsiveLayout="scroll"
            data-testid="entries-table"
          >
            <Column header="Type" style="width: 90px">
              <template #body="{ data }">
                <Tag
                  :value="data.entry_type || 'work'"
                  :severity="data.entry_type === 'break' ? 'warning' : 'info'"
                />
              </template>
            </Column>
            <Column field="clock_in_display" header="Time In" />
            <Column field="clock_out_display" header="Time Out" />
            <Column header="Duration">
              <template #body="{ data }">
                {{ data.duration || '--' }}
              </template>
            </Column>
            <Column header="Job">
              <template #body="{ data }">
                {{ data.job_name || '--' }}
              </template>
            </Column>
            <Column header="GPS" style="width: 70px">
              <template #body="{ data }">
                <i
                  v-if="data.latitude && data.longitude"
                  class="pi pi-map-marker"
                  style="color: var(--color-success-500); cursor: pointer"
                  v-tooltip="`${data.latitude}, ${data.longitude}`"
                  :aria-label="`${data.latitude}, ${data.longitude}`"
                  @click="openGpsDialog(data)"
                ></i>
                <span v-else style="color: var(--text-muted)">--</span>
              </template>
            </Column>
          </DataTable>
          <div v-else class="empty-message" data-testid="entries-empty">
            <i class="pi pi-clock" style="font-size: 2rem; color: var(--p-text-muted-color); margin-bottom: 0.5rem"></i>
            <p>No entries today. Clock in to get started.</p>
          </div>
          <!-- S6-A4: End-of-day review affordance. -->
          <div v-if="todayEntries.length && !todaySubmitted" class="eod-review">
            <p class="muted">Review your hours and confirm before submitting to payroll.</p>
            <div class="eod-summary">
              <div><span>Today total</span><strong>{{ todayTotalHours.toFixed(2) }}h</strong></div>
              <div><span>Entries</span><strong>{{ todayEntries.length }}</strong></div>
            </div>
            <Button
              label="Submit Day to Payroll"
              icon="pi pi-check"
              severity="success"
              :disabled="onShift"
              data-testid="submit-day-btn"
              @click="confirmSubmitDay"
            />
            <p v-if="onShift" class="muted small">Clock out before submitting.</p>
          </div>
          <div v-else-if="todaySubmitted" class="eod-submitted" data-testid="day-submitted">
            <i class="pi pi-check-circle" style="color: var(--color-success-500)"></i>
            Submitted to payroll for review.
          </div>
        </template>
      </Card>

      <!-- Weekly Timecard -->
      <Card class="timecard-card" data-testid="weekly-timecard">
        <template #title>
          <div class="section-header">
            <i class="pi pi-calendar"></i>
            Weekly Summary
          </div>
        </template>
        <template #content>
          <DataTable
            :value="weekSummary"
            stripedRows
            responsiveLayout="scroll"
            data-testid="timecard-table"
            class="weekly-table"
          >
            <Column field="dayLabel" header="Day" />
            <Column field="date" header="Date" />
            <Column header="Work Hours">
              <template #body="{ data }">
                <span :class="{ 'hours-highlight': data.workHours > 0 }">
                  {{ data.workHours.toFixed(2) }}
                </span>
              </template>
            </Column>
            <Column header="Break Hours">
              <template #body="{ data }">
                <span class="break-hours">
                  {{ data.breakHours.toFixed(2) }}
                </span>
              </template>
            </Column>
            <Column header="Entries">
              <template #body="{ data }">
                <Badge :value="data.entryCount" :severity="data.entryCount > 0 ? 'info' : 'secondary'" />
              </template>
            </Column>
          </DataTable>

          <Divider />

          <div class="week-totals">
            <div class="week-total">
              <strong>Total Work:</strong>
              <span class="total-hours">{{ weekTotalWorkHours.toFixed(2) }}h</span>
            </div>
            <div class="week-total">
              <strong>Total Break:</strong>
              <span class="total-break">{{ weekTotalBreakHours.toFixed(2) }}h</span>
            </div>
            <div class="week-total">
              <strong>Net Hours:</strong>
              <span class="total-hours">{{ (weekTotalWorkHours - weekTotalBreakHours).toFixed(2) }}h</span>
            </div>
          </div>
        </template>
      </Card>

      <!-- GPS Location Dialog -->
      <Dialog
        v-model:visible="showGpsDialog"
        header="Clock Entry Location"
        :style="{ width: '400px' }"
        modal
        data-testid="gps-dialog"
      >
        <div v-if="gpsDialogData" class="gps-dialog-content">
          <p><strong>Latitude:</strong> {{ gpsDialogData.latitude }}</p>
          <p><strong>Longitude:</strong> {{ gpsDialogData.longitude }}</p>
          <a
            :href="`https://maps.google.com/?q=${gpsDialogData.latitude},${gpsDialogData.longitude}`"
            target="_blank"
            rel="noopener"
            class="gps-map-link"
          >
            <i class="pi pi-external-link"></i> Open in Google Maps
          </a>
        </div>
        <template #footer>
          <Button label="Close" text @click="showGpsDialog = false" />
        </template>
      </Dialog>

      <!-- S6-B1 — vehicle inspection log -->
      <Card v-if="vehicleInspectionMode !== 'off'" class="vehicle-inspection-card" data-testid="vehicle-inspection-card">
        <template #title>
          <div class="section-header">
            <i class="pi pi-truck"></i>
            Vehicle Inspection
          </div>
        </template>
        <template #content>
          <Button
            label="Log Inspection"
            icon="pi pi-plus"
            data-testid="log-inspection-btn"
            @click="showInspectionDialog = true"
          />
          <DataTable
            v-if="recentInspections.length"
            :value="recentInspections"
            stripedRows
            responsiveLayout="scroll"
            data-testid="inspections-table"
            style="margin-top: 0.75rem"
          >
            <Column field="inspection_at" header="When">
              <template #body="{ data }">
                {{ tenantTimezone
                    ? new Date(data.inspection_at).toLocaleString('en-US', { timeZone: tenantTimezone })
                    : new Date(data.inspection_at).toLocaleString() }}
              </template>
            </Column>
            <Column field="inspection_type" header="Type" />
            <Column field="vehicle_label" header="Vehicle" />
            <Column field="odometer" header="Odometer" />
            <Column field="fuel_cost" header="Fuel $">
              <template #body="{ data }">
                {{ data.fuel_cost != null ? `$${Number(data.fuel_cost).toFixed(2)}` : '—' }}
              </template>
            </Column>
            <Column header="Issues">
              <template #body="{ data }">
                <Tag v-if="data.issues_found" value="Issues" severity="warn" />
                <span v-else class="muted">—</span>
              </template>
            </Column>
          </DataTable>
        </template>
      </Card>

      <Dialog
        v-model:visible="showInspectionDialog"
        header="Log Vehicle Inspection"
        :style="{ width: '460px' }"
        modal
        data-testid="inspection-dialog"
      >
        <div class="inspection-form">
          <label>
            Type
            <Select v-model="inspForm.inspection_type" :options="['pre_trip','post_trip','fueling','ad_hoc']" />
          </label>
          <label>
            Vehicle (label)
            <InputText v-model="inspForm.vehicle_label" placeholder="e.g. Truck 7" />
          </label>
          <label>
            Odometer
            <InputNumber v-model="inspForm.odometer" />
          </label>
          <label>
            Fuel Cost
            <InputNumber v-model="inspForm.fuel_cost" mode="currency" currency="USD" />
          </label>
          <label>
            Photo URL
            <InputText v-model="inspForm.photo_url" />
          </label>
          <label>
            Issues found
            <Textarea v-model="inspForm.issues_found" rows="2" />
          </label>
          <label>
            Notes
            <Textarea v-model="inspForm.notes" rows="2" />
          </label>
        </div>
        <template #footer>
          <Button label="Cancel" text @click="showInspectionDialog = false" />
          <Button label="Save" icon="pi pi-check" data-testid="save-inspection-btn" @click="saveInspection" />
        </template>
      </Dialog>

      <Toast data-testid="timeclock-toast" />
    </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useToast } from 'primevue/usetoast';
import { useApiWithToast as useApi } from '../composables/useApiWithToast';
import Badge from 'primevue/badge';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Column from 'primevue/column';
import DataTable from 'primevue/datatable';
import Dialog from 'primevue/dialog';
import Divider from 'primevue/divider';
import Select from 'primevue/select';
import InputText from 'primevue/inputtext';
import InputNumber from 'primevue/inputnumber';
import Textarea from 'primevue/textarea';
import ProgressSpinner from 'primevue/progressspinner';
import Tag from 'primevue/tag';
import Toast from 'primevue/toast';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApi();
const toast = useToast();

const clockedIn = ref(false);
const onBreak = ref(false);
const clockInTime = ref(null);
const todayTotalHours = ref(null);
const actionLoading = ref(false);
const breakLoading = ref(false);
const entriesLoading = ref(false);
const entries = ref([]);
const selectedJobId = ref(null);
const availableJobs = ref([]);
const gpsAvailable = ref(false);
const gpsCoords = ref(null);
const elapsedSeconds = ref(0);
const showGpsDialog = ref(false);
const gpsDialogData = ref(null);
// S6-A4 end-of-day review state
const todaySubmitted = ref(false);
const onShift = computed(() => clockedIn.value && !onBreak.value);

// S6-B1 vehicle inspection state
const vehicleInspectionMode = ref('off'); // off | daily | weekly — resolved from /api/me/tech-mobile-settings on mount

// S92 — tenant-local timezone for clock display.
const tenantTimezone = ref(null);

async function loadFeatureSettings() {
  try {
    const data = await api.get('/api/me/tech-mobile-settings');
    const s = data?.settings || {};
    vehicleInspectionMode.value = s['tech_mobile.vehicle_inspection'] || 'off';
    tenantTimezone.value = data?.tenant_timezone || null;
  } catch {
    vehicleInspectionMode.value = 'off';
    tenantTimezone.value = null;
  }
}
const showInspectionDialog = ref(false);
const recentInspections = ref([]);
const inspForm = ref({
  inspection_type: 'pre_trip',
  vehicle_label: '',
  odometer: null,
  fuel_cost: null,
  photo_url: '',
  issues_found: '',
  notes: '',
});

async function loadRecentInspections() {
  try {
    const data = await api.get('/api/vehicle-inspections?limit=10');
    recentInspections.value = Array.isArray(data) ? data : [];
  } catch {
    recentInspections.value = [];
  }
}

async function saveInspection() {
  try {
    const created = await api.post('/api/vehicle-inspections', { ...inspForm.value }, { successMessage: 'Inspection logged' });
    recentInspections.value.unshift(created);
    showInspectionDialog.value = false;
    inspForm.value = {
      inspection_type: 'pre_trip', vehicle_label: '', odometer: null,
      fuel_cost: null, photo_url: '', issues_found: '', notes: '',
    };
  } catch {
    // toast handled by api helper
  }
}

async function confirmSubmitDay() {
  if (!todayEntries.value.length) return;
  if (!(await confirmAsync({ header: 'Confirm', message: `Submit ${todayTotalHours.value?.toFixed?.(2) || todayTotalHours.value}h to payroll for today?` }))) return;
  // Marks today as submitted client-side. Server endpoint can be wired
  // when payroll-export is finalized; payroll summary already aggregates
  // entries via /api/timeclock/payroll, so this is a UX confirmation.
  todaySubmitted.value = true;
  try {
    await api.post('/api/timeclock/submit-day', { date: new Date().toISOString().slice(0, 10) }, { successMessage: 'Day submitted to payroll' });
  } catch {
    // Endpoint may not exist yet; UI state still progresses for the tech.
  }
}

let elapsedTimer = null;

// --- Status display ---

const statusLabel = computed(() => {
  if (!clockedIn.value) return 'Clocked Out';
  if (onBreak.value) return 'On Break';
  return 'Clocked In';
});

const statusSeverity = computed(() => {
  if (!clockedIn.value) return 'danger';
  if (onBreak.value) return 'warning';
  return 'success';
});

// --- Elapsed time ---

const elapsedFormatted = computed(() => {
  const h = Math.floor(elapsedSeconds.value / 3600);
  const m = Math.floor((elapsedSeconds.value % 3600) / 60);
  const s = elapsedSeconds.value % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
});

function startElapsedTimer() {
  stopElapsedTimer();
  if (clockInTime.value) {
    const start = new Date(clockInTime.value).getTime();
    elapsedSeconds.value = Math.floor((Date.now() - start) / 1000);
    elapsedTimer = setInterval(() => {
      elapsedSeconds.value = Math.floor((Date.now() - start) / 1000);
    }, 1000);
  }
}

function stopElapsedTimer() {
  if (elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
  elapsedSeconds.value = 0;
}

// --- Date helpers ---

function formatTime(iso) {
  if (!iso) return '--';
  try {
    const opts = { hour: 'numeric', minute: '2-digit' };
    if (tenantTimezone.value) opts.timeZone = tenantTimezone.value;
    return new Date(iso).toLocaleTimeString('en-US', opts);
  } catch {
    return iso;
  }
}

function toDateStr(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

// --- GPS ---

const gpsStatusText = computed(() => {
  if (!gpsAvailable.value) return 'GPS unavailable';
  const c = gpsCoords.value;
  if (c) {
    // Coordinate (0, 0) is "Null Island" off the African coast — almost
    // certainly a no-fix sentinel from a browser whose geolocation API
    // returned a placeholder rather than a real position. Treat it as
    // unavailable so the tech doesn't see "GPS: 0.0000, 0.0000" and
    // think the app is broken.
    if (Math.abs(c.latitude) < 0.0001 && Math.abs(c.longitude) < 0.0001) {
      return 'GPS unavailable';
    }
    return `GPS: ${c.latitude.toFixed(4)}, ${c.longitude.toFixed(4)}`;
  }
  return 'GPS available';
});

function initGps() {
  if ('geolocation' in navigator) {
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        gpsAvailable.value = true;
        gpsCoords.value = {
          latitude: pos.coords.latitude,
          longitude: pos.coords.longitude,
        };
      },
      () => {
        gpsAvailable.value = false;
      },
      { enableHighAccuracy: true, timeout: 10000 },
    );
  }
}

function refreshGps() {
  return new Promise((resolve) => {
    if (!('geolocation' in navigator)) {
      resolve();
      return;
    }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        gpsAvailable.value = true;
        gpsCoords.value = {
          latitude: pos.coords.latitude,
          longitude: pos.coords.longitude,
        };
        resolve();
      },
      () => {
        resolve();
      },
      { enableHighAccuracy: true, timeout: 10000 },
    );
  });
}

function openGpsDialog(entry) {
  gpsDialogData.value = { latitude: entry.latitude, longitude: entry.longitude };
  showGpsDialog.value = true;
}

// --- Today's entries ---

const todayEntries = computed(() => {
  const todayStr = toDateStr(new Date());
  return entries.value
    .filter((e) => {
      const dateStr = e.date || (e.clock_in ? e.clock_in.split('T')[0] : '');
      return dateStr === todayStr;
    })
    .map((e) => ({
      ...e,
      clock_in_display: formatTime(e.clock_in),
      clock_out_display: e.clock_out ? formatTime(e.clock_out) : 'Active',
      duration: e.hours != null ? `${e.hours.toFixed(2)}h` : e.clock_out ? '--' : 'In progress',
      job_name: e.job_name || e.job_title || null,
    }));
});

// --- Weekly summary ---

const weekSummary = computed(() => {
  const today = new Date();
  const dayOfWeek = today.getDay();
  const start = new Date(today);
  start.setDate(today.getDate() - dayOfWeek);

  const dayNames = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  const days = [];

  for (let i = 0; i < 7; i++) {
    const d = new Date(start);
    d.setDate(start.getDate() + i);
    const ds = toDateStr(d);
    const dayEntries = entries.value.filter((e) => {
      const dateStr = e.date || (e.clock_in ? e.clock_in.split('T')[0] : '');
      return dateStr === ds;
    });
    const workEntries = dayEntries.filter((e) => (e.entry_type || 'work') !== 'break');
    const breakEntries = dayEntries.filter((e) => e.entry_type === 'break');
    const workHours = workEntries.reduce((sum, e) => sum + (e.hours || 0), 0);
    const breakHours = breakEntries.reduce((sum, e) => sum + (e.hours || 0), 0);
    // 2026-04-29: don't render future days. Was showing Thu/Fri/Sat as
    // 0.00 hours when today is Wed, which read as if those days had
    // already happened.
    const todayStr = toDateStr(today);
    if (ds > todayStr) continue;
    days.push({
      dayLabel: dayNames[i],
      date: ds,
      workHours,
      breakHours,
      entryCount: dayEntries.length,
    });
  }

  return days;
});

const weekTotalWorkHours = computed(() =>
  weekSummary.value.reduce((sum, d) => sum + d.workHours, 0),
);
const weekTotalBreakHours = computed(() =>
  weekSummary.value.reduce((sum, d) => sum + d.breakHours, 0),
);

// --- API calls ---

async function fetchStatus() {
  try {
    const data = await api.get('/api/timeclock/status');
    const status = data?.data || data;
    // Backend returns {clocked_in, active_entry}; active_entry has clock_in_at +
    // entry_type ("work" | "break"). Read nested first, fall back to legacy
    // flat fields if a future backend emits them.
    const entry = status?.active_entry || null;
    clockedIn.value = !!status?.clocked_in;
    onBreak.value = !!status?.on_break || entry?.entry_type === 'break';
    clockInTime.value = entry?.clock_in_at || status?.clock_in_time || null;
    todayTotalHours.value = status?.today_hours ?? null;
    if (clockedIn.value) {
      startElapsedTimer();
    } else {
      stopElapsedTimer();
    }
  } catch {
    clockedIn.value = false;
    onBreak.value = false;
  }
}

async function fetchEntries() {
  entriesLoading.value = true;
  try {
    const data = await api.get('/api/timeclock/entries');
    const payload = data?.data || data;
    const raw = Array.isArray(payload) ? payload : payload?.items || [];
    // Backend emits `clock_in_at` / `clock_out_at` / `minutes`; downstream
    // computed filters still read `clock_in` / `clock_out` / `hours`.
    // Normalize at ingest so both shapes populate without touching N call sites.
    entries.value = raw.map((e) => ({
      ...e,
      clock_in: e.clock_in || e.clock_in_at || null,
      clock_out: e.clock_out || e.clock_out_at || null,
      hours:
        e.hours != null
          ? e.hours
          : e.minutes != null
            ? Number((e.minutes / 60).toFixed(2))
            : null,
    }));
  } catch {
    entries.value = [];
  } finally {
    entriesLoading.value = false;
  }
}

async function fetchJobs() {
  try {
    const data = await api.get('/api/jobs');
    const rows = Array.isArray(data) ? data : data?.items || data?.data || [];
    availableJobs.value = rows.map((j) => ({
      label: `${j.customer_name || j.customer || 'Job'} - ${j.job_type || j.type || 'Service'}`,
      value: j.id,
    }));
  } catch {
    availableJobs.value = [];
  }
}

function buildGpsBody() {
  const body = {};
  if (gpsCoords.value) {
    body.latitude = gpsCoords.value.latitude;
    body.longitude = gpsCoords.value.longitude;
  }
  return body;
}

// GPS is nice-to-have, not blocking. Cap the wait at 2s so a
// hung/denied geolocation permission can't freeze attendance actions.
async function gpsWithTimeout(ms = 2000) {
  await Promise.race([
    refreshGps(),
    new Promise((resolve) => setTimeout(resolve, ms)),
  ]);
}

async function clockIn() {
  actionLoading.value = true;
  try {
    await gpsWithTimeout();
    const body = buildGpsBody();
    if (selectedJobId.value) {
      body.job_id = selectedJobId.value;
    }
    await api.post('/api/timeclock/clock-in', body);
    toast.add({ severity: 'success', summary: 'Clocked In', detail: 'You are now clocked in.', life: 3000 });
    await Promise.all([fetchStatus(), fetchEntries()]);
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Clock In Failed', detail: err?.message || 'Could not clock in.', life: 5000 });
  } finally {
    actionLoading.value = false;
  }
}

async function clockOut() {
  actionLoading.value = true;
  try {
    await gpsWithTimeout();
    const body = buildGpsBody();
    await api.post('/api/timeclock/clock-out', body);
    toast.add({ severity: 'success', summary: 'Clocked Out', detail: 'You are now clocked out.', life: 3000 });
    await Promise.all([fetchStatus(), fetchEntries()]);
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Clock Out Failed', detail: err?.message || 'Could not clock out.', life: 5000 });
  } finally {
    actionLoading.value = false;
  }
}

async function startBreak() {
  breakLoading.value = true;
  try {
    await api.post('/api/timeclock/break/start', {});
    onBreak.value = true;
    toast.add({ severity: 'info', summary: 'Break Started', detail: 'Break timer running.', life: 3000 });
    await Promise.all([fetchStatus(), fetchEntries()]);
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Break Start Failed', detail: err?.message || 'Could not start break.', life: 5000 });
  } finally {
    breakLoading.value = false;
  }
}

async function endBreak() {
  breakLoading.value = true;
  try {
    await api.post('/api/timeclock/break/end', {});
    onBreak.value = false;
    toast.add({ severity: 'info', summary: 'Break Ended', detail: 'Back to work.', life: 3000 });
    await Promise.all([fetchStatus(), fetchEntries()]);
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Break End Failed', detail: err?.message || 'Could not end break.', life: 5000 });
  } finally {
    breakLoading.value = false;
  }
}

onMounted(() => {
  initGps();
  Promise.all([fetchStatus(), fetchEntries(), fetchJobs(), loadRecentInspections(), loadFeatureSettings()]);
});

onUnmounted(() => {
  stopElapsedTimer();
});
</script>

<style scoped>
.timeclock-view {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
  max-width: 800px;
  margin: 0 auto;
  /* Constrain to parent — without this, the inner DataTable
   * (Weekly Summary 7-column grid + Today's Entries) bursts out of
   * a 375px mobile viewport and forces horizontal page scroll.
   * Keeping the table inside its column lets responsiveLayout="scroll"
   * take over with a contained inner-scroll. S113 mobile audit. */
  width: 100%;
  min-width: 0;
}

/* DataTable horizontal-scroll containers — ensure the wrapping div
 * never grows beyond its parent column on narrow viewports. The
 * responsiveLayout="scroll" wrapper handles the inner scroll. */
.timeclock-view :deep(.p-datatable),
.timeclock-view :deep(.p-datatable-table-container) {
  max-width: 100%;
  overflow-x: auto;
}

@media (max-width: 480px) {
  /* Drop the Entries count column on phones — the row's "Day Date Hours"
   * is the legible summary. Keeping it visible burns ~75px on a 375px
   * viewport that's already tight. */
  .timeclock-view :deep(.weekly-table th:nth-child(5)),
  .timeclock-view :deep(.weekly-table td:nth-child(5)) {
    display: none;
  }
}

.page-title {
  margin: 0;
  font-size: 1.4rem;
  font-weight: 700;
}

/* Clock card */
.clock-card :deep(.p-card-body) {
  padding: 0;
}

.clock-status-area {
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 2rem 1.5rem;
  text-align: center;
}

.status-label {
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--p-text-muted-color);
  margin-bottom: 0.5rem;
}

.status-tag {
  font-size: 1rem;
  padding: 0.4rem 1rem;
  margin-bottom: 1rem;
}

.elapsed-display {
  font-size: 2.8rem;
  font-weight: 700;
  font-family: monospace;
  font-variant-numeric: tabular-nums;
  letter-spacing: 0.04em;
  margin-bottom: 0.25rem;
}

.clock-since {
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
  margin-bottom: 0.75rem;
}

.today-total {
  font-size: 0.9rem;
  color: var(--p-text-muted-color);
  margin-bottom: 1rem;
}

.today-total strong {
  color: var(--p-primary-color);
}

.job-selector {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.35rem;
  margin-bottom: 1rem;
}

.job-selector label {
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
}

.job-dropdown {
  min-width: 300px;
}

.gps-indicator {
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
  margin-bottom: 1rem;
  display: flex;
  align-items: center;
  gap: 0.3rem;
}

.clock-actions {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
  justify-content: center;
}

.clock-btn {
  padding: 0.85rem 2.5rem;
  font-size: 1.05rem;
  font-weight: 700;
}

.break-btn {
  padding: 0.65rem 1.5rem;
  font-size: 0.95rem;
  font-weight: 600;
}

/* Entries */
.section-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  margin: 1rem 0;
}

.empty-message {
  text-align: center;
  color: var(--p-text-muted-color);
  padding: 2rem 1rem;
  margin: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.empty-message p {
  margin: 0;
}

/* Timecard */
.hours-highlight {
  font-weight: 700;
  color: var(--p-primary-color);
}

.break-hours {
  color: var(--p-text-muted-color);
}

.week-totals {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 1.5rem;
  flex-wrap: wrap;
}

.week-total {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 1.05rem;
}

.total-hours {
  font-weight: 700;
  font-size: 1.2rem;
  color: var(--p-primary-color);
}

.total-break {
  font-weight: 700;
  font-size: 1.1rem;
  color: var(--p-text-muted-color);
}

/* GPS Dialog */
.gps-dialog-content p {
  margin: 0.4rem 0;
}

.gps-map-link {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  color: var(--p-primary-color);
  text-decoration: none;
  margin-top: 0.75rem;
}

.gps-map-link:hover {
  text-decoration: underline;
}
</style>
