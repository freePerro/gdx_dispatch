<!--
  SS-15 slice D — Tenant-admin PAT issuance UI.

  TODO: mount in gdx/frontend/src/router/index.js at path
    /admin/api-keys once SS-15 integration lands. Gate route behind
    tenant-admin capability check.
  TODO: backend /api/admin/tenant-members endpoint (for the
    target-user dropdown) is expected to return [{identity_id, email,
    display_name, role}]. If that endpoint is not yet available, the UI
    falls back to a free-form UUID input.
  TODO: capability options endpoint is the same one SettingsApiKeys
    uses — /api/capabilities/available — reused here unchanged.
-->
<template>
    <section class="tenant-admin-api-keys view-card" data-testid="tenant-admin-api-keys-view">
    <header class="header view-heading-row">
      <Button
        icon="pi pi-arrow-left"
        aria-label="Back"
        text
        severity="secondary"
        size="small"
        class="back-button"
        @click="$router.back()"
      />
      <h1 class="view-heading">Tenant API Keys (Admin)</h1>
      <p class="muted">
        Issue Personal Access Tokens on behalf of users in your tenant.
        Write-scope tokens require your approval before they become usable.
      </p>
    </header>

    <!-- Issue form -->
    <section class="create-card" aria-labelledby="admin-issue-heading">
      <h3 id="admin-issue-heading">Issue a token on behalf of a user</h3>

      <div class="form-row">
        <label for="admin-target">Target user</label>
        <select
          v-if="members.length > 0"
          id="admin-target"
          v-model="form.targetIdentityId"
          data-testid="admin-target-select"
        >
          <option value="">— select user —</option>
          <option
            v-for="m in members"
            :key="m.identity_id"
            :value="m.identity_id"
            :data-testid="`admin-target-opt-${m.identity_id}`"
          >
            {{ m.display_name || m.email }} ({{ m.role }})
          </option>
        </select>
        <input
          v-else
          id="admin-target"
          v-model="form.targetIdentityId"
          type="text"
          placeholder="target identity UUID"
          data-testid="admin-target-input"
        />
      </div>

      <div class="form-row">
        <label for="admin-pat-name">Token name</label>
        <input
          id="admin-pat-name"
          v-model="form.name"
          type="text"
          placeholder="e.g. pager-duty-integration"
          data-testid="admin-pat-name-input"
        />
      </div>

      <div class="form-row">
        <label for="admin-pat-expiry">Expires in (days)</label>
        <input
          id="admin-pat-expiry"
          v-model.number="form.expiresInDays"
          type="number"
          min="1"
          max="366"
          data-testid="admin-pat-expiry-input"
        />
      </div>

      <fieldset class="capabilities-grid" data-testid="admin-capabilities-grid">
        <legend>Capabilities</legend>
        <p v-if="capsLoading" class="muted">Loading…</p>
        <p v-else-if="capabilities.length === 0" class="muted">
          No capabilities available.
        </p>
        <div
          v-for="cap in capabilities"
          :key="cap.id"
          class="capability-row"
          :data-testid="`admin-cap-row-${cap.id}`"
        >
          <label>
            <input
              type="checkbox"
              :value="cap.id"
              v-model="form.capabilityIds"
              :data-testid="`admin-cap-check-${cap.id}`"
            />
            <span class="cap-label">{{ cap.label }}</span>
            <span class="cap-pair muted">{{ cap.action }}:{{ cap.resource_type }}</span>
          </label>
        </div>
      </fieldset>

      <div class="actions">
        <button
          type="button"
          :disabled="creating || !canSubmit"
          data-testid="admin-pat-create-button"
          @click="issueToken"
        >
          {{ creating ? 'Issuing…' : 'Issue token' }}
        </button>
      </div>

      <div
        v-if="lastResult"
        class="result-banner"
        role="alert"
        data-testid="admin-pat-result-banner"
      >
        <p v-if="lastResult.status === 'pending_approval'" data-testid="admin-pat-pending">
          Token issued in <strong>pending approval</strong> state. Approve below to
          release the secret.
        </p>
        <p v-else-if="lastResult.secret" data-testid="admin-pat-secret-wrap">
          <strong>Copy this token now — it will never be shown again:</strong>
          <code data-testid="admin-pat-secret-value">{{ lastResult.secret }}</code>
        </p>
      </div>

      <p v-if="errorMessage" class="error" role="alert" data-testid="admin-pat-error">
        {{ errorMessage }}
      </p>
    </section>

    <!-- Issued tokens list -->
    <section class="list-card" aria-labelledby="admin-list-heading">
      <h3 id="admin-list-heading">Issued tokens</h3>
      <p v-if="listLoading" class="muted">Loading…</p>
      <table v-else-if="tokens.length" data-testid="admin-pat-list-table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Owner</th>
            <th>Status</th>
            <th>Prefix</th>
            <th>Expires</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="t in tokens"
            :key="t.id"
            :data-testid="`admin-pat-row-${t.id}`"
          >
            <td>{{ t.name }}</td>
            <td><code>{{ t.owner_identity_id }}</code></td>
            <td :data-testid="`admin-pat-status-${t.id}`">{{ t.status }}</td>
            <td><code>{{ t.prefix }}</code></td>
            <td>{{ formatDate(t.expires_at) }}</td>
            <td>
              <button
                v-if="t.status === 'pending_approval'"
                type="button"
                :data-testid="`admin-pat-approve-${t.id}`"
                @click="approveToken(t.id)"
              >
                Approve
              </button>
              <button
                type="button"
                :data-testid="`admin-pat-revoke-${t.id}`"
                @click="revokeToken(t.id)"
              >
                Revoke
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="muted" data-testid="admin-pat-list-empty">No tokens issued.</p>
    </section>
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import { createApiClient } from '../composables/useApi'

