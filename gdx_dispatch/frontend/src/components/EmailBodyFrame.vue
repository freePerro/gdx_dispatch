<!--
  EmailBodyFrame — safely render a fetched email body (D1).

  Email HTML is attacker-controlled: it carries <script>, on* handlers, and
  remote tracking pixels. We render it inside a FULLY sandboxed iframe
  (`sandbox=""` — no allow-scripts, no allow-same-origin, no forms) with a
  restrictive CSP that blocks ALL remote loads by default. Scripts can't run
  (sandbox), and tracking pixels/fonts/styles can't phone home (CSP). A
  "Show images" toggle relaxes only `img-src` when the user opts in.

  This component NEVER uses v-html — the whole point is to keep the hostile
  markup out of the parent document. `srcdoc` is a string the browser parses
  in the isolated frame.

  Residual (tracking, NOT script execution): CSP can't block a
  `<meta http-equiv="refresh">` self-navigation, so a crafted body could fire
  ONE beacon by navigating the frame — but that also visibly blanks the email
  (self-defeating for a tracker, obvious to the user). referrerpolicy +
  sandbox keep it origin-less. Full neutralization needs a server-side
  sanitizer (no bleach/nh3 in the image today); tracked as a follow-up.
-->
<template>
  <div class="email-body-frame">
    <div v-if="loading" class="ebf-state">
      <i class="pi pi-spin pi-spinner" /> Loading message…
    </div>

    <template v-else>
      <div v-if="note" class="ebf-note" data-test="ebf-note">{{ note }}</div>

      <div v-if="canShowImagesToggle" class="ebf-toolbar">
        <button
          type="button"
          class="ebf-imgbtn"
          data-test="ebf-images-toggle"
          @click="showImages = !showImages"
        >
          {{ showImages ? 'Hide remote images' : 'Show remote images' }}
        </button>
        <span class="ebf-imgnote">Remote images are blocked until you allow them.</span>
      </div>

      <iframe
        ref="frame"
        class="ebf-iframe"
        sandbox=""
        referrerpolicy="no-referrer"
        title="Email body"
        data-test="ebf-iframe"
        :srcdoc="srcdoc"
      />
    </template>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'

const props = defineProps({
  // Raw body from the /body endpoint. For html: attacker HTML. For text: plain.
  html: { type: String, default: '' },
  contentType: { type: String, default: 'html' }, // 'html' | 'text'
  // Shown when the live body could not be fetched (reconnect / gone).
  note: { type: String, default: '' },
  loading: { type: Boolean, default: false },
})

const showImages = ref(false)

// Only offer the images toggle for real HTML bodies (plain text has none).
const canShowImagesToggle = computed(
  () => !props.loading && props.contentType === 'html' && !!props.html,
)

function escapeHtml(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

const srcdoc = computed(() => {
  // CSP: default-src 'none' blocks scripts, frames, connects, media, objects.
  // style-src 'unsafe-inline' lets legit inline email styles render. img-src
  // is data: only until the user opts into remote images. No script-src at
  // all → inline + remote scripts are both dead (belt to the sandbox brace).
  const imgSrc = showImages.value ? "img-src data: https: http:" : "img-src data:"
  const csp = [
    "default-src 'none'",
    "style-src 'unsafe-inline'",
    imgSrc,
  ].join('; ')

  const inner =
    props.contentType === 'text'
      ? `<pre class="txt">${escapeHtml(props.html)}</pre>`
      : (props.html || '')

  return `<!doctype html><html><head><meta charset="utf-8">` +
    `<meta http-equiv="Content-Security-Policy" content="${csp}">` +
    `<base target="_blank">` +
    `<style>` +
    `html,body{margin:0;padding:12px;font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1e293b;word-break:break-word;overflow-wrap:anywhere;}` +
    `img{max-width:100%;height:auto;}` +
    `pre.txt{white-space:pre-wrap;font:inherit;margin:0;}` +
    `a{color:#2563eb;}` +
    `@media (prefers-color-scheme: dark){html,body{color:#e2e8f0;background:transparent;}a{color:#60a5fa;}}` +
    `</style></head><body>${inner}</body></html>`
})
</script>

<style scoped>
.email-body-frame {
  width: 100%;
}
.ebf-state {
  padding: 1rem;
  color: var(--p-text-muted-color, #64748b);
  font-size: 0.9rem;
}
.ebf-note {
  padding: 0.5rem 0.75rem;
  margin-bottom: 0.5rem;
  border-radius: 6px;
  background: var(--p-content-hover-background, #f1f5f9);
  color: var(--p-text-muted-color, #64748b);
  font-size: 0.8rem;
}
.ebf-toolbar {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.5rem;
  flex-wrap: wrap;
}
.ebf-imgbtn {
  border: 1px solid var(--p-content-border-color, #cbd5e1);
  background: var(--p-content-background, #fff);
  color: var(--p-text-color, #1e293b);
  border-radius: 6px;
  padding: 0.25rem 0.6rem;
  font-size: 0.8rem;
  cursor: pointer;
}
.ebf-imgbtn:hover {
  background: var(--p-content-hover-background, #f1f5f9);
}
.ebf-imgnote {
  font-size: 0.75rem;
  color: var(--p-text-muted-color, #94a3b8);
}
.ebf-iframe {
  width: 100%;
  min-height: 260px;
  height: 55vh;
  border: 1px solid var(--p-content-border-color, #e2e8f0);
  border-radius: 8px;
  background: var(--p-content-background, #fff);
  resize: vertical;
}
</style>
