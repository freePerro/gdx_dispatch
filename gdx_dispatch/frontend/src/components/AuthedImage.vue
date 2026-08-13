<script setup>
/**
 * An <img> for a URL that needs the auth header.
 *
 * Job photos live behind `/api/documents/{id}/download`, which requires a
 * Bearer token — and a plain `<img src>` cannot send one, so it 401s and the
 * browser paints a broken-image icon. There is no static uploads mount to fall
 * back on. Found by driving a real phone: the upload succeeded, the record
 * landed on the job, and the thumbnail was still broken.
 *
 * So fetch the bytes with auth and hand the <img> an object URL instead.
 */
import { onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps({
  src: { type: String, default: '' },
  alt: { type: String, default: '' },
})

const objectUrl = ref(null)
const failed = ref(false)

function release() {
  if (objectUrl.value) {
    URL.revokeObjectURL(objectUrl.value)
    objectUrl.value = null
  }
}

async function load(src) {
  release()
  failed.value = false
  if (!src) return
  let token = null
  try {
    token = sessionStorage.getItem('gdx_access_token') || null
  } catch { /* private mode */ }
  try {
    const resp = await fetch(src, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    // `resp.ok` is not enough. A url the API doesn't serve falls through to the
    // SPA catch-all, which answers 200 with index.html — and an object URL made
    // from HTML paints an empty frame with no error, so a photo that isn't
    // there looks like a photo that failed to load slowly, forever. Trust the
    // bytes' own type, not the status code.
    if (blob.type && !blob.type.startsWith('image/')) {
      throw new Error(`not an image: ${blob.type}`)
    }
    objectUrl.value = URL.createObjectURL(blob)
  } catch {
    // Show the caller's fallback rather than a broken-image icon.
    failed.value = true
  }
}

watch(() => props.src, load, { immediate: true })
onBeforeUnmount(release)
</script>

<template>
  <img v-if="objectUrl" :src="objectUrl" :alt="alt" loading="lazy" />
  <slot v-else-if="failed" name="fallback" />
  <span v-else class="authed-img-loading" aria-hidden="true" />
</template>

<style scoped>
.authed-img-loading {
  display: block;
  width: 100%;
  height: 100%;
  background: var(--p-content-border-color, #e5e7eb);
}
</style>
