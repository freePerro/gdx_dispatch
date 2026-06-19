<script setup>
import { onMounted, ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'

// Sprint Outlook Integration — quick-glance card on Settings → Integrations.
// Full config lives at /settings/integrations/outlook (OutlookSettingsView).

const api = useApi()
const router = useRouter()
const toast = useToast()

const loading = ref(true)
const credentials = ref(null)
const adminError = ref(false)
const loadError = ref(null)

const isConfigured = computed(() => Boolean(credentials.value?.secret_set))

async function load() {
  loading.value = true
  adminError.value = false
  loadError.value = null
  try {
    credentials.value = await api.get('/api/admin/outlook/credentials')
  } catch (err) {
    if (err?.status === 403) {
      // Non-admin viewing the card — render a quiet hint, don't toast.
      adminError.value = true
    } else {
      // Status-only card — don't toast a global error. Surface inline so
      // the rest of the Settings page is unaffected. Click "Configure" to
      // get to the full OutlookSettingsView, which has its own error handling.
      const detail = err?.message || err?.response?.data?.detail || 'Unable to load status'
      loadError.value = detail
      console.warn('outlook card load failed:', err)
    }
  } finally {
    loading.value = false
  }
}

function openSettings() {
  router.push({ name: 'outlook-settings' })
}

onMounted(() => {
  load()
})
</script>

<template>
  <div class="outlook-card" data-testid="outlook-integration-card">
    <div class="outlook-header">
      <div>
        <h3><i class="pi pi-microsoft ms-icon" /> Outlook / Microsoft 365</h3>
        <p class="muted">Per-employee mailbox sync, tagging, and send-from-GDX.</p>
      </div>
      <Tag
        v-if="!loading && !adminError"
        :value="isConfigured ? 'Connected' : 'Not Configured'"
        :severity="isConfigured ? 'success' : 'warning'"
      />
    </div>

    <div v-if="loading" class="muted">Loading…</div>
    <div v-else-if="adminError" class="status-warn">
      Admin access required to view Outlook integration status.
    </div>
    <small v-else-if="loadError" class="muted">
      Status unavailable — click Configure to manage.
    </small>
    <small v-else-if="isConfigured" class="muted">
      Microsoft tenant + Entra app credentials configured. Employees can connect their mailboxes from their profile.
    </small>
    <small v-else class="muted">
      Microsoft Entra ID client_secret not set. Configure to enable the integration.
    </small>

    <div class="integration-actions">
      <Button
        :label="isConfigured ? 'Manage' : 'Configure'"
        :severity="isConfigured ? 'secondary' : 'primary'"
        @click="openSettings"
      />
    </div>
  </div>
</template>

<style scoped>
.outlook-card {
  background: var(--surface-panel);
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.outlook-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
}
.outlook-header h3 {
  margin: 0 0 0.25rem 0;
  font-size: 1rem;
  font-weight: 600;
}
.muted { color: var(--text-muted); font-size: 0.875rem; margin: 0; }
.status-warn {
  color: var(--color-warning-500, #f59e0b);
  background: var(--color-warning-bg, transparent);
  font-size: 0.875rem;
  padding: 0.5rem 0.75rem;
  border-radius: 6px;
}
.ms-icon {
  color: var(--p-blue-500, #3b82f6);
  margin-right: 0.4rem;
}
.integration-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
</style>
