<script setup>
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useApiWithToast as useApi } from '../composables/useApiWithToast'

const router = useRouter()
const api = useApi()

const step = ref(1)
const loading = ref(false)
const error = ref('')

const company = ref({ name: '', phone: '', address: '', timezone: 'America/Chicago' })
const tech = ref({ name: '', email: '', phone: '', password: '' })
// A technician row must point at a real login (POST /api/technicians answers
// 400 "user_id is required"), so step 2 creates the user first. Both ids are
// kept: a retry after a failed technician POST must not POST the email again
// (409), and Back then Next must update the row it made, not add a second.
// A different email on a return trip is a different person: start over.
const techUserId = ref(null)
const techId = ref(null)
const techOwned = ref(false) // techId is a row this wizard made, not one it found
const techLoginMade = ref(false) // techUserId is a login this wizard created
const techEmail = ref('')
const needsPassword = computed(
  () => !techUserId.value || tech.value.email.trim().toLowerCase() !== techEmail.value,
)
const customerFile = ref(null)

const timezones = [
  'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
  'America/Phoenix', 'America/Anchorage', 'Pacific/Honolulu',
]

function onFileChange(e) { customerFile.value = e.target.files[0] }

async function nextStep() {
  error.value = ''
  loading.value = true
  try {
    if (step.value === 1) {
      // Company profile lives in the tenant settings row — POST onboarding/company
      // was never backed by a real endpoint, so the wizard silently no-op'd on
      // step 1. PATCH /api/settings accepts the same fields (company_name is the
      // canonical field name server-side).
      await api.patch('/api/settings', {
        company_name: company.value.name,
        phone: company.value.phone,
        address: company.value.address,
        timezone: company.value.timezone,
      })
    } else if (step.value === 2 && tech.value.name.trim()) {
      await addTechnician()
    }
    step.value++
  } catch (e) {
    error.value = e.message || 'Something went wrong'
  } finally {
    loading.value = false
  }
}

async function addTechnician() {
  const name = tech.value.name.trim()
  const email = tech.value.email.trim()
  if (!email) throw new Error('Email is required: it is the technician\'s login.')
  if (techEmail.value && techEmail.value !== email.toLowerCase()) {
    techUserId.value = null
    techId.value = null
    techOwned.value = false
    techLoginMade.value = false
  }
  if (!techUserId.value) {
    const login = await techLogin(name, email)
    techUserId.value = login.id
    techLoginMade.value = login.made
    techEmail.value = email.toLowerCase()
  }
  // The login may already have its technician row: the wizard run before, the
  // page reloaded after step 2, or a technician POST that committed but whose
  // response was lost. Leave that row as it is — blank wizard fields must not
  // erase what it holds — and add no twin. Looked up before every POST, so a
  // failed lookup runs again on retry instead of falling through. A row found
  // under a login this wizard created can only be the wizard's own.
  if (!techId.value) {
    techId.value = await technicianIdFor(techUserId.value)
    techOwned.value = techLoginMade.value
  }
  const row = { name, email, phone: tech.value.phone.trim() }
  if (techId.value) {
    if (techOwned.value) await api.patch(`/api/technicians/${techId.value}`, row)
    return
  }
  const created = await api.post('/api/technicians', {
    user_id: techUserId.value,
    name: row.name,
    email: row.email,
    phone: row.phone,
  })
  techId.value = created.id
  techOwned.value = true
}

// The id of the login this technician signs in with. A one-person shop's owner
// is its technician: their email already has a login (409), so the technician
// is attached to that user instead of stalling the step.
async function techLogin(name, email) {
  const password = tech.value.password
  if (password && password.length < 8) {
    throw new Error('Password must be at least 8 characters.')
  }
  let taken = false
  if (password) {
    try {
      const user = await api.post('/api/users', {
        name, email, password, role: 'technician',
      }, { suppressErrorToast: true })
      return { id: user.id, made: true }
    } catch (e) {
      if (e?.status !== 409) throw e
      taken = true
    }
  }
  const existing = await findUserByEmail(email)
  if (existing) return { id: existing.id, made: false }
  if (taken) throw new Error('That email already has a login, but it was not found in the user list.')
  throw new Error('Password must be at least 8 characters.')
}

