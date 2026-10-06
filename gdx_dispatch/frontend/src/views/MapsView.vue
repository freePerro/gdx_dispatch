<template>
    <section class="maps-view view-card" data-testid="maps-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title" data-testid="maps-title">Maps</h2>
        </template>
      </Toolbar>

      <!-- Google Map -->
      <div ref="mapContainer" class="google-map-container" data-testid="google-map"
        style="height: 400px; width: 100%; border-radius: 8px; margin-bottom: 1rem; background: var(--surface-ground);">
        <div v-if="!mapReady" style="display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;color:var(--p-text-muted-color);text-align:center;padding:1rem">
          <i class="pi pi-map" style="font-size:2rem;margin-bottom:0.5rem;opacity:0.4"></i>
          <span v-if="!mapsKeyConfigured">Map view unavailable — Google Maps API key not configured. An admin can paste one in Settings → Integrations. Technician table below still works.</span>
          <span v-else>Loading map...</span>
        </div>
      </div>
      <div class="maps-filters" data-testid="maps-tech-filters">
        <InputText
          v-model="techFilter"
          placeholder="Filter by technician"
          class="w-full"
          data-testid="maps-tech-filter"
        />
        <Button
          label="Refresh"
          icon="pi pi-refresh"
          @click="loadMaps"
          :loading="loading"
          data-testid="maps-refresh"
        />
      </div>
      <div class="last-refresh" data-testid="maps-last-refresh">
        <span class="muted">Last refreshed:</span>
        <strong>{{ lastRefreshLabel }}</strong>
      </div>
      <div v-if="loading" class="spinner-wrap" data-testid="maps-loading">
        <ProgressSpinner />
      </div>
      <p v-else-if="loadError" class="maps-error" data-testid="maps-load-error">
        Could not load technician locations. Try Refresh.
      </p>
      <DataTable
        v-else
        :value="filteredTechLocations"
        striped-rows
        responsiveLayout="scroll"
        data-testid="maps-tech-table"
      >
        <template #empty>
          <span v-if="techLocations.length" data-testid="maps-tech-no-match">
            No technician matches "{{ techFilter }}".
          </span>
          <span v-else data-testid="maps-tech-empty">
            No technician has reported a location in the last {{ WINDOW_MINUTES }} minutes.
            A tech's phone sends its location only while they are clocked in, have the
            mobile Today screen open, have allowed location access, and the tech mobile
            setting "Background GPS breadcrumb" is on.
          </span>
        </template>
        <Column field="tech_name" header="Technician" />
        <Column header="Lat / Lng">
          <template #body="{ data }">{{ formatCoordinate(data.lat) }}, {{ formatCoordinate(data.lng) }}</template>
        </Column>
        <Column header="Accuracy">
          <template #body="{ data }">{{ data.accuracy_m != null ? `±${Math.round(data.accuracy_m)} m` : '—' }}</template>
        </Column>
        <Column header="Updated">
          <template #body="{ data }">{{ formatTimestamp(data.recorded_at) }}</template>
        </Column>
      </DataTable>
    </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatDateTime as formatTimestamp } from "../composables/useFormatters";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Toolbar from "primevue/toolbar";

// Same window as the Dispatch board's Live Techs card.
const WINDOW_MINUTES = 30;

const api = useApiWithToast();

const techLocations = ref([]);
const loading = ref(true);
const loadError = ref(false);
const mapContainer = ref(null);
const mapReady = ref(false);
// Per-tenant Google Maps key, fetched from /api/settings/integrations/google-maps
// at mount. Reactive so the "not configured" message disappears the moment
// an admin saves a key in Settings → Integrations and revisits this page.
const mapsApiKey = ref('');
const mapsKeyConfigured = computed(() => Boolean(mapsApiKey.value));
let googleMap = null;
let mapMarkers = [];
let mapInfoWindow = null;
const techFilter = ref("");
const lastRefresh = ref(null);

const filteredTechLocations = computed(() => {
  const filterText = techFilter.value.toLowerCase().trim();
  if (!filterText) return techLocations.value;
  return techLocations.value.filter((item) => (item.tech_name ?? "").toLowerCase().includes(filterText));
});

const lastRefreshLabel = computed(() => {
  if (!lastRefresh.value) return "—";
  return new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(lastRefresh.value);
});

function formatCoordinate(value) {
  if (value === null || value === undefined) return "—";
  return Number(value).toFixed(5);
}

