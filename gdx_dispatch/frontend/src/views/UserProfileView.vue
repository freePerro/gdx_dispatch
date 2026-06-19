<template>
    <section class="user-profile-view view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">My Profile</h1>
        </template>
        <template #end>
          <Button
            label="Refresh"
            icon="pi pi-refresh"
            severity="secondary"
            @click="fetchMe"
          />
        </template>
      </Toolbar>

      <div v-if="error" class="error-banner">{{ error }}</div>
      <div v-if="successMsg" class="success-banner">{{ successMsg }}</div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <div v-else-if="me" class="profile-grid">
        <Card>
          <template #title>Account</template>
          <template #content>
            <div class="field-row">
              <label>Email</label>
              <InputText :model-value="me.email" disabled />
            </div>
            <div class="field-row">
              <label>Role</label>
              <Tag :value="me.role || 'unknown'" severity="info" />
            </div>
            <div class="field-row">
              <label>Member since</label>
              <span class="text-muted">{{ formatDateTime(me.created_at) }}</span>
            </div>
          </template>
        </Card>

        <Card>
          <template #title>Personal info</template>
          <template #content>
            <div class="field-row">
              <label for="profile-name">Name</label>
              <InputText id="profile-name" v-model="form.name" data-testid="profile-name" />
            </div>
            <div class="field-row">
              <label for="profile-phone">Phone</label>
              <InputText id="profile-phone" v-model="form.phone" data-testid="profile-phone" />
            </div>
            <div class="field-row">
              <label for="profile-route-start">Route start address</label>
              <InputText
                id="profile-route-start"
                v-model="form.route_start_address"
                data-testid="profile-route-start"
                placeholder="Used for tech route optimization"
              />
            </div>
            <div class="actions-row">
              <Button
                label="Save changes"
                icon="pi pi-save"
                :loading="saving"
                :disabled="!hasProfileChanges"
                data-testid="profile-save"
                @click="saveProfile"
              />
            </div>
          </template>
        </Card>

        <Card>
          <template #title>Change password</template>
          <template #content>
            <div class="field-row">
              <label for="pw-current">Current password</label>
              <Password
                id="pw-current"
                v-model="pw.current"
                :feedback="false"
                toggleMask
                inputClass="pw-input"
                data-testid="pw-current"
              />
            </div>
            <div class="field-row">
              <label for="pw-new">New password</label>
              <Password
                id="pw-new"
                v-model="pw.next"
                toggleMask
                inputClass="pw-input"
                data-testid="pw-new"
              />
            </div>
            <div class="field-row">
              <label for="pw-confirm">Confirm new password</label>
              <Password
                id="pw-confirm"
                v-model="pw.confirm"
                :feedback="false"
                toggleMask
                inputClass="pw-input"
                data-testid="pw-confirm"
              />
            </div>
            <div v-if="pwError" class="inline-error">{{ pwError }}</div>
            <div class="actions-row">
              <Button
                label="Change password"
                icon="pi pi-lock"
                :loading="changingPw"
                :disabled="!canChangePw"
                data-testid="pw-submit"
                @click="changePassword"
              />
            </div>
          </template>
        </Card>
      </div>
    </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useApi } from '../composables/useApi'

import Toolbar from 'primevue/toolbar'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Password from 'primevue/password'
import Card from 'primevue/card'
import Tag from 'primevue/tag'
import ProgressSpinner from 'primevue/progressspinner'

const api = useApi()

const me = ref(null)
const loading = ref(false)
const saving = ref(false)
const changingPw = ref(false)
const error = ref(null)
const successMsg = ref(null)

const form = reactive({
  name: '',
  phone: '',
  route_start_address: '',
})

const pw = reactive({
  current: '',
  next: '',
  confirm: '',
})

const pwError = computed(() => {
  if (!pw.next && !pw.confirm) return null
  if (pw.next && pw.next.length < 8) return 'New password must be at least 8 characters.'
  if (pw.next !== pw.confirm) return 'New password and confirmation do not match.'
  return null
})

