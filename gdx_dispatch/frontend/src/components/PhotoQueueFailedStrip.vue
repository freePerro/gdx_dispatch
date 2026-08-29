<script setup>
// The reader for FAILED photo rows (2026-08-28, #525). One strip, both
// surfaces that capture (the job page's Photos card and the closeout sheet):
// "N couldn't upload — <why>" with the two things a tech can do about it.
//
// Scoped to ONE job: the queue's counts are phone-wide, but this strip sits
// inside job A's screen, and offering to delete job B's photo from there is
// how the only copy of the wrong door disappears.
import { computed } from 'vue'
import { useToast } from 'primevue/usetoast'
import { usePhotoQueue } from '../composables/usePhotoQueue'
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm'

const props = defineProps({
  jobId: { type: String, default: null },
})

const toast = useToast()
const { failedRows, retryFailedPhotos, discardFailedPhotos, describePhotoRefusal } = usePhotoQueue()
const { confirmAsync } = useDestructiveConfirm()

const rows = computed(() =>
  (failedRows?.value || []).filter((r) => !props.jobId || String(r.job_id) === String(props.jobId)),
)
const count = computed(() => rows.value.length)
// The reason for the first refusal. A photo refused during a BACKGROUND drain
// never had a toast — this line is the only place its reason is ever spoken.
const reason = computed(() =>
  rows.value.length && typeof describePhotoRefusal === 'function' ? describePhotoRefusal(rows.value[0].http_status) : '',
)

async function retry() {
  const o = await retryFailedPhotos({ jobId: props.jobId })
  if (o.uploaded && !o.refused && !o.pending) {
    toast.add({ severity: 'success', summary: o.uploaded === 1 ? 'Photo uploaded' : `${o.uploaded} photos uploaded`, life: 3000 })
  } else if (o.refused) {
    toast.add({
      severity: 'warn',
      summary: o.refused === 1 ? 'Still refused' : `${o.refused} still refused`,
      detail: describePhotoRefusal(o.status),
      life: 6000,
    })
  } else if (o.pending) {
    toast.add({ severity: 'warn', summary: 'Waiting for signal', detail: 'Uploads when you have signal.', life: 3500 })
  }
}
async function discard() {
  const n = count.value
  // Destructive: these blobs are the only copy of a door the tech has driven
  // away from. Ask in words that say so.
  const ok = await confirmAsync({
    header: `Delete ${n} photo${n === 1 ? '' : 's'} that couldn't upload?`,
    message: `${n === 1 ? 'It exists' : 'They exist'} only on this phone. Retry first if the job was fixed since.`,
    acceptLabel: 'Delete',
  })
  if (!ok) return
  const deleted = await discardFailedPhotos({ jobId: props.jobId })
  toast.add({ severity: 'info', summary: `Deleted ${deleted} photo${deleted === 1 ? '' : 's'} from this phone`, life: 3000 })
}
</script>

<template>
  <div v-if="count" class="photo-failed" role="alert" data-testid="photo-failed-strip">
    <i class="pi pi-exclamation-triangle" />
    <span class="photo-failed-text">
      <span data-testid="photo-failed-count">{{ count }} photo{{ count === 1 ? '' : 's' }} couldn't upload</span>
      <span class="photo-failed-reason" data-testid="photo-failed-reason"> — {{ reason }}</span>
    </span>
    <button type="button" class="photo-failed-btn" data-testid="photo-failed-retry" @click="retry">Retry</button>
    <button type="button" class="photo-failed-btn" data-testid="photo-failed-discard" @click="discard">Discard</button>
  </div>
</template>

<style scoped>
.photo-failed {
  display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
  padding: 0.5rem 0.65rem; border-radius: 0.5rem;
  border: 1px solid var(--p-red-400, #f87171);
  background: color-mix(in srgb, var(--p-red-400, #f87171) 12%, transparent);
  color: var(--p-text-color);
  font-size: 0.85rem;
}
.photo-failed .pi { color: var(--p-red-500, #ef4444); }
.photo-failed-text { flex: 1 1 auto; font-weight: 600; min-width: 0; }
.photo-failed-reason { font-weight: 400; color: var(--p-text-muted-color); }
.photo-failed-btn {
  min-height: 36px; padding: 0.3rem 0.7rem; border-radius: 999px; cursor: pointer;
  font: inherit; font-size: 0.8rem; font-weight: 600;
  background: var(--p-content-background); color: var(--p-text-color);
  border: 1px solid var(--p-content-border-color);
}
</style>
