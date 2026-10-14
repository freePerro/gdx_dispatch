<script setup>
import { ref, watch, onMounted } from 'vue';
import {
  getServerUrl,
  setServerUrl,
  testServerConnection,
  PRESETS,
} from '../lib/serverConfig';

const props = defineProps({
  visible: {
    type: Boolean,
    default: false,
  },
});

const emit = defineEmits(['update:visible', 'saved']);

const serverInput = ref('');
const testing = ref(false);
const testResult = ref(null);
const saving = ref(false);

function syncFromStorage() {
  serverInput.value = getServerUrl();
  testResult.value = null;
}

watch(
  () => props.visible,
  (isOpen) => {
    if (isOpen) {
      syncFromStorage();
    }
  },
  { immediate: true }
);

function selectPreset(url) {
  serverInput.value = url;
  testResult.value = null;
}

async function handleTest() {
  if (!serverInput.value.trim()) {
    testResult.value = { ok: false, message: 'Please enter a server URL first' };
    return;
  }
  testing.value = true;
  testResult.value = null;
  try {
    const res = await testServerConnection(serverInput.value);
    testResult.value = res;
  } finally {
    testing.value = false;
  }
}

async function handleSave() {
  saving.value = true;
  try {
    const saved = await setServerUrl(serverInput.value);
    emit('saved', saved);
    emit('update:visible', false);
  } finally {
    saving.value = false;
  }
}

function handleReset() {
  serverInput.value = '';
  testResult.value = null;
}

function close() {
  emit('update:visible', false);
}
</script>

<template>
  <div v-if="visible" class="server-config-mask" @click.self="close">
    <div class="server-config-dialog" role="dialog" aria-modal="true" aria-labelledby="server-cfg-title">
      <div class="dialog-header">
        <div class="title-wrap">
          <i class="pi pi-server header-icon"></i>
          <h2 id="server-cfg-title">Server Settings</h2>
        </div>
        <button type="button" class="close-btn" @click="close" aria-label="Close">
          <i class="pi pi-times"></i>
        </button>
      </div>

      <div class="dialog-body">
        <p class="description">
          Set the GDX Dispatch instance URL to connect this mobile app to your company server.
        </p>

        <div class="input-group">
          <label for="server-url-input">Server Address</label>
          <div class="input-wrap">
            <i class="pi pi-globe field-icon"></i>
            <input
              id="server-url-input"
              v-model="serverInput"
              type="url"
              placeholder="https://dispatch.example.com"
              autocomplete="off"
              autocorrect="off"
              autocapitalize="off"
              spellcheck="false"
              data-testid="server-url-input"
            />
          </div>
        </div>

        <div class="presets-section">
          <span class="presets-label">Quick Presets:</span>
          <div class="preset-chips">
            <button
              v-for="p in PRESETS"
              :key="p.url"
              type="button"
              class="preset-chip"
              @click="selectPreset(p.url)"
            >
              {{ p.label }}
            </button>
            <button
              type="button"
              class="preset-chip chip-clear"
              @click="handleReset"
            >
              Clear / Relative
            </button>
          </div>
        </div>

        <div v-if="testResult" class="test-feedback" :class="{ 'is-ok': testResult.ok, 'is-error': !testResult.ok }">
          <i :class="testResult.ok ? 'pi pi-check-circle' : 'pi pi-exclamation-triangle'"></i>
          <span>{{ testResult.message }} {{ testResult.status ? `(status: ${testResult.status})` : '' }}</span>
        </div>
      </div>

      <div class="dialog-footer">
        <button
          type="button"
          class="btn btn-secondary"
          :disabled="testing || saving"
          @click="handleTest"
        >
          <i v-if="testing" class="pi pi-spin pi-spinner"></i>
          <span v-else>Test Connection</span>
        </button>

        <div class="footer-right">
          <button
            type="button"
            class="btn btn-subtle"
            @click="close"
          >
            Cancel
          </button>
          <button
            type="button"
            class="btn btn-primary"
            :disabled="saving"
            @click="handleSave"
            data-testid="server-save-btn"
          >
            <span v-if="!saving">Save & Connect</span>
            <span v-else class="loader"></span>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.server-config-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.7);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: 1rem;
  backdrop-filter: blur(4px);
}