const api = createApiClient()
const CAPABILITIES_ENDPOINT = '/api/capabilities/available'
const ADMIN_PATS_ENDPOINT = '/api/admin/pats'
const MEMBERS_ENDPOINT = '/api/admin/tenant-members'

const capabilities = ref([])
const capsLoading = ref(false)
const members = ref([])
const tokens = ref([])
const listLoading = ref(false)
const creating = ref(false)
const lastResult = ref(null)
const errorMessage = ref('')

const form = ref({
  targetIdentityId: '',
  name: '',
  expiresInDays: 90,
  capabilityIds: [],
})

const canSubmit = computed(
  () => form.value.name.trim().length > 0 && form.value.targetIdentityId.trim().length > 0,
)

function formatDate(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleDateString()
  } catch {
    return iso
  }
}

async function loadCapabilities() {
  capsLoading.value = true
  try {
    const raw = await api.get(CAPABILITIES_ENDPOINT)
    const list = Array.isArray(raw) ? raw : raw.options || []
    capabilities.value = list.map((c, idx) => ({
      id: c.id || `${c.action}:${c.resource_type}:${idx}`,
      action: c.action,
      resource_type: c.resource_type,
      label: c.label || `${c.action} ${c.resource_type}`,
    }))
  } catch (err) {
    errorMessage.value = `Failed to load capabilities: ${err.message}`
  } finally {
    capsLoading.value = false
  }
}

async function loadMembers() {
  try {
    members.value = await api.get(MEMBERS_ENDPOINT)
  } catch {
    // Fallback: leave members empty → UI offers free-form UUID input.
    members.value = []
  }
}

async function loadTokens() {
  listLoading.value = true
  try {
    tokens.value = await api.get(ADMIN_PATS_ENDPOINT)
  } catch (err) {
    errorMessage.value = `Failed to load tokens: ${err.message}`
  } finally {
    listLoading.value = false
  }
}

async function issueToken() {
  errorMessage.value = ''
  lastResult.value = null
  creating.value = true
  try {
    lastResult.value = await api.post(ADMIN_PATS_ENDPOINT, {
      target_identity_id: form.value.targetIdentityId.trim(),
      name: form.value.name.trim(),
      expires_in_days: form.value.expiresInDays,
      capability_ids: form.value.capabilityIds,
    })
    form.value.name = ''
    form.value.capabilityIds = []
    await loadTokens()
  } catch (err) {
    errorMessage.value = err.message
  } finally {
    creating.value = false
  }
}

async function approveToken(id) {
  errorMessage.value = ''
  try {
    const body = await api.post(`${ADMIN_PATS_ENDPOINT}/${id}/approve`, {})
    // Approve may release a secret once — surface it in the same result banner.
    if (body.secret) {
      lastResult.value = { status: 'active', secret: body.secret }
    }
    await loadTokens()
  } catch (err) {
    errorMessage.value = err.message
  }
}

async function revokeToken(id) {
  errorMessage.value = ''
  try {
    await api.del(`${ADMIN_PATS_ENDPOINT}/${id}`)
    await loadTokens()
  } catch (err) {
    errorMessage.value = err.message
  }
}

onMounted(() => {
  loadCapabilities()
  loadMembers()
  loadTokens()
})
</script>

<style scoped>
.tenant-admin-api-keys {
  max-width: 900px;
  margin: 0 auto;
  padding: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}
.header h2 {
  margin: 0 0 0.25rem 0;
}
.muted {
  color: #666;
}
.create-card,
.list-card {
  border: 1px solid #ddd;
  border-radius: 6px;
  padding: 1rem 1.25rem;
}
.form-row {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  margin-bottom: 0.75rem;
}
.capabilities-grid {
  border: 1px solid #eee;
  border-radius: 4px;
  padding: 0.75rem;
  margin: 0.75rem 0;
}
.capability-row {
  display: block;
  padding: 0.25rem 0;
}
.cap-pair {
  margin-left: 0.5rem;
  font-family: monospace;
  font-size: 0.85em;
}
.actions {
  margin-top: 0.5rem;
}
.result-banner {
  margin-top: 1rem;
  padding: 0.75rem;
  background: #fff8d6;
  border: 1px solid #e6d56b;
  border-radius: 4px;
}
.error {
  color: #b00020;
}
table {
  width: 100%;
  border-collapse: collapse;
}
th,
td {
  text-align: left;
  padding: 0.4rem 0.5rem;
  border-bottom: 1px solid #eee;
}
</style>
