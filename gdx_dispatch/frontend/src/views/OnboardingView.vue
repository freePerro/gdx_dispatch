<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import { useApiWithToast as useApi } from '../composables/useApiWithToast'

const router = useRouter()
const auth = useAuthStore()
const api = useApi()

const step = ref(1)
const loading = ref(false)
const error = ref('')

const company = ref({ name: '', phone: '', address: '', timezone: 'America/Chicago' })
const tech = ref({ name: '', email: '', phone: '' })
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
    } else if (step.value === 2 && tech.value.name) {
      await api.post('/api/technicians', tech.value)
    }
    step.value++
  } catch (e) {
    error.value = e.message || 'Something went wrong'
  } finally {
    loading.value = false
  }
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
      await fetch('/api/admin/import/customers', {
        method: 'POST',
        headers: { Authorization: `Bearer ${auth.token}` },
        body: fd,
      })
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
.done .dot { background: var(--p-green-500, #22c55e); color: #fff; }
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
.error {
  color: var(--p-red-400, #f87171);
  font-size: 0.85rem;
  text-align: center;
  margin-bottom: 1rem;
  background: var(--p-red-50, rgba(248, 113, 113, 0.1));
  padding: 0.5rem;
  border-radius: 6px;
}
</style>