.server-config-dialog {
  width: 100%;
  max-width: 480px;
  background: var(--surface-card, #1e293b);
  color: var(--text-color, #f8fafc);
  border-radius: 12px;
  box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5);
  border: 1px solid rgba(255, 255, 255, 0.08);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.dialog-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1.25rem 1.5rem;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.title-wrap {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.header-icon {
  font-size: 1.25rem;
  color: var(--p-primary-color);
}

.dialog-header h2 {
  font-size: 1.2rem;
  font-weight: 600;
  margin: 0;
}

.close-btn {
  background: none;
  border: none;
  color: var(--text-muted-color, #94a3b8);
  cursor: pointer;
  padding: 0.4rem;
  font-size: 1rem;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  transition: background 0.15s;
}

.close-btn:hover {
  background: rgba(255, 255, 255, 0.06);
  color: #fff;
}

.dialog-body {
  padding: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
}

.description {
  margin: 0;
  font-size: 0.875rem;
  color: var(--text-muted-color, #94a3b8);
  line-height: 1.4;
}

.input-group {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.input-group label {
  font-size: 0.85rem;
  font-weight: 500;
}

.input-wrap {
  position: relative;
  display: flex;
  align-items: center;
}

.field-icon {
  position: absolute;
  left: 12px;
  color: #64748b;
  pointer-events: none;
}

input {
  width: 100%;
  padding: 0.75rem 0.75rem 0.75rem 2.4rem;
  background: #0f172a;
  border: 1px solid #334155;
  border-radius: 8px;
  color: #f8fafc;
  font: inherit;
  font-size: 1rem;
  transition: border-color 0.2s, box-shadow 0.2s;
}

input:focus {
  outline: none;
  border-color: #3b82f6;
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.2);
}

.presets-section {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.presets-label {
  font-size: 0.75rem;
  color: #94a3b8;
  font-weight: 500;
}

.preset-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.preset-chip {
  padding: 0.35rem 0.65rem;
  background: #334155;
  border: 1px solid rgba(255, 255, 255, 0.08);
  border-radius: 6px;
  color: #e2e8f0;
  font-size: 0.75rem;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
}

.preset-chip:hover {
  background: #475569;
  border-color: #3b82f6;
}

.chip-clear {
  background: transparent;
  border-style: dashed;
  color: #94a3b8;
}

.test-feedback {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.75rem;
  border-radius: 8px;
  font-size: 0.85rem;
}

.test-feedback.is-ok {
  background: rgba(34, 197, 94, 0.12);
  border: 1px solid rgba(34, 197, 94, 0.3);
  color: #4ade80;
}

.test-feedback.is-error {
  background: rgba(239, 68, 68, 0.12);
  border: 1px solid rgba(239, 68, 68, 0.3);
  color: #f87171;
}

.dialog-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1.25rem 1.5rem;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  background: rgba(0, 0, 0, 0.15);
}

.footer-right {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.btn {
  padding: 0.6rem 1rem;
  border-radius: 6px;
  font: inherit;
  font-size: 0.875rem;
  font-weight: 500;
  cursor: pointer;
  border: none;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  transition: opacity 0.15s, background 0.15s;
}

.btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn-primary {
  background: #2563eb;
  color: #fff;
}

.btn-primary:hover:not(:disabled) {
  background: #1d4ed8;
}

.btn-secondary {
  background: #334155;
  color: #e2e8f0;
}

.btn-secondary:hover:not(:disabled) {
  background: #475569;
}

.btn-subtle {
  background: transparent;
  color: #94a3b8;
}

.btn-subtle:hover:not(:disabled) {
  color: #fff;
}

.loader {
  width: 16px;
  height: 16px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-radius: 50%;
  border-top-color: #fff;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
