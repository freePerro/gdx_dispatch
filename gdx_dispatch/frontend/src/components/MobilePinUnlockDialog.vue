<script setup>
import { ref, watch, onMounted } from 'vue';
import {
  verifyQuickPin,
  checkBiometricAvailability,
  isBiometricEnabled,
  performBiometricUnlock,
} from '../lib/nativeAuth';

const props = defineProps({
  visible: {
    type: Boolean,
    default: false,
  },
  autoPromptBiometric: {
    type: Boolean,
    default: true,
  },
});

const emit = defineEmits(['update:visible', 'unlocked', 'forgotPassword']);

const enteredDigits = ref('');
const errorMsg = ref('');
const verifying = ref(false);
const biometricAvailable = ref(false);

async function checkBiometrics() {
  if (isBiometricEnabled()) {
    const res = await checkBiometricAvailability();
    biometricAvailable.value = Boolean(res.available);
  } else {
    biometricAvailable.value = false;
  }
}

async function triggerBiometric() {
  if (!biometricAvailable.value) return;
  const res = await performBiometricUnlock();
  if (res.success) {
    emit('unlocked');
    emit('update:visible', false);
  } else if (!res.cancelled && res.error) {
    errorMsg.value = 'Fingerprint not recognized. Enter PIN.';
  }
}

watch(
  () => props.visible,
  async (isOpen) => {
    if (isOpen) {
      enteredDigits.value = '';
      errorMsg.value = '';
      await checkBiometrics();
      if (props.autoPromptBiometric && biometricAvailable.value) {
        setTimeout(triggerBiometric, 200);
      }
    }
  },
  { immediate: true }
);

async function handleDigit(d) {
  if (verifying.value) return;
  if (enteredDigits.value.length < 4) {
    enteredDigits.value += String(d);
    errorMsg.value = '';
    if (enteredDigits.value.length === 4) {
      await attemptUnlock();
    }
  }
}

function handleBackspace() {
  if (verifying.value) return;
  if (enteredDigits.value.length > 0) {
    enteredDigits.value = enteredDigits.value.slice(0, -1);
    errorMsg.value = '';
  }
}

function handleClear() {
  if (verifying.value) return;
  enteredDigits.value = '';
  errorMsg.value = '';
}

async function attemptUnlock() {
  verifying.value = true;
  try {
    const success = await verifyQuickPin(enteredDigits.value);
    if (success) {
      emit('unlocked');
      emit('update:visible', false);
    } else {
      errorMsg.value = 'Incorrect PIN. Try again.';
      setTimeout(() => {
        enteredDigits.value = '';
      }, 400);
    }
  } finally {
    verifying.value = false;
  }
}

function handleForgot() {
  emit('forgotPassword');
  emit('update:visible', false);
}
</script>

<template>
  <div
    v-if="visible"
    class="pin-overlay"
    role="dialog"
    aria-modal="true"
    aria-labelledby="pin-modal-title"
    data-test="mobile-pin-dialog"
  >
    <div class="pin-modal">
      <div class="pin-header">
        <div class="icon-bubble">
          <i :class="biometricAvailable ? 'pi pi-fingerprint' : 'pi pi-lock'" aria-hidden="true" />
        </div>
        <h2 id="pin-modal-title">Unlock GDX Dispatch</h2>
        <p class="pin-sub">
          {{ biometricAvailable ? 'Use fingerprint or enter your 4-digit PIN' : 'Enter your 4-digit mobile PIN to resume' }}
        </p>
      </div>

      <div class="pin-dots" data-test="pin-dots">
        <span
          v-for="i in 4"
          :key="i"
          class="dot"
          :class="{ filled: enteredDigits.length >= i }"
        />
      </div>

      <div v-if="errorMsg" class="error-msg" data-test="pin-error">
        {{ errorMsg }}
      </div>

      <!-- Quick Fingerprint Action Button -->
      <button
        v-if="biometricAvailable"
        type="button"
        class="btn-biometric"
        data-test="btn-biometric-unlock"
        @click="triggerBiometric"
      >
        <i class="pi pi-fingerprint" />
        <span>Scan Fingerprint</span>
      </button>

      <div class="keypad">
        <button
          v-for="n in 9"
          :key="n"
          type="button"
          class="key-btn"
          :data-test="`key-${n}`"
          @click="handleDigit(n)"
        >
          {{ n }}
        </button>

        <button
          type="button"
          class="key-btn action-key"
          data-test="key-clear"
          @click="handleClear"
        >
          C
        </button>

        <button
          type="button"
          class="key-btn"
          data-test="key-0"
          @click="handleDigit(0)"
        >
          0
        </button>

        <button
          type="button"
          class="key-btn action-key"
          data-test="key-backspace"
          @click="handleBackspace"
        >
          <i class="pi pi-arrow-left" />
        </button>
      </div>

      <div class="forgot-wrapper">
        <button
          type="button"
          class="btn-forgot"
          data-test="btn-forgot-pin"
          @click="handleForgot"
        >
          Forgot PIN? Log in with password
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.pin-overlay {
  position: fixed;
  inset: 0;
  z-index: 99999;
  background-color: #0b1329;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 1.5rem;
}

.pin-modal {
  width: 100%;
  max-width: 22rem;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.pin-header {
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-bottom: 1.25rem;
}

.icon-bubble {
  width: 3.5rem;
  height: 3.5rem;
  border-radius: 50%;
  background-color: rgba(37, 99, 235, 0.2);
  color: #60a5fa;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.5rem;
  margin-bottom: 0.75rem;
}

.pin-header h2 {
  font-size: 1.35rem;
  font-weight: 700;
  color: #f9fafb;
  margin: 0 0 0.35rem;
}

.pin-sub {
  font-size: 0.85rem;
  color: #9ca3af;
  margin: 0;
  text-align: center;
}

.pin-dots {
  display: flex;
  gap: 1.25rem;
  margin-bottom: 1.25rem;
}

.dot {
  width: 1rem;
  height: 1rem;
  border-radius: 50%;
  border: 2px solid #4b5563;
  transition: all 0.15s ease;
}

.dot.filled {
  background-color: #3b82f6;
  border-color: #3b82f6;
  transform: scale(1.1);
}

.error-msg {
  color: #ef4444;
  font-size: 0.875rem;
  font-weight: 500;
  margin-bottom: 1rem;
}

.btn-biometric {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.5rem;
  padding: 0.625rem 1.25rem;
  background-color: #1e3a8a;
  color: #bfdbfe;
  border: 1px solid #3b82f6;
  border-radius: 9999px;
  font-size: 0.875rem;
  font-weight: 600;
  cursor: pointer;
  margin-bottom: 1.25rem;
  transition: background-color 0.15s ease;
}

.btn-biometric:active {
  background-color: #1d4ed8;
}

.keypad {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 0.875rem;
  width: 100%;
  margin-bottom: 1.25rem;
}

.key-btn {
  height: 3.75rem;
  border-radius: 50%;
  background-color: #1f2937;
  color: #f9fafb;
  border: 1px solid #374151;
  font-size: 1.5rem;
  font-weight: 600;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background-color 0.1s ease;
}

.key-btn:active {
  background-color: #374151;
}

.action-key {
  font-size: 1.15rem;
  color: #9ca3af;
}

.forgot-wrapper {
  margin-top: 0.25rem;
}

.btn-forgot {
  background: transparent;
  border: none;
  color: #60a5fa;
  font-size: 0.875rem;
  cursor: pointer;
  padding: 0.5rem;
}
</style>
