<script setup>
import { ref } from 'vue'

const email = ref('')
const sent = ref(false)
const error = ref('')
const submitting = ref(false)

async function handleSubmit() {
  error.value = ''
  const trimmed = email.value.trim()
  if (!trimmed) { error.value = 'Please enter your email address'; return }
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)) { error.value = 'Please enter a valid email address'; return }
  submitting.value = true
  try {
    const resp = await fetch('/auth/forgot-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: email.value }),
    })
    const data = await resp.json()
    if (!resp.ok) throw new Error(data.detail || 'Something went wrong')
    sent.value = true
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Something went wrong'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="forgot-page">
    <div class="forgot-card">
      <div v-if="!sent">
        <h1>Reset Password</h1>
        <p class="subtitle">Enter your email and we'll send you a reset link.</p>

        <form @submit.prevent="handleSubmit" class="forgot-form">
          <div class="input-group">
            <label for="forgot-email">Email</label>
            <input
              id="forgot-email"
              v-model="email"
              type="email"
              placeholder="name@company.com"
              required
              autofocus
            />
          </div>
          <button type="submit" class="submit-btn" :disabled="submitting">
            <span v-if="!submitting">Send Reset Link</span>
            <span v-else class="loader"></span>
          </button>
          <p v-if="error" class="error-message">{{ error }}</p>
        </form>

        <p class="back-link"><router-link to="/login">Back to Sign In</router-link></p>
      </div>

      <div v-else class="success">
        <div class="success-icon">&#9993;</div>
        <h2>Check Your Email</h2>
        <p>If an account exists for <strong>{{ email }}</strong>, we've sent a password reset link.</p>
        <p class="hint">Didn't receive it? Check your spam folder or try again in a few minutes.</p>
        <router-link to="/login" class="submit-btn" style="display:inline-block;text-align:center;text-decoration:none;margin-top:1rem;">Back to Sign In</router-link>
      </div>
    </div>
  </div>
</template>

<style scoped>
.forgot-page { min-height: 100vh; display: flex; align-items: center; justify-content: center; background: radial-gradient(ellipse at top left, #1e293b, #0f172a 70%); padding: 20px; }
.forgot-card { width: 100%; max-width: 420px; background: var(--card, #1e293b); color: var(--text, #f8fafc); border-radius: 16px; padding: 2.5rem; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5); }
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
.back-link { text-align: center; margin-top: 1rem; font-size: 0.85rem; }
.back-link a { color: #3b82f6; text-decoration: none; }
.success { text-align: center; }
.success-icon { font-size: 3rem; margin-bottom: 1rem; }
.success h2 { margin-bottom: 0.5rem; }
.success p { color: #94a3b8; font-size: 0.9rem; margin-bottom: 0.5rem; }
.hint { font-size: 0.8rem; color: #64748b; }
.loader { width: 20px; height: 20px; border: 2px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: #fff; animation: spin 0.8s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
