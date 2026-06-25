<template>
  <div class="browser-stream" data-testid="browser-stream">
    <div class="browser-stream__bar">
      <span :class="['browser-stream__dot', connected ? 'is-on' : 'is-off']" />
      <span>{{ connected ? 'Connected' : 'Connecting…' }}</span>
      <Button
        label="Save login session"
        size="small"
        :disabled="!connected"
        @click="onSave"
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
import { useApiWithToast } from '../composables/useApiWithToast';
import { useBrowserStream } from '../composables/useBrowserStream';

const props = defineProps({
  pluginKey: { type: String, required: true },
  url: { type: String, required: true },
});
const emit = defineEmits(['session']);

const transparentPixel =
  'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';

const api = useApiWithToast();
const screen = ref(null);
const { frameSrc, connected, error, connect, mouse, wheel, key, paste, saveSession, disconnect } =
  useBrowserStream();

async function onSave() {
  const state = await saveSession();
  emit('session', state);
}

onMounted(() => connect({ key: props.pluginKey, url: props.url, api }));
onBeforeUnmount(disconnect);
</script>

<style scoped>
.browser-stream__bar { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; }
.browser-stream__dot { width: 10px; height: 10px; border-radius: 50%; }
.browser-stream__dot.is-on { background: #22c55e; }
.browser-stream__dot.is-off { background: #f59e0b; }
.browser-stream__error { color: #ef4444; }
.browser-stream__screen {
  width: 100%; max-width: 1280px; aspect-ratio: 1280 / 800;
  border: 1px solid var(--surface-border, #ccc); background: #000;
  cursor: crosshair; user-select: none; outline: none;
}
</style>
