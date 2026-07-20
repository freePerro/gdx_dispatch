<template>
  <div class="browser-stream" data-testid="browser-stream">
    <div class="browser-stream__bar">
      <span :class="['browser-stream__dot', connected ? 'is-on' : 'is-off']" />
      <span>{{ connected ? 'Connected' : 'Connecting…' }}</span>
      <!-- Folder picker (ADR-013): pick an existing folder or type a new one;
           captures are filed under it so they don't pile into one flat list. -->
      <Select
        v-if="foldersEndpoint"
        v-model="folder"
        :options="folderOptions"
        editable
        size="small"
        placeholder="Folder…"
        class="browser-stream__folder"
        data-testid="browser-folder"
      />
      <Button
        v-if="captureEndpoint"
        :label="captureLabel"
        size="small"
        :loading="capturing"
        :disabled="!connected"
        @click="onCapture"
      />
      <Button
        :label="credsSaved ? 'Login remembered' : 'Remember login'"
        :icon="credsSaved ? 'pi pi-check' : 'pi pi-key'"
        size="small"
        severity="secondary"
        outlined
        data-testid="browser-creds-btn"
        @click="credsOpen = true"
      />
      <span v-if="error" class="browser-stream__error">{{ error }}</span>
    </div>

    <!-- Remembered sign-in: stored encrypted server-side and auto-filled into
         the remote site's login form. Fill only — the operator still clicks
         the site's own Sign in button. -->
    <Dialog
      v-model:visible="credsOpen"
      modal
      header="Remember sign-in"
      :style="{ width: '24rem' }"
      data-testid="browser-creds-dialog"
    >
      <p class="browser-stream__creds-hint">
        Saved securely on your server and typed into the site's sign-in form
        for you — you just click its Sign&nbsp;in button.
      </p>
      <div class="browser-stream__creds-form">
        <InputText
          v-model="credsUsername"
          placeholder="Username / email"
          autocomplete="off"
          data-testid="browser-creds-user"
        />
        <Password
          v-model="credsPassword"
          :feedback="false"
          toggle-mask
          :placeholder="credsSaved ? '••••••• (unchanged)' : 'Password'"
          input-class="browser-stream__creds-pw"
          data-testid="browser-creds-pass"
        />
      </div>
      <template #footer>
        <Button
          v-if="credsSaved"
          label="Forget"
          severity="danger"
          text
          data-testid="browser-creds-forget"
          @click="onForgetCreds"
        />
        <Button
          label="Save"
          :loading="credsSaving"
          :disabled="!credsUsername && !credsPassword"
          data-testid="browser-creds-save"
          @click="onSaveCreds"
        />
      </template>
    </Dialog>

    <img
      ref="screen"
      class="browser-stream__screen"
      :src="frameSrc || transparentPixel"
      draggable="false"
      alt="Remote browser"
      @mousedown.prevent="(e) => { focusKeyboard(); mouse('mousedown', e, screen); }"
      @mouseup.prevent="(e) => mouse('mouseup', e, screen)"
      @mousemove.prevent="(e) => mouse('mousemove', e, screen)"
      @wheel.prevent="(e) => wheel(e, screen)"
    />

    <!-- All typing lands here, not on the <img>: only a real editable element
         summons the phone's on-screen keyboard, so tapping the screen focuses
         this visually-hidden input. Desktop keydowns are preventDefaulted and
         sent as key/text events; soft-keyboard (IME) edits slip past keydown and
         are mirrored to the remote page by diffing the input's value. -->
    <input
      ref="kbd"
      class="browser-stream__kbd"
      type="text"
      autocomplete="off"
      autocapitalize="none"
      autocorrect="off"
      spellcheck="false"
      aria-label="Remote browser keyboard input"
      @keydown="(e) => key('keydown', e)"
      @keyup="(e) => key('keyup', e)"
      @input="(e) => imeInput(e, kbd)"
      @compositionstart="compositionStart"
      @compositionend="compositionEnd"
      @paste.prevent="(e) => paste(e)"
      @focus="() => seedKeyboard(kbd)"
    />
  </div>
</template>

<script setup>
// Phase 2 (ADR-014): streamed headless browser the operator drives, e.g. to log
// into a no-API site. Pixels in, input out — the remote site never executes
// here. All logic is in useBrowserStream so it unit-tests; this is the template.
import { onMounted, onBeforeUnmount, ref } from 'vue';
import Button from 'primevue/button';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import Password from 'primevue/password';
import Select from 'primevue/select';
import { useApiWithToast } from '../composables/useApiWithToast';
import { useBrowserStream } from '../composables/useBrowserStream';

