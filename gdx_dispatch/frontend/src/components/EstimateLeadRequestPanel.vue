<template>
  <aside
    v-if="canReadLeads && lead"
    class="lead-request-panel"
    data-testid="estimate-lead-request-panel"
  >
    <Card class="request-card">
      <template #title>
        <div class="panel-header">
          <div class="panel-title-wrap">
            <i class="pi pi-inbox panel-icon" />
            <span class="panel-title">Customer Request</span>
          </div>
          <router-link
            :to="{ path: '/leads', query: { id: lead.id } }"
            class="view-lead-link"
            data-testid="lead-request-link"
          >
            Lead #{{ lead.id?.slice(0, 8) }}
            <i class="pi pi-external-link" />
          </router-link>
        </div>
      </template>

      <template #content>
        <div class="panel-body">
          <div v-if="lead.notes" class="field-block">
            <span class="field-label">Request Notes</span>
            <p class="field-notes" data-testid="lead-request-notes">{{ lead.notes }}</p>
          </div>

          <QuoteRequestDoors :lead-id="lead.id" />

          <div class="meta-grid">
            <div v-if="lead.source" class="meta-item">
              <span class="meta-label">Source</span>
              <span class="meta-value" data-testid="lead-request-source">{{ lead.source }}</span>
            </div>
            <div v-if="lead.follow_up_date" class="meta-item">
              <span class="meta-label">Call Back</span>
              <span class="meta-value" data-testid="lead-request-followup">{{ formatDate(lead.follow_up_date) }}</span>
            </div>
            <div v-if="lead.created_at" class="meta-item">
              <span class="meta-label">Received</span>
              <span class="meta-value">{{ formatDate(lead.created_at) }}</span>
            </div>
          </div>

          <div v-if="displayCustomFields.length" class="custom-fields-section">
            <span class="field-label">Intake Details</span>
            <dl class="custom-fields-list" data-testid="lead-request-custom-fields">
              <div
                v-for="cf in displayCustomFields"
                :key="cf.field_key"
                class="custom-field-row"
                :data-testid="`lead-cf-${cf.field_key}`"
              >
                <dt class="cf-label">{{ cf.label || cf.field_key }}</dt>
                <dd class="cf-value">{{ formatCustomValue(cf) }}</dd>
              </div>
            </dl>
          </div>
        </div>
      </template>
    </Card>
  </aside>
</template>

<script setup>
import { computed, ref, watch } from 'vue';
import { useAuthStore } from '../stores/auth';
import { useApi } from '../composables/useApi';
import { formatDate } from '../composables/useFormatters';
import Card from 'primevue/card';
import QuoteRequestDoors from './QuoteRequestDoors.vue';

const props = defineProps({
  estimateId: {
    type: [String, Number],
    default: null,
  },
});

const auth = useAuthStore();
const api = useApi();

const lead = ref(null);
const loading = ref(false);

const canReadLeads = computed(() => auth.hasPermission('leads.read'));

const displayCustomFields = computed(() => {
  if (!lead.value?.custom_fields || !Array.isArray(lead.value.custom_fields)) return [];
  return lead.value.custom_fields.filter(
    (cf) => cf.value !== null && cf.value !== undefined && cf.value !== '',
  );
});

// Answers are stored as text, so a boolean arrives as "true"/"false".
function formatCustomValue(cf) {
  if (cf.value === true || cf.value === 'true') return 'Yes';
  if (cf.value === false || cf.value === 'false') return 'No';
  return String(cf.value);
}

async function loadLeadForEstimate(estId) {
  if (!estId || !canReadLeads.value) {
    lead.value = null;
    return;
  }
  loading.value = true;
  try {
    // 404 is the normal answer for an estimate no lead started — most of
    // them — so it must not toast.
    const data = await api.get(`/api/leads/by-estimate/${estId}`, { suppressErrorToast: true });
    lead.value = data && data.id ? data : null;
  } catch {
    lead.value = null;
  } finally {
    loading.value = false;
  }
}

watch(
  () => props.estimateId,
  (newId) => {
    loadLeadForEstimate(newId);
  },
  { immediate: true },
);
</script>

<style scoped>
.lead-request-panel {
  display: flex;
  flex-direction: column;
}

.request-card {
  border: 1px solid var(--p-content-border-color);
  background: var(--p-content-background);
  border-radius: 8px;
}

.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 0.95rem;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--p-content-border-color);
}

.panel-title-wrap {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-weight: 600;
  color: var(--p-text-color);
}

.panel-icon {
  color: var(--p-primary-color);
}

.view-lead-link {
  font-size: 0.8rem;
  display: flex;
  align-items: center;
  gap: 0.25rem;
  color: var(--p-primary-color);
  text-decoration: none;
}

.view-lead-link:hover {
  text-decoration: underline;
}

.panel-body {
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
  font-size: 0.85rem;
}

.field-block {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.field-label {
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-weight: 700;
  color: var(--p-text-muted-color);
}

.field-notes {
  margin: 0;
  padding: 0.6rem;
  background: var(--p-content-hover-background);
  border-radius: 6px;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.4;
  color: var(--p-text-color);
}

.meta-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 0.5rem;
  padding: 0.5rem 0;
  border-top: 1px dashed var(--p-content-border-color);
  border-bottom: 1px dashed var(--p-content-border-color);
}

.meta-item {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}

.meta-label {
  font-size: 0.75rem;
  color: var(--p-text-muted-color);
}

.meta-value {
  font-weight: 600;
  color: var(--p-text-color);
}

.custom-fields-section {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.custom-fields-list {
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.custom-field-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 0.5rem;
  padding: 0.2rem 0;
}

.cf-label {
  color: var(--p-text-muted-color);
  font-size: 0.82rem;
}

.cf-value {
  margin: 0;
  font-weight: 600;
  color: var(--p-text-color);
  text-align: right;
  word-break: break-word;
}

@media print {
  .lead-request-panel {
    display: none !important;
  }
}
</style>
