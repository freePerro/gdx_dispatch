<script setup>
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const token = ref('')
const password = ref('')
const confirmPassword = ref('')
const done = ref(false)
const error = ref('')
const submitting = ref(false)

onMounted(() => {
  token.value = route.query.token || ''
  if (!token.value) error.value = 'Missing reset token. Please use the link from your email.'
})

async function handleSubmit() {
  error.value = ''
  if (password.value.length < 8) { error.value = 'Password must be at least 8 characters.'; return }
  if (password.value !== confirmPassword.value) { error.value = 'Passwords do not match.'; return }
  submitting.value = true
  try {
    const resp = await fetch('/auth/reset-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token.value, new_password: password.value }),
    })
    const data = await resp.json()
    if (!resp.ok) throw new Error(data.detail || 'Reset failed')
    done.value = true
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Reset failed. The link may have expired.'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="reset-page">
    <div class="reset-card">
      <div v-if="!done">
        <h1>Set New Password</h1>
        <p class="subtitle">Choose a strong password for your account.</p>

        <form @submit.prevent="handleSubmit" class="reset-form">
          <div class="input-group">
            <label for="new-password">New Password</label>
            <input id="new-password" v-model="password" type="password" placeholder="At least 8 characters" required autofocus />
          </div>
          <div class="input-group">
            <label for="confirm-password">Confirm Password</label>
            <input id="confirm-password" v-model="confirmPassword" type="password" placeholder="Repeat password" required />
          </div>
          <button type="submit" class="submit-btn" :disabled="submitting || !token">
            <span v-if="!submitting">Reset Password</span>
            <span v-else class="loader"></span>
          </button>
          <p v-if="error" class="error-message">{{ error }}</p>
        </form>
      </div>

      <div v-else class="success">
        <div class="success-icon">&#10003;</div>
        <h2>Password Reset</h2>
        <p>Your password has been updated. You can now sign in.</p>
        <router-link to="/login" class="submit-btn" style="display:inline-block;text-align:center;text-decoration:none;margin-top:1rem;">Sign In</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.reset-page { min-height: 100vh; display: flex; align-items: center; justify-content: center; background: radial-gradient(ellipse at top left, #1e293b, #0f172a 70%); padding: 20px; }
.reset-card { width: 100%; max-width: 420px; background: var(--card, #1e293b); color: var(--text, #f8fafc); border-radius: 16px; padding: 2.5rem; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5); }
h1 { font-size: 1.5rem; margin-bottom: 0.25rem; }
.subtitle { color: #94a3b8; font-size: 0.9rem; margin-bottom: 1.5rem; }
.input-group { margin-bottom: 1rem; }
.input-group label { display: block; font-size: 0.85rem; color: #94a3b8; margin-bottom: 0.35rem; font-weight: 500; }
input { width: 100%; padding: 0.75rem; background: #0f172a; border: 1px solid #334155; border-radius: 8px; color: #f8fafc; font-size: 0.95rem; box-sizing: border-box; }
input:focus { outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15); }
.submit-btn { width: 100%; padding: 0.75rem; background: #3b82f6; color: white; border: none; border-radius: 8px; font-size: 0.95rem; font-weight: 600; cursor: pointer; }
.submit-btn:hover { background: #2563eb; }
.submit-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.error-message { color: #f87171; font-size: 0.85rem; text-align: center; margin-top: 0.75rem; }
.success { text-align: center; }
.success-icon { font-size: 3rem; color: #22c55e; margin-bottom: 1rem; }
.success h2 { margin-bottom: 0.5rem; }
.success p { color: #94a3b8; font-size: 0.9rem; }
.loader { width: 20px; height: 20px; border: 2px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: #fff; animation: spin 0.8s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
