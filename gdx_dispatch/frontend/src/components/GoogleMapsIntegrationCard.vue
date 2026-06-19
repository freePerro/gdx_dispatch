<script setup>
import { ref, onMounted } from 'vue'
import { useApi } from '../composables/useApi'

const api = useApi()

const keyInput = ref('')
const configured = ref(false)
const saving = ref(false)
const status = ref(null)
const showKey = ref(false)

const fetchKey = async () => {
  try {
    const r = await api.get('/api/settings/integrations/google-maps')
    keyInput.value = r.key || ''
    configured.value = !!r.configured
  } catch (err) {
    status.value = { ok: false, message: err.message || 'Failed to load Maps key' }
  }
}

const saveKey = async () => {
  saving.value = true
  status.value = null
  try {
    const r = await api.patch('/api/settings/integrations/google-maps', {
      key: keyInput.value || null,
    })
    configured.value = !!r.configured
    status.value = {
      ok: true,
      message: r.configured ? 'Key saved.' : 'Key removed.',
    }
  } catch (err) {
    status.value = { ok: false, message: err.message || 'Failed to save key' }
  } finally {
    saving.value = false
  }
}

onMounted(fetchKey)
</script>

<template>
  <div class="maps-card" data-testid="maps-integration-card">
    <div class="maps-header">
      <div>
        <h3>Google Maps</h3>
        <p class="muted">
          Drives the Maps and GPS tabs. Bring your own Google Cloud project — restrict the
          key to your tenant domain in Google Cloud Console.
        </p>
      </div>
      <span :class="['badge', configured ? 'badge-ok' : 'badge-warn']">
        {{ configured ? 'Configured' : 'Not Configured' }}
      </span>
    </div>

    <div class="key-section">
      <label for="maps-key-input">API Key</label>
      <div class="key-row">
        <input
          id="maps-key-input"
          :type="showKey ? 'text' : 'password'"
          v-model="keyInput"
          data-testid="maps-key-input"
          placeholder="AIza..."
          class="key-input"
        />
        <button
          type="button"
          class="btn-link"
          data-testid="maps-key-toggle-visibility"
          @click="showKey = !showKey"
        >
          {{ showKey ? 'Hide' : 'Show' }}
        </button>
        <button
          type="button"
          class="btn-primary"
          data-testid="maps-key-save"
          :disabled="saving"
          @click="saveKey"
        >
          {{ saving ? 'Saving…' : 'Save' }}
        </button>
      </div>
      <p class="hint muted">
        Required APIs: Maps JavaScript API. Recommended: restrict the key to
        <code>*.example.com</code> in the Google Cloud Console &gt; APIs &amp; Services &gt; Credentials.
      </p>

      <div v-if="status" :class="['status-row', status.ok ? 'status-ok' : 'status-error']">
        {{ status.message }}
      </div>
    </div>
  </div>
</template>

<style scoped>
.maps-card {
  background: var(--surface-panel);
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.maps-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
}
.maps-header h3 {
  margin: 0 0 0.25rem 0;
  font-size: 1rem;
  font-weight: 600;
}
.muted { color: var(--text-muted); font-size: 0.875rem; }
.hint { font-size: 0.75rem; }
.badge {
  font-size: 0.75rem;
  padding: 0.25rem 0.5rem;
  border-radius: 999px;
  font-weight: 600;
}
.badge-ok { background: var(--color-success-bg, #dcfce7); color: var(--color-success-500, #15803d); }
.badge-warn { background: var(--color-warning-bg, #fef3c7); color: var(--color-warning-500, #92400e); }
.key-section { display: flex; flex-direction: column; gap: 0.5rem; }
.key-row { display: flex; gap: 0.5rem; align-items: center; }
.key-input {
  flex: 1;
  padding: 0.5rem 0.75rem;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--surface-elevated);
  color: var(--text-primary);
  font-family: monospace;
}
.btn-primary {
  padding: 0.5rem 1rem;
  background: var(--primary, #2563eb);
  color: #fff;
  border: 0;
  border-radius: 6px;
  cursor: pointer;
}
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-link {
  background: none;
  border: 1px solid var(--border-subtle, #d1d5db);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
  color: var(--text-primary, #374151);
  cursor: pointer;
  font-size: 0.875rem;
}
.status-row { padding: 0.5rem 0.75rem; border-radius: 6px; font-size: 0.875rem; }
.status-error { color: #dc2626; background: #fef2f2; border: 1px solid #fecaca; }
.status-ok { color: #15803d; background: #f0fdf4; border: 1px solid #bbf7d0; }
code { background: #f3f4f6; padding: 0.05em 0.3em; border-radius: 3px; }
</style>
