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
      <span v-if="error" class="browser-stream__error">{{ error }}</span>
    </div>

    <img
      ref="screen"
      class="browser-stream__screen"
      :src="frameSrc || transparentPixel"
      tabindex="0"
      draggable="false"
      alt="Remote browser"
      @mousedown.prevent="(e) => mouse('mousedown', e, screen)"
      @mouseup.prevent="(e) => mouse('mouseup', e, screen)"
      @mousemove.prevent="(e) => mouse('mousemove', e, screen)"
      @wheel.prevent="(e) => wheel(e, screen)"
      @keydown.prevent="(e) => key('keydown', e)"
      @keyup.prevent="(e) => key('keyup', e)"
      @paste.prevent="(e) => paste(e)"
    />
  </div>
</template>

<script setup>
// Phase 2 (ADR-014): streamed headless browser the operator drives, e.g. to log
// into a no-API site. Pixels in, input out — the remote site never executes
// here. All logic is in useBrowserStream so it unit-tests; this is the template.
import { onMounted, onBeforeUnmount, ref } from 'vue';
import Button from 'primevue/button';
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
const capturing = ref(false);
const folder = ref('');
const folderOptions = ref([]);
const { frameSrc, connected, error, connect, mouse, wheel, key, paste, capturePage, disconnect } =
  useBrowserStream();

async function loadFolders() {
  if (!props.foldersEndpoint) return;
  try {
    folderOptions.value = (await api.get(props.foldersEndpoint)) || [];
  } catch {
    folderOptions.value = [];
  }
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
});
onBeforeUnmount(disconnect);
</script>

<style scoped>
.browser-stream__bar { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }
.browser-stream__dot { width: 10px; height: 10px; border-radius: 50%; }
.browser-stream__dot.is-on { background: var(--color-success-500); }
.browser-stream__dot.is-off { background: var(--color-warning-500); }
.browser-stream__error { color: var(--color-danger-500); }
.browser-stream__screen {
  width: 100%; max-width: 1280px; aspect-ratio: 1280 / 800;
  border: 1px solid var(--surface-border, #ccc); background: #000;
  cursor: crosshair; user-select: none; outline: none;
}
</style>
