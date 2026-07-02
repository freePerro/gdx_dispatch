<template>
    <section class="maps-view view-card" data-testid="maps-view">
      <Toolbar>
        <template #start>
          <h2 class="page-title" data-testid="maps-title">Maps & Routing</h2>
        </template>
      </Toolbar>

      <Tabs value="tech" class="maps-tabs" data-testid="maps-tabs">
        <TabList>
          <Tab value="tech" data-testid="maps-tab-tech">Tech Locations</Tab>
          <Tab value="routes" data-testid="maps-tab-routes">Route Optimizations</Tab>
        </TabList>
        <TabPanels>
        <TabPanel value="tech">
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
              placeholder="Filter by tech or status"
              class="w-full"
              data-testid="maps-tech-filter"
            />
            <Select
              v-model="statusFilter"
              :options="statusFilterOptions"
              optionLabel="label"
              optionValue="value"
              class="w-full"
              data-testid="maps-status-filter"
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
          <DataTable
            v-else
            :value="filteredTechLocations"
            striped-rows
            responsiveLayout="scroll"
            emptyMessage="No technician locations"
            data-testid="maps-tech-table"
          >
            <Column field="tech_name" header="Technician" />
            <Column field="lat" header="Lat" :body="({ data }) => formatCoordinate(data.lat)" />
            <Column field="lng" header="Lng" :body="({ data }) => formatCoordinate(data.lng)" />
            <Column field="updated_at" header="Updated" :body="({ data }) => formatTimestamp(data.updated_at)" />
            <Column field="status" header="Status" />
          </DataTable>
        </TabPanel>

        <TabPanel value="routes">
          <div v-if="loading" class="spinner-wrap" data-testid="maps-loading-routes">
            <ProgressSpinner />
          </div>
          <DataTable
            v-else
            :value="routeOptimizations"
            striped-rows
            responsiveLayout="scroll"
            emptyMessage="No route plans"
            data-testid="maps-routes-table"
          >
            <Column field="date" header="Date" :body="({ data }) => formatDate(data.date)" />
            <Column field="tech" header="Technician" />
            <Column field="stops" header="Stops" />
            <Column field="distance" header="Distance" :body="({ data }) => formatDistance(data.distance)" />
            <Column field="duration" header="Duration" :body="({ data }) => formatDuration(data.duration)" />
            <Column
              field="saved_minutes"
              header="Saved"
              :body="({ data }) =>
                (data.saved_minutes || data.saved_minutes === 0) ? `${data.saved_minutes} min` : '—'"
            />
          </DataTable>
        </TabPanel>
        </TabPanels>
      </Tabs>
    </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatDateTime as formatTimestamp } from "../composables/useFormatters";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Select from "primevue/select";
import InputText from "primevue/inputtext";
import ProgressSpinner from "primevue/progressspinner";
import Tab from "primevue/tab";
import TabList from "primevue/tablist";
import TabPanel from "primevue/tabpanel";
import TabPanels from "primevue/tabpanels";
import Tabs from "primevue/tabs";
import Toolbar from "primevue/toolbar";

const api = useApiWithToast();

const techLocations = ref([]);
const routeOptimizations = ref([]);
const loading = ref(true);
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
const statusFilter = ref("all");
const lastRefresh = ref(null);

const statusFilterOptions = computed(() => {
  const statuses = Array.from(new Set(techLocations.value.map((item) => item.status).filter(Boolean)));
  const items = statuses.map((status) => ({ label: status, value: status }));
  return [{ label: "All Statuses", value: "all" }, ...items];
});

const filteredTechLocations = computed(() => {
  return techLocations.value.filter((item) => {
    const matchesStatus = statusFilter.value === "all" || item.status === statusFilter.value;
    const filterText = techFilter.value.toLowerCase().trim();
    if (!filterText) return matchesStatus;
    const haystack = `${item.tech_name ?? ""} ${item.status ?? ""}`.toLowerCase();
    return matchesStatus && haystack.includes(filterText);
  });
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

function formatDate(value) {
  if (!value) return "—";
  if (typeof value === "string" && value.includes("T")) {
    return value.split("T")[0];
  }
  return value;
}

function formatDistance(value) {
  if (value === null || value === undefined) return "—";
  const parsed = Number(value);
  if (Number.isNaN(parsed)) return "—";
  return `${parsed.toFixed(1)} mi`;
}

function formatDuration(value) {
  if (value === null || value === undefined) return "—";
  const parsed = Number(value);
  if (Number.isNaN(parsed)) return "—";
  const minutes = Math.floor(parsed / 60);
  const seconds = parsed % 60;
  if (minutes) {
    return `${minutes}m ${seconds}s`;
  }
  return `${seconds}s`;
}

async function loadMaps() {
  loading.value = true;
  try {
    const data = await api.get("/api/maps");
    techLocations.value = Array.isArray(data?.tech_locations) ? data.tech_locations : [];
    routeOptimizations.value = Array.isArray(data?.route_optimizations)
      ? data.route_optimizations
      : Array.isArray(data?.routes)
        ? data.routes
        : [];
    lastRefresh.value = new Date();
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
    if (!t.lat || !t.lng) continue;
    const marker = new window.google.maps.Marker({
      position: { lat: parseFloat(t.lat), lng: parseFloat(t.lng) },
      map: googleMap,
      title: t.tech_name || 'Technician',
      icon: { path: window.google.maps.SymbolPath.CIRCLE, scale: 10, fillColor: '#3b82f6', fillOpacity: 1, strokeColor: '#fff', strokeWeight: 2 },
    });
    marker.addListener('click', () => {
      mapInfoWindow.setContent(`<div style="padding:4px"><strong>${t.tech_name}</strong><br>Status: ${t.status || 'active'}</div>`);
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
