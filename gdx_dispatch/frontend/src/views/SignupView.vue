<script setup>
import { ref } from 'vue'

const company = ref('')
const name = ref('')
const email = ref('')
const password = ref('')
const confirmPassword = ref('')
const error = ref('')
const submitting = ref(false)

async function handleSignup() {
  error.value = ''
  if (password.value.length < 8) { error.value = 'Password must be at least 8 characters.'; return }
  if (password.value !== confirmPassword.value) { error.value = 'Passwords do not match.'; return }
  submitting.value = true
  try {
    const resp = await fetch('/signup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        company_name: company.value,
        email: email.value,
        password: password.value,
        plan: 'professional',
      }),
    })
    const data = await resp.json()
    if (data.checkout_url) {
      window.location.href = data.checkout_url
    } else if (data.login_url) {
      window.location.href = data.login_url
    } else if (data.detail) {
      error.value = data.detail
    }
  } catch (e) {
    error.value = 'Something went wrong. Please try again.'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="signup-page">
    <div class="signup-card">
      <div class="header">
        <div class="logo">Team<span>GarageDoor</span></div>
        <h1>Start your free trial</h1>
        <p>14 days free. No credit card required.</p>
      </div>
      <form @submit.prevent="handleSignup" class="signup-form">
        <div class="field">
          <label>Company Name</label>
          <input v-model="company" type="text" placeholder="Smith Garage Doors" required />
        </div>
        <div class="field">
          <label>Your Name</label>
          <input v-model="name" type="text" placeholder="Doug Smith" required />
        </div>
        <div class="field">
          <label>Email</label>
          <input v-model="email" type="email" placeholder="doug@smithdoors.com" required />
        </div>
        <div class="field">
          <label>Password</label>
          <input v-model="password" type="password" placeholder="Min 8 characters" required minlength="8" />
        </div>
        <div class="field">
          <label>Confirm Password</label>
          <input v-model="confirmPassword" type="password" placeholder="Repeat password" required />
        </div>
        <button type="submit" class="submit-btn" :disabled="submitting">
          <span v-if="!submitting">Start Free Trial</span>
          <span v-else class="loader"></span>
        </button>
        <p v-if="error" class="error">{{ error }}</p>
        <p class="login-link">Already have an account? <router-link to="/login">Sign in</router-link></p>
      </form>
    </div>
  </div>
</template>

<style scoped>
.signup-page { min-height: 100vh; display: flex; align-items: center; justify-content: center; background: radial-gradient(ellipse at top left, #1e293b, #0f172a 70%); padding: 20px; }
.signup-card { width: 100%; max-width: 440px; background: #1e293b; color: #f8fafc; padding: 2.5rem; border-radius: 16px; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5); border: 1px solid rgba(255,255,255,0.06); }
.header { text-align: center; margin-bottom: 2rem; }
.logo { font-size: 1.3rem; font-weight: 800; margin-bottom: 1rem; }
.logo span { color: #3b82f6; }
h1 { font-size: 1.5rem; font-weight: 700; margin: 0 0 0.25rem; }
.header p { color: #94a3b8; font-size: 0.875rem; margin: 0; }
.signup-form { display: flex; flex-direction: column; gap: 1rem; }
.field label { display: block; font-size: 0.85rem; font-weight: 500; margin-bottom: 0.3rem; color: #94a3b8; }
.field input { width: 100%; padding: 0.7rem 0.85rem; background: #0f172a; border: 1px solid #334155; border-radius: 8px; color: #f8fafc; font-size: 0.9rem; box-sizing: border-box; }
.field input:focus { outline: none; border-color: #3b82f6; box-shadow: 0 0 0 3px rgba(59,130,246,0.15); }
.submit-btn { margin-top: 0.5rem; padding: 0.8rem; background: #3b82f6; color: #fff; border: none; border-radius: 8px; font-size: 1rem; font-weight: 600; cursor: pointer; display: flex; justify-content: center; }
.submit-btn:hover:not(:disabled) { background: #2563eb; }
.submit-btn:disabled { opacity: 0.6; cursor: not-allowed; }
.error { color: #f87171; font-size: 0.85rem; text-align: center; }
.login-link { text-align: center; font-size: 0.85rem; color: #94a3b8; margin-top: 0.5rem; }
.login-link a { color: #3b82f6; text-decoration: none; }
.loader { width: 20px; height: 20px; border: 2px solid rgba(255,255,255,0.3); border-radius: 50%; border-top-color: #fff; animation: spin 0.8s linear infinite; display: inline-block; }
@keyframes spin { to { transform: rotate(360deg); } }
</style>