const canChangePw = computed(
  () => Boolean(pw.current) && Boolean(pw.next) && pw.next === pw.confirm && pw.next.length >= 8,
)

const hasProfileChanges = computed(() => {
  if (!me.value) return false
  return (
    (form.name || '') !== (me.value.name || '') ||
    (form.phone || '') !== (me.value.phone || '') ||
    (form.route_start_address || '') !== (me.value.route_start_address || '')
  )
})

function formatDateTime(iso) {
  if (!iso) return '—'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  return new Date(t).toLocaleString()
}

function flashSuccess(msg) {
  successMsg.value = msg
  setTimeout(() => {
    successMsg.value = null
  }, 3500)
}

async function fetchMe() {
  loading.value = true
  error.value = null
  try {
    const r = await api.get('/api/users/me')
    me.value = r
    form.name = r.name || ''
    form.phone = r.phone || ''
    form.route_start_address = r.route_start_address || ''
  } catch (err) {
    error.value = err.message || 'Failed to load profile'
  } finally {
    loading.value = false
  }
}

async function saveProfile() {
  saving.value = true
  error.value = null
  try {
    const payload = {
      name: form.name,
      phone: form.phone,
      route_start_address: form.route_start_address,
    }
    const r = await api.patch('/api/users/me', payload)
    me.value = r
    flashSuccess('Profile saved.')
  } catch (err) {
    error.value = err.message || 'Failed to save profile'
  } finally {
    saving.value = false
  }
}

async function changePassword() {
  if (!canChangePw.value) return
  changingPw.value = true
  error.value = null
  try {
    await api.post('/api/users/me/change-password', {
      current_password: pw.current,
      new_password: pw.next,
    })
    pw.current = ''
    pw.next = ''
    pw.confirm = ''
    flashSuccess('Password changed.')
  } catch (err) {
    error.value = err.message || 'Failed to change password'
  } finally {
    changingPw.value = false
  }
}

onMounted(fetchMe)
</script>

<style scoped>
.user-profile-view {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.view-heading {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 600;
}

.profile-grid {
  display: grid;
  /* MH-5 (audit P1 #14 / instance of #3): pre-fix the `minmax(360px, 1fr)`
     could push the grid item past a 390px-wide viewport because 360px
     became a hard minimum, causing horizontal overflow on Profile when
     reached on a phone. Clamping the minimum with `min(360px, 100%)`
     keeps the column as wide as fits but never wider than the container —
     canonical responsive-grid pattern.   */
  grid-template-columns: repeat(auto-fit, minmax(min(360px, 100%), 1fr));
  gap: 1rem;
  /* Guard: descendant inputs that might still try to push the grid
     wider (PrimeVue InputText defaults to a content-based width on
     long values like an email address). min-width: 0 lets them shrink. */
  min-width: 0;
}
.profile-grid > * {
  min-width: 0;
}

.field-row {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-bottom: 1rem;
}

.field-row label {
  font-size: 0.85rem;
  color: var(--p-text-muted-color);
  font-weight: 500;
}

.actions-row {
  display: flex;
  justify-content: flex-end;
  margin-top: 0.5rem;
}

.text-muted {
  color: var(--p-text-muted-color);
}

.error-banner {
  background: var(--p-red-50, #fef2f2);
  color: var(--p-red-700, #b91c1c);
  border: 1px solid var(--p-red-200, #fecaca);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
}

.success-banner {
  background: var(--p-green-50, #f0fdf4);
  color: var(--p-green-700, #15803d);
  border: 1px solid var(--p-green-200, #bbf7d0);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
}

.inline-error {
  color: var(--p-red-700, #b91c1c);
  font-size: 0.85rem;
  margin-bottom: 0.5rem;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem;
}

:deep(.pw-input) {
  width: 100%;
}

:deep(.p-password) {
  width: 100%;
}
</style>