const props = defineProps({
  pluginKey: { type: String, required: true },
  url: { type: String, required: true },
  // When a screen declares a capture endpoint, show a button that ships the live
  // page's text+URL to it (the plugin extracts the structured data server-side).
  captureEndpoint: { type: String, default: '' },
  captureLabel: { type: String, default: 'Capture this page' },
  // When set, show a folder picker (existing folders from this endpoint, or type
  // a new one); the chosen folder rides along with each capture.
  foldersEndpoint: { type: String, default: '' },
});
const emit = defineEmits(['captured']);

const transparentPixel =
  'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';

const api = useApiWithToast();
const screen = ref(null);
const kbd = ref(null);
const capturing = ref(false);
const folder = ref('');
const folderOptions = ref([]);
const {
  frameSrc, connected, error, connect, mouse, wheel, key, paste,
  imeInput, seedKeyboard, compositionStart, compositionEnd, capturePage, disconnect,
} = useBrowserStream();

// @mousedown.prevent suppresses the focus a press normally gives, so focus the
// keyboard input explicitly — synchronously, inside the tap's event handler,
// or mobile browsers refuse to open the on-screen keyboard.
function focusKeyboard() {
  if (kbd.value) kbd.value.focus();
}

async function loadFolders() {
  if (!props.foldersEndpoint) return;
  try {
    folderOptions.value = (await api.get(props.foldersEndpoint)) || [];
  } catch {
    folderOptions.value = [];
  }
}

// ---- Remembered sign-in (login autofill) ----
const credsOpen = ref(false);
const credsSaved = ref(false);
const credsSaving = ref(false);
const credsUsername = ref('');
const credsPassword = ref('');
const credsUrl = `/api/plugins/_browser/credentials?key=${encodeURIComponent(props.pluginKey)}`;

async function loadCredsStatus() {
  try {
    const s = await api.get(credsUrl);
    credsSaved.value = !!s?.saved;
    credsUsername.value = s?.username || '';
  } catch {
    credsSaved.value = false; // non-owner / no consent — dialog will just 403 on save
  }
}

async function onSaveCreds() {
  credsSaving.value = true;
  try {
    // Blank password on an existing save keeps the stored one (the placeholder
    // says "unchanged"); Forget is the only way to clear.
    await api.post('/api/plugins/_browser/credentials', {
      key: props.pluginKey,
      username: credsUsername.value,
      password: credsPassword.value,
    }, { successMessage: 'Sign-in remembered — it will be pre-filled for you' });
    credsSaved.value = true;
    credsPassword.value = '';
    credsOpen.value = false;
  } finally {
    credsSaving.value = false;
  }
}

async function onForgetCreds() {
  await api.del(credsUrl);
  credsSaved.value = false;
  credsUsername.value = '';
  credsPassword.value = '';
  credsOpen.value = false;
}

async function onCapture() {
  capturing.value = true;
  try {
    const { url, text, image } = await capturePage();
    const res = await api.post(props.captureEndpoint,
      { url, text, image, folder: (folder.value || '').trim() || null },
      { successMessage: 'Captured' });
    emit('captured', res);
    loadFolders();   // a brand-new folder name should appear in the picker next time
  } finally {
    capturing.value = false;
  }
}

onMounted(() => {
  connect({ key: props.pluginKey, url: props.url, api });
  loadFolders();
  loadCredsStatus();
});
onBeforeUnmount(disconnect);
</script>

<style scoped>
.browser-stream { position: relative; }
.browser-stream__bar { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }
/* Focusable but invisible (display:none/visibility:hidden would kill focus, and
   focus is what opens the phone keyboard). Kept on-screen — offscreen positions
   make iOS scroll-jump on focus. 16px font stops iOS zooming on focus. */
.browser-stream__kbd {
  position: absolute; bottom: 0; left: 0;
  width: 1px; height: 1px;
  opacity: 0; border: 0; padding: 0;
  font-size: 16px;
  pointer-events: none;
}
.browser-stream__dot { width: 10px; height: 10px; border-radius: 50%; }
.browser-stream__dot.is-on { background: var(--color-success-500); }
.browser-stream__dot.is-off { background: var(--color-warning-500); }
.browser-stream__error { color: var(--color-danger-500); }
.browser-stream__creds-hint { margin: 0 0 0.75rem; color: var(--p-text-muted-color, #666); }
.browser-stream__creds-form { display: flex; flex-direction: column; gap: 0.75rem; }
.browser-stream__creds-form :deep(.p-password),
.browser-stream__creds-form :deep(.p-password-input) { width: 100%; }
.browser-stream__screen {
  width: 100%; max-width: 1280px; aspect-ratio: 1280 / 800;
  border: 1px solid var(--surface-border, #ccc); background: #000;
  cursor: crosshair; user-select: none; outline: none;
}
</style>
