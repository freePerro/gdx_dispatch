<script setup>
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import { useConfirm } from 'primevue/useconfirm'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'

// Sprint Outlook Integration — slice S10. Renders Connect/Connected state +
// confirms before disconnect. Mounted in user profile (slice S40 wires the
// user-Settings → Integrations tab).

const api = useApi()
const confirm = useConfirm()
const toast = useToast()

const state = ref(null)
const loading = ref(false)
const error = ref(null)

const isConnected = computed(() => !!state.value?.connected)

async function refresh() {
  loading.value = true
  error.value = null
  try {
    state.value = await api.get('/api/oauth/outlook/account')
  } catch (err) {
    const detail = err?.message || err?.response?.data?.detail || 'Failed to load Outlook state'
    error.value = detail
    console.error('OutlookConnectButton: load failed', err)
    toast.add({
      severity: 'error',
      summary: 'Outlook status unavailable',
      detail,
      life: 5000,
    })
  } finally {
    loading.value = false
  }
}

async function onConnect() {
  // POST /start with the SPA's Bearer header so the server can authenticate
  // the user. Server returns the Microsoft consent URL, then we navigate.
  // (A direct `window.location.href = '/api/...'` can't carry the Bearer
  // header — caused 401s in 2026-04-28 prod.)
  try {
    const { authorize_url } = await api.post('/api/oauth/outlook/start', {})
    if (!authorize_url) {
      throw new Error('Server returned no authorize_url')
    }
    window.location.href = authorize_url
  } catch (err) {
    console.error('OutlookConnectButton: connect failed', err)
    toast.add({
      severity: 'error',
      summary: 'Could not start Outlook sign-in',
      detail: err?.message || 'Unknown error',
      life: 6000,
    })
  }
}

function onDisconnect() {
  confirm.require({
    message: `Disconnect ${state.value?.upn ?? 'Outlook'}? Historical messages stay; new sync stops until you reconnect.`,
    header: 'Disconnect Outlook',
    icon: 'pi pi-exclamation-triangle',
    acceptClass: 'p-button-danger',
    accept: async () => {
      try {
        await api.del('/api/oauth/outlook/account')
        await refresh()
        toast.add({
          severity: 'success',
          summary: 'Disconnected',
          detail: 'Outlook integration removed. Reconnect anytime.',
          life: 4000,
        })
      } catch (err) {
        toast.add({
          severity: 'error',
          summary: 'Disconnect failed',
          detail: err?.message || 'Unknown error',
          life: 6000,
        })
      }
    },
  })
}

onMounted(() => {
  refresh()
})

defineExpose({ refresh, isConnected, state })
</script>

<template>
  <div class="outlook-connect">
    <!-- ConfirmDialog removed 2026-05-12 — AppLayout.vue:49 mounts one globally. -->
    <div v-if="loading" class="loading-text">Loading…</div>
    <template v-else-if="isConnected">
      <div class="flex items-center gap-3">
        <i class="pi pi-microsoft ms-icon" />
        <div class="flex-1">
          <div class="font-medium">{{ state?.display_name ?? state?.upn }}</div>
          <div class="upn-text">{{ state?.upn }}</div>
          <div v-if="state?.last_error" class="error-text mt-1">
            ⚠ {{ state.last_error }} — try reconnecting.
          </div>
        </div>
        <Button
          label="Disconnect"
          severity="secondary"
          size="small"
          @click="onDisconnect"
        />
      </div>
    </template>
    <template v-else>
      <Button
        icon="pi pi-microsoft"
        label="Connect Outlook"
        @click="onConnect"
      />
      <div v-if="error" class="error-text">⚠ {{ error }}</div>
    </template>
  </div>
</template>

<style scoped>
.outlook-connect {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.ms-icon {
  color: var(--p-blue-500, #3b82f6);
  font-size: 1.25rem;
}
.loading-text {
  color: var(--p-text-muted-color);
  font-size: 0.875rem;
}
.upn-text {
  color: var(--p-text-muted-color);
  font-size: 0.75rem;
}
.error-text {
  color: var(--p-red-500, #ef4444);
  font-size: 0.75rem;
}
</style>