async function technicianIdFor(userId) {
  // include_verification: the list otherwise hides rows named "Technician".
  const rows = await api.get('/api/technicians?include_verification=true')
  const list = Array.isArray(rows) ? rows : rows?.items || []
  return list.find((t) => String(t.user_id) === String(userId))?.id || null
}

async function findUserByEmail(email) {
  const users = await api.get('/api/users?limit=500')
  const want = email.toLowerCase()
  return (Array.isArray(users) ? users : []).find((u) => (u.email || '').toLowerCase() === want)
}

async function finish() {
  error.value = ''
  loading.value = true
  try {
    if (customerFile.value) {
      const fd = new FormData()
      fd.append('file', customerFile.value)
      // /api/customers/import was never routed; the real admin CSV/JSON
      // import handler lives at /api/admin/import/customers (admin_ops.py).
      // Must go through the api client: the raw fetch here sent
      // `Bearer ${auth.token}` — a field the auth store doesn't have
      // (it exports accessToken) — so every wizard import 401'd, and the
      // missing resp.ok check let onboarding "complete" with 0 customers.
      // api.post throws on non-2xx, which keeps us in this catch block
      // instead of marking onboarding complete.
      await api.post('/api/admin/import/customers', fd)
    }
    await api.post('/api/onboarding/complete', {})
    router.push('/dashboard')
  } catch (e) {
    error.value = e.message || 'Something went wrong'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="onboarding">
    <div class="steps">
      <div v-for="s in [{n:1,l:'Company'},{n:2,l:'Technician'},{n:3,l:'Customers'}]" :key="s.n"
           class="step-dot" :class="{ active: step === s.n, done: step > s.n }">
        <div class="dot">{{ step > s.n ? '\u2713' : s.n }}</div>
        <span>{{ s.l }}</span>
      </div>
    </div>

    <div class="card">
      <p v-if="error" class="error">{{ error }}</p>

      <div v-if="step === 1">
        <h2>Set up your company</h2>
        <div class="field"><label>Company Name</label><input v-model="company.name" placeholder="Smith Garage Doors" /></div>
        <div class="field"><label>Phone</label><input v-model="company.phone" type="tel" placeholder="(555) 123-4567" /></div>
        <div class="field"><label>Address</label><input v-model="company.address" placeholder="123 Main St, City, ST" /></div>
        <div class="field">
          <label>Timezone</label>
          <select v-model="company.timezone">
            <option v-for="tz in timezones" :key="tz" :value="tz">{{ tz }}</option>
          </select>
        </div>
        <div class="actions"><button @click="nextStep" :disabled="loading" class="btn-primary">Next</button></div>
      </div>

      <div v-if="step === 2">
        <h2>Add your first technician</h2>
        <p class="hint">You can skip this and add techs later.</p>
        <div class="field"><label>Name</label><input v-model="tech.name" placeholder="John Smith" /></div>
        <div class="field"><label>Email</label><input v-model="tech.email" type="email" placeholder="john@company.com" /></div>
        <div class="field"><label>Phone</label><input v-model="tech.phone" type="tel" placeholder="(555) 987-6543" /></div>
        <div v-if="needsPassword" class="field">
          <label>Password</label>
          <input v-model="tech.password" type="password" autocomplete="new-password" placeholder="At least 8 characters" />
        </div>
        <p class="hint">This also creates the technician's login, with the email and password above. If that email already has a login (yours, say), the technician is attached to it and the password is not used.</p>
        <div class="actions">
          <button @click="step--" class="btn-back">Back</button>
          <button @click="step++" class="btn-skip">Skip</button>
          <button @click="nextStep" :disabled="loading" class="btn-primary">Next</button>
        </div>
      </div>

      <div v-if="step === 3">
        <h2>Import customers</h2>
        <p class="hint">Upload a CSV or add customers manually later from the dashboard.</p>
        <div class="upload-zone" @click="$refs.fileInput.click()">
          <div v-if="!customerFile">Drop CSV here or click to browse</div>
          <div v-else class="file-selected">{{ customerFile.name }}</div>
          <input ref="fileInput" type="file" accept=".csv" @change="onFileChange" style="display:none" />
        </div>
        <div class="actions">
          <button @click="step--" class="btn-back">Back</button>
          <button @click="finish" :disabled="loading" class="btn-primary">
            {{ loading ? 'Setting up...' : 'Launch Dashboard' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.onboarding {
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: var(--surface-app);
  color: var(--text-primary);
  padding: 2rem;
}
.steps { display: flex; gap: 3rem; margin-bottom: 2rem; }
.step-dot { display: flex; flex-direction: column; align-items: center; opacity: 0.35; transition: opacity 0.3s; }
.step-dot.active, .step-dot.done { opacity: 1; }
.dot {
  width: 36px; height: 36px; border-radius: 50%;
  background: var(--surface-elevated);
  display: flex; align-items: center; justify-content: center;
  font-weight: 700; margin-bottom: 0.4rem;
}
.active .dot { background: var(--interactive-primary); color: #fff; }
.done .dot { background: var(--p-green-700); color: #fff; }
.step-dot span { font-size: 0.8rem; color: var(--text-muted); }
.card {
  background: var(--surface-panel, var(--surface-elevated));
  border: 1px solid var(--border-subtle);
  border-radius: 16px;
  padding: 2.5rem;
  width: 100%;
  max-width: 460px;
  box-shadow: var(--shadow-lg);
}
h2 { font-size: 1.4rem; margin: 0 0 1.5rem; text-align: center; }
.hint { color: var(--text-muted); font-size: 0.85rem; text-align: center; margin-bottom: 1.5rem; }
.field { margin-bottom: 1rem; }
.field label {
  display: block;
  font-size: 0.82rem;
  color: var(--text-muted);
  margin-bottom: 0.3rem;
  font-weight: 500;
}
.field input, .field select {
  width: 100%;
  padding: 0.7rem 0.85rem;
  background: var(--surface-app);
  border: 1px solid var(--border-strong);
  border-radius: 8px;
  color: var(--text-primary);
  font-size: 0.9rem;
  box-sizing: border-box;
}
.field input:focus, .field select:focus {
  outline: none;
  border-color: var(--interactive-primary);
}
.upload-zone {
  border: 2px dashed var(--border-strong);
  border-radius: 12px;
  padding: 2rem;
  text-align: center;
  cursor: pointer;
  color: var(--text-muted);
  margin-bottom: 1rem;
}
.upload-zone:hover { border-color: var(--interactive-primary); }
.file-selected { color: var(--p-green-500, #22c55e); font-weight: 600; }
.actions { display: flex; gap: 0.75rem; justify-content: flex-end; margin-top: 1.5rem; }
button {
  padding: 0.7rem 1.5rem;
  border-radius: 8px;
  font-weight: 600;
  font-size: 0.9rem;
  cursor: pointer;
  border: none;
}
.btn-primary { background: var(--interactive-primary); color: #fff; }
.btn-primary:hover:not(:disabled) { filter: brightness(1.1); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-back { background: var(--surface-elevated); color: var(--text-muted); }
.btn-skip { background: transparent; color: var(--text-muted); }
/* An error banner has to read as an error, so the text stays red — but no
   single red clears AA on both tints (#dc2626 is 4.01:1 on the light one,
   #f87171 is 3.37:1 on the dark one). Pin each theme to its own red, the way
   MobileTimeclockView's banners do: 5.37:1 light, 5.97:1 dark. The
   prefers-color-scheme block is the fallback for first paint, before the
   theme store has stamped data-theme onto <html>. */
.error {
  color: #b91c1c;
  font-size: 0.85rem;
  text-align: center;
  margin-bottom: 1rem;
  background: color-mix(in srgb, #dc2626 12%, var(--p-content-background));
  border: 1px solid color-mix(in srgb, #dc2626 45%, var(--p-content-background));
  padding: 0.5rem;
  border-radius: 6px;
}
[data-theme="dark"] .error {
  color: #ff6d77;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .error {
    color: #ff6d77;
  }
}
</style>