// A breadcrumb carries technician_id (Technician.id) when the poster had an
// active technician row, and always carries user_id. Resolve a name from
// either; fall back to the raw id so a row is never anonymous.
async function fetchTechnicianNames() {
  try {
    const data = await api.get("/api/technicians", { suppressErrorToast: true });
    const rows = Array.isArray(data) ? data : data?.items || data?.data || [];
    const byId = new Map();
    for (const t of rows) {
      const name = t.name || t.user_id || `Tech ${t.id}`;
      if (t.id != null) byId.set(String(t.id), name);
      if (t.user_id) byId.set(String(t.user_id), name);
    }
    return byId;
  } catch (_err) {
    return new Map();
  }
}

async function loadMaps() {
  loading.value = true;
  loadError.value = false;
  try {
    const [data, names] = await Promise.all([
      api.get(`/api/dispatch/locations?minutes=${WINDOW_MINUTES}`),
      fetchTechnicianNames(),
    ]);
    const rows = Array.isArray(data) ? data : [];
    techLocations.value = rows.map((r) => ({
      ...r,
      tech_name:
        (r.technician_id && names.get(String(r.technician_id)))
        || names.get(String(r.user_id))
        || r.technician_id
        || r.user_id,
    }));
    lastRefresh.value = new Date();
    updateMapMarkers();
  } catch (_err) {
    // The toast already said what failed; don't leave stale rows looking live.
    techLocations.value = [];
    loadError.value = true;
    updateMapMarkers();
  } finally {
    loading.value = false;
  }
}

function initGoogleMap() {
  if (!mapContainer.value || !window.google?.maps) return;
  googleMap = new window.google.maps.Map(mapContainer.value, {
    center: { lat: 46.8738, lng: -96.7678 },
    zoom: 10,
    mapTypeControl: true,
    streetViewControl: false,
  });
  mapInfoWindow = new window.google.maps.InfoWindow();
  mapReady.value = true;
  updateMapMarkers();
}

function updateMapMarkers() {
  if (!googleMap) return;
  mapMarkers.forEach(m => m.setMap(null));
  mapMarkers = [];
  for (const t of techLocations.value) {
    if (t.lat == null || t.lng == null) continue;
    const marker = new window.google.maps.Marker({
      position: { lat: Number(t.lat), lng: Number(t.lng) },
      map: googleMap,
      title: t.tech_name || 'Technician',
      icon: { path: window.google.maps.SymbolPath.CIRCLE, scale: 10, fillColor: '#3b82f6', fillOpacity: 1, strokeColor: '#fff', strokeWeight: 2 },
    });
    marker.addListener('click', () => {
      // Text nodes, not an HTML string: the name is user-entered data.
      const box = document.createElement('div');
      box.style.padding = '4px';
      const name = document.createElement('strong');
      name.textContent = t.tech_name || 'Technician';
      box.appendChild(name);
      box.appendChild(document.createElement('br'));
      box.appendChild(document.createTextNode(`Updated: ${formatTimestamp(t.recorded_at)}`));
      mapInfoWindow.setContent(box);
      mapInfoWindow.open(googleMap, marker);
    });
    mapMarkers.push(marker);
  }
}

function loadGoogleMapsScript() {
  if (window.google?.maps) { initGoogleMap(); return; }
  // Tenant-scoped key — fetched at mount via fetchMapsKey(). When absent,
  // we render the table-only view rather than injecting a script with a
  // placeholder/empty key (the previous behavior left a silently broken
  // map). Admins can paste a key in Settings → Integrations.
  const key = mapsApiKey.value;
  if (!key) {
    mapReady.value = false;
    return;
  }
  const s = document.createElement('script');
  s.src = `https://maps.googleapis.com/maps/api/js?key=${key}`;
  s.async = true;
  s.onload = initGoogleMap;
  document.head.appendChild(s);
}

async function fetchMapsKey() {
  try {
    const r = await api.get('/api/settings/integrations/google-maps');
    mapsApiKey.value = r.key || '';
  } catch (_err) {
    mapsApiKey.value = '';
  }
}

onMounted(async () => {
  await fetchMapsKey();
  loadMaps();
  loadGoogleMapsScript();
});

onBeforeUnmount(() => {
  try {
    mapMarkers.forEach((m) => { try { m.setMap(null); } catch (_) {} });
    mapMarkers = [];
    if (mapInfoWindow) { try { mapInfoWindow.close(); } catch (_) {} }
    mapInfoWindow = null;
    googleMap = null;
    if (mapContainer.value) {
      while (mapContainer.value.firstChild) {
        mapContainer.value.removeChild(mapContainer.value.firstChild);
      }
    }
  } catch (_) {
    // swallow — never let teardown error block route change
  }
});
</script>
