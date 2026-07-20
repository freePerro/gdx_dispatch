<template>
  <div v-if="pdf?.content_base64" class="pdf-preview" data-testid="composer-pdf-preview">
    <div class="pdf-preview-bar">
      <small class="pdf-preview-hint">Preview — this PDF is exactly what the customer receives.</small>
      <a
        v-if="blobUrl"
        :href="blobUrl"
        target="_blank"
        rel="noopener"
        class="pdf-preview-open"
        data-testid="composer-pdf-open"
      >Open full size</a>
    </div>
    <iframe
      v-if="blobUrl"
      :src="blobUrl + '#toolbar=0&navpanes=0&view=FitH'"
      class="pdf-preview-frame"
      :title="pdf?.name || 'PDF preview'"
      data-testid="composer-pdf-frame"
    />
    <div v-else class="pdf-preview-unavailable">
      Inline preview isn't available in this browser — the PDF is still attached.
    </div>
  </div>
</template>

<script setup>
import { onUnmounted, ref, watch } from "vue";

// Renders the composer's auto-attached PDF inline so the sender sees the
// actual document before it leaves the building. The composer endpoints
// return the PDF as base64, so the preview never re-fetches — what's shown
// is byte-for-byte the attachment that will be sent.
const props = defineProps({
  pdf: { type: Object, default: null }, // {name, content_type, content_base64}
});

const blobUrl = ref("");

function revoke() {
  if (blobUrl.value) {
    URL.revokeObjectURL(blobUrl.value);
    blobUrl.value = "";
  }
}

watch(
  () => props.pdf?.content_base64,
  (b64) => {
    revoke();
    // jsdom (vitest) has no createObjectURL — degrade to the unavailable note.
    if (!b64 || typeof URL.createObjectURL !== "function") return;
    try {
      const bytes = Uint8Array.from(atob(b64), (ch) => ch.charCodeAt(0));
      blobUrl.value = URL.createObjectURL(
        new Blob([bytes], { type: props.pdf?.content_type || "application/pdf" }),
      );
    } catch {
      blobUrl.value = "";
    }
  },
  { immediate: true },
);

onUnmounted(revoke);
</script>

<style scoped>
.pdf-preview {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-top: 0.5rem;
}
.pdf-preview-bar {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.75rem;
}
.pdf-preview-hint {
  color: var(--p-text-muted-color, #6b7280);
}
.pdf-preview-open {
  color: var(--p-primary-color, #3b82f6);
  font-size: 0.85rem;
  white-space: nowrap;
}
.pdf-preview-frame {
  width: 100%;
  height: 420px;
  border: 1px solid var(--p-content-border-color, #d1d5db);
  border-radius: 6px;
  background: var(--p-content-background, #f3f4f6);
}
.pdf-preview-unavailable {
  padding: 0.75rem;
  border: 1px dashed var(--p-content-border-color, #d1d5db);
  border-radius: 6px;
  color: var(--p-text-muted-color, #6b7280);
  font-size: 0.9rem;
}
</style>
