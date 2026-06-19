<script setup>
import { ref, onMounted } from 'vue'
import { useApi } from '../composables/useApi'
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApi()

const llmEnabled = ref(false)
const loading = ref(true)
const error = ref(null)

const aiSettings = ref({ key_set: false, last_validated_at: null, last_error: null })
const keyInput = ref('')
const keyStatus = ref(null)
const adminError = ref(false)

const aiAuditItems = ref([])
const auditError = ref(null)

function formatRelative(iso) {
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const diffSec = Math.max(0, (Date.now() - t) / 1000)
  if (diffSec < 60) return 'just now'
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`
  if (diffSec < 7 * 86400) return `${Math.floor(diffSec / 86400)}d ago`
  return new Date(t).toLocaleDateString()
}

const fetchModules = async () => {
  try {
    loading.value = true
    error.value = null
    const response = await api.get('/api/settings/modules')
    const llmModule = response.modules?.find(m => m.key === 'llm')
    if (llmModule) llmEnabled.value = !!llmModule.enabled
  } catch (err) {
    error.value = err.message || 'Failed to load settings'
  } finally {
    loading.value = false
  }
}

const fetchAiSettings = async () => {
  try {
    aiSettings.value = await api.get('/api/admin/ai-settings')
    adminError.value = false
  } catch (err) {
    if (err.status === 403) adminError.value = true
    else console.error('Failed to fetch AI settings:', err)
  }
}

const fetchAiAudit = async () => {
  try {
    const response = await api.get('/api/admin/ai-settings/audit?limit=10')
    aiAuditItems.value = response.items || []
    auditError.value = null
  } catch (err) {
    aiAuditItems.value = []
    auditError.value = err.message || 'audit unavailable'
  }
}

const toggleModule = async () => {
  const previousValue = llmEnabled.value
  const newValue = !previousValue
  llmEnabled.value = newValue
  try {
    const endpoint = newValue ? '/api/settings/modules/llm/enable' : '/api/settings/modules/llm/disable'
    await api.post(endpoint)
  } catch (err) {
    llmEnabled.value = previousValue
    error.value = err.message || 'Failed to update setting'
  }
}

const saveApiKey = async () => {
  if (keyInput.value.length < 10) return
  try {
    const response = await api.put('/api/admin/ai-settings', { key: keyInput.value })
    if (response.ok) {
      keyStatus.value = {
        ok: true,
        message: `${response.model} ${response.latency_ms}ms - valid`,
        class: 'status-ok',
      }
      keyInput.value = ''
      await fetchAiSettings()
      await fetchAiAudit()
    } else {
      keyStatus.value = {
        ok: false,
        message: response.error || 'Validation failed',
        class: 'status-error',
      }
    }
  } catch (err) {
    keyStatus.value = {
      ok: false,
      message: err.message || 'Failed to save key',
      class: 'status-error',
    }
  }
}

const rotateKey = () => {
  const input = document.getElementById('ai-key-input')
  if (input) {
    input.focus()
    input.removeAttribute('disabled')
  }
}

const removeKey = async () => {
  if (!(await confirmAsync({ header: 'Confirm', message: 'Are you sure you want to remove the Anthropic API key? This will also disable the AI Assistant.' }))) return
  try {
    await api.del('/api/admin/ai-settings')
    try {
      await api.post('/api/settings/modules/llm/disable')
      llmEnabled.value = false
    } catch (disableErr) {
      error.value = disableErr.message || 'Key removed, but failed to disable AI Assistant.'
    }
    await fetchAiSettings()
    await fetchAiAudit()
  } catch (err) {
    error.value = err.message || 'Failed to remove key'
  }
}

onMounted(() => {
  fetchModules()
  fetchAiSettings()
  fetchAiAudit()
})
</script>

<template>
  <div class="ai-card" data-testid="ai-integration-card">
    <div class="ai-card-header">
      <div>
        <h3>AI Assistant</h3>
        <p class="muted">Enable or disable the LLM-powered features. Bring your own Anthropic API key.</p>
      </div>
      <div class="toggle-row">
        <input
          type="checkbox"
          id="ai-module-toggle"
          class="toggle-input"
          data-test="ai-module-toggle"
          :checked="llmEnabled"
          @change="toggleModule"
        />
        <label for="ai-module-toggle">Enabled</label>
      </div>
    </div>

    <div v-if="error" class="status-row status-error">{{ error }}</div>
    <div v-if="loading" class="muted">Loading…</div>

    <div v-else>
      <div v-if="adminError" class="status-row status-warn">
        Admin access required to manage API keys.
      </div>
      <div v-else class="key-section">
        <label for="ai-key-input">Anthropic API Key</label>
        <div class="key-row">
          <input
            id="ai-key-input"
            type="password"
            v-model="keyInput"
            data-test="ai-key-input"
            placeholder="sk-ant-..."
            class="key-input"
          />
          <button
            type="button"
            data-test="ai-key-save"
            :disabled="keyInput.length < 10"
            @click="saveApiKey"
            class="btn-primary"
          >
            Save &amp; Test
          </button>
        </div>

        <div v-if="keyStatus" data-test="ai-key-status" :class="['status-row', keyStatus.class]">
          {{ keyStatus.message }}
        </div>

        <div v-if="aiSettings.key_set" class="key-meta">
          <span v-if="aiSettings.last_error" class="status-error">
            Key configured, but last validation failed: {{ aiSettings.last_error }}
          </span>
          <span v-else class="muted">
            Key on file (last validated: {{ aiSettings.last_validated_at }})
          </span>
          <div class="key-actions">
            <button type="button" data-test="ai-key-rotate" @click="rotateKey" class="btn-link">Rotate key</button>
            <button type="button" data-test="ai-key-remove" @click="removeKey" class="btn-link btn-link-danger">Remove key</button>
          </div>
        </div>
      </div>

      <section data-test="ai-audit-panel" class="audit-panel">
        <h4>Recent Activity</h4>
        <div v-if="aiAuditItems.length === 0" class="muted">No events yet</div>
        <ul v-else class="audit-list">
          <li v-for="item in aiAuditItems" :key="item.id">
            <div class="audit-action">
              <span class="audit-name">{{ item.action }}</span>
              <span class="muted">{{ item.user_name }}</span>
            </div>
            <span class="muted audit-date" :title="item.created_at">{{ formatRelative(item.created_at) }}</span>
          </li>
        </ul>
      </section>
    </div>
  </div>
</template>

<style scoped>
.ai-card {
  background: var(--surface-panel);
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.ai-card-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
}
.ai-card-header h3 {
  margin: 0 0 0.25rem 0;
  font-size: 1rem;
  font-weight: 600;
}
.muted { color: var(--text-muted); font-size: 0.875rem; }
.toggle-row { display: flex; align-items: center; gap: 0.5rem; }
.toggle-input { width: 1.25rem; height: 1.25rem; cursor: pointer; }
.key-section { display: flex; flex-direction: column; gap: 0.5rem; }
.key-row { display: flex; gap: 0.5rem; }
.key-input {
  flex: 1;
  padding: 0.5rem 0.75rem;
  border: 1px solid var(--border-subtle);
  border-radius: 6px;
  background: var(--surface-elevated);
  color: var(--text-primary);
}
.btn-primary {
  padding: 0.5rem 1rem;
  background: var(--interactive-primary);
  color: #fff;
  border: 0;
  border-radius: 6px;
  cursor: pointer;
}
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-link {
  background: none;
  border: 0;
  color: var(--interactive-primary);
  cursor: pointer;
  font-size: 0.875rem;
  padding: 0;
}
.btn-link-danger { color: var(--color-danger-500); }
.status-row { padding: 0.5rem 0.75rem; border-radius: 6px; font-size: 0.875rem; }
.status-error { color: var(--color-danger-500); background: var(--color-danger-bg); border: 1px solid var(--color-danger-border); }
.status-warn { color: var(--color-warning-500); background: var(--color-warning-bg); border: 1px solid var(--color-warning-border); }
.status-ok { color: var(--color-success-500); background: var(--color-success-bg); border: 1px solid var(--color-success-border); }
.key-meta { display: flex; flex-direction: column; gap: 0.5rem; }
.key-actions { display: flex; gap: 1rem; }
.audit-panel { border-top: 1px solid var(--border-subtle); padding-top: 1rem; }
.audit-panel h4 { margin: 0 0 0.5rem 0; font-size: 0.875rem; font-weight: 600; }
.audit-list { list-style: none; padding: 0; margin: 0; }
.audit-list li {
  display: flex;
  justify-content: space-between;
  padding: 0.5rem 0;
  border-bottom: 1px solid var(--border-subtle);
}
.audit-action { display: flex; flex-direction: column; }
.audit-name { font-size: 0.875rem; font-weight: 500; }
.audit-date { font-size: 0.75rem; }
</style>
