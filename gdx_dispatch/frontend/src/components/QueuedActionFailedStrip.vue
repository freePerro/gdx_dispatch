<script setup>
// The reader for refused offline writes (#528). A closeout, note, arrival or
// part request made in a dead zone replays when signal returns; when the
// server refuses it for good, the tech had been told "saved offline — submits
// automatically", and nothing else will ever say it didn't. This strip does:
// what didn't send, why, and what the tech can do about each one.
//
// Scoped to one job when mounted inside that job's screen — offering to delete
// job B's closeout from job A is how the only copy of a signature disappears —
// and phone-wide on Today. Actions are PER ROW, on the row the tech is looking
// at: a bulk Retry on Today would replay every stale body on the phone at
// once, payments included, and a bulk Discard could delete a refusal that
// arrived while the confirm was open and was never seen.
import { computed, onMounted, ref } from 'vue'
import { useToast } from 'primevue/usetoast'
// The module's exports, NOT useOfflineSync(): every mount of that composable
// starts a drain, and a reader that drains beside its host view sent every
// queued row twice.
import {
  failedActions, refreshFailedActions, retryFailedActions, discardFailedActions,
  describeQueuedAction, describeQueuedRefusal, isRetryable,
} from '../composables/useOfflineSync'
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm'

const props = defineProps({
  jobId: { type: String, default: null },
})

const toast = useToast()
const { confirmAsync } = useDestructiveConfirm()
// Read what is already on the phone — no drain.
onMounted(() => { refreshFailedActions() })

const COLLAPSED = 3
const showAll = ref(false)
const busyId = ref(null)

const rows = computed(() =>
  (failedActions?.value || []).filter((r) => !props.jobId || String(r.resource_id) === String(props.jobId)),
)
const count = computed(() => rows.value.length)
const shown = computed(() => (showAll.value ? rows.value : rows.value.slice(0, COLLAPSED)))
const hasCloseout = computed(() => rows.value.some((r) => r.action_type === 'job.closeout'))

function canRetry(row) {
  return isRetryable(row.action_type)
}

// A payment's refusal is money, and the strip must not settle what only the
// office can. A `duplicate_payment` 409 says an IDENTICAL payment (same
// invoice, amount and method, reference-less, in the server's last 120 s) is on
// the invoice — which is this one replayed after a lost answer, OR a second,
// separate one: the drain replays a dead zone's payments back to back, so two
// $200 cash payments taken hours apart collide. Neither "missing" nor "on the
// invoice" is safe to say flat, so every note sends the tech to the office.
// Every other refusal only proves THIS phone's copy was not added (a duplicate
// check number is refused with a sentence, not a code).
function paymentNote(row) {
  if (row.reason === 'duplicate_payment') {
    return 'An identical payment is already on the invoice. If this was a second, separate payment, tell the office before you delete it here.'
  }
  if (row.uncertain) return 'It may already be on the invoice. Ask the office to check before you delete it here.'
  return "This phone's copy was not added to the invoice. Tell the office before you delete it here."
}

function paymentDiscardMessage(row) {
  if (row.reason === 'duplicate_payment') {
    return 'An identical payment is already on the invoice. If this was a second, separate payment, this is the only record of it — tell the office first.'
  }
  return 'This may be the only record of it. Make sure the office has checked the invoice first.'
}

async function retry(row) {
  busyId.value = row.id
  try {
    const o = await retryFailedActions({ ids: [row.id] })
    const what = describeQueuedAction(row.action_type)
    if (o.superseded) {
      toast.add(o.supersededBy === 'newer_on_phone'
        ? {
            severity: 'info',
            summary: `A newer ${what.toLowerCase()} exists`,
            detail: `You made a later ${what.toLowerCase()} for this job on this phone, so this older one was not resent. If the later one didn't send either, do it again.`,
            life: 7000,
          }
        : {
            severity: 'info',
            summary: 'Already closed out',
            // Also what a "may have sent" closeout that DID land looks like —
            // the server's closeout may be this very one, or a newer one.
            detail: 'The server already has this closeout or a newer one — this one was not resent.',
            life: 6000,
          })
    } else if (o.sent) {
      toast.add({ severity: 'success', summary: `${what} sent`, life: 3000 })
    } else if (o.refused) {
      toast.add({
        severity: 'warn',
        summary: 'Still refused',
        detail: o.first ? `${describeQueuedAction(o.first.action_type)}: ${describeQueuedRefusal(o.first)}` : '',
        life: 7000,
      })
    } else if (o.pending) {
      toast.add(o.serverError
        ? { severity: 'warn', summary: 'Server trouble', detail: 'Not sent yet — it tries again the next time the app syncs, or tap Retry in a minute.', life: 6000 }
        : o.online
          ? { severity: 'info', summary: 'Still sending', detail: 'It will go through in a moment.', life: 3500 }
          : { severity: 'warn', summary: 'Waiting for signal', detail: 'Sends when you have signal.', life: 3500 })
    }
  } finally {
    busyId.value = null
  }
}

async function discard(row) {
  const what = describeQueuedAction(row.action_type)
  // Destructive: for a closeout this row is the only copy of the parts, hours
  // and signature. Name exactly what goes.
  const ok = await confirmAsync({
    header: `Delete this ${what.toLowerCase()} from the phone?`,
    message: row.action_type === 'job.closeout'
      ? "It holds the parts, hours and signature — they exist only on this phone. Close the job out again first if it still needs it."
      : !canRetry(row)
        ? paymentDiscardMessage(row)
        : 'It exists only on this phone. Retry first if the job was fixed since.',
    acceptLabel: 'Delete',
  })
  if (!ok) return
  const deleted = await discardFailedActions({ ids: [row.id] })
  if (deleted) toast.add({ severity: 'info', summary: `${what} deleted from this phone`, life: 3000 })
}
</script>

<template>
  <div v-if="count" class="queued-failed" role="alert" data-testid="queued-failed-strip">
    <div class="queued-failed-head">
      <i class="pi pi-exclamation-triangle" />
      <span class="queued-failed-title" data-testid="queued-failed-count">
        {{ count }} change{{ count === 1 ? '' : 's' }} didn't send
      </span>
    </div>
    <ul class="queued-failed-list">
      <li v-for="r in shown" :key="r.id" class="queued-failed-item" data-testid="queued-failed-item">
        <div class="queued-failed-what">
          <strong>{{ describeQueuedAction(r.action_type) }}</strong>
          <span v-if="r.amount" class="queued-failed-amount" data-testid="queued-failed-amount">{{ r.amount }}</span>
          <span class="queued-failed-reason"> — {{ describeQueuedRefusal(r) }}</span>
          <span v-if="!canRetry(r)" class="queued-failed-office" data-testid="queued-failed-office">
            {{ paymentNote(r) }}
          </span>
        </div>
        <div class="queued-failed-actions">
          <button
            v-if="canRetry(r)"
            type="button"
            class="queued-failed-btn"
            :disabled="busyId === r.id"
            data-testid="queued-failed-retry"
            @click="retry(r)"
          >Retry</button>
          <button
            type="button"
            class="queued-failed-btn"
            :disabled="busyId === r.id"
            data-testid="queued-failed-discard"
            @click="discard(r)"
          >Discard</button>
        </div>
      </li>
    </ul>
    <button
      v-if="count > COLLAPSED"
      type="button"
      class="queued-failed-more"
      data-testid="queued-failed-show-all"
      @click="showAll = !showAll"
    >{{ showAll ? 'Show fewer' : `Show all ${count}` }}</button>
    <p v-if="hasCloseout" class="queued-failed-hint" data-testid="queued-failed-closeout-hint">
      A closeout here is still on this phone. Fix what it needs and Retry — or close the job out again, and this one retires itself.
    </p>
  </div>
</template>

<style scoped>
.queued-failed {
  display: flex; flex-direction: column; gap: 0.45rem;
  padding: 0.6rem 0.7rem; border-radius: 0.5rem;
  border: 1px solid var(--p-red-400, #f87171);
  background: color-mix(in srgb, var(--p-red-400, #f87171) 12%, transparent);
  color: var(--p-text-color);
  font-size: 0.85rem;
}
.queued-failed-head { display: flex; align-items: center; gap: 0.5rem; }
.queued-failed-head .pi { color: var(--p-red-500, #ef4444); }
.queued-failed-title { font-weight: 700; }
.queued-failed-list { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 0.5rem; }
.queued-failed-item { display: flex; flex-direction: column; gap: 0.35rem; }
.queued-failed-reason { color: var(--p-text-muted-color); }
/* A space in the template is condensed away; the gap is layout's job. */
.queued-failed-amount { margin-left: 0.35em; font-weight: 600; }
/* What to do about a payment — its own line, not a run-on of the server's. */
.queued-failed-office { display: block; margin-top: 0.25rem; font-weight: 600; }
.queued-failed-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.queued-failed-btn {
  min-height: 40px; padding: 0.35rem 0.9rem; border-radius: 999px; cursor: pointer;
  font: inherit; font-size: 0.85rem; font-weight: 600;
  background: var(--p-content-background); color: var(--p-text-color);
  border: 1px solid var(--p-content-border-color);
}
.queued-failed-btn:disabled { opacity: 0.6; cursor: default; }
.queued-failed-more {
  align-self: flex-start; padding: 0.2rem 0; border: 0; background: none; cursor: pointer;
  font: inherit; font-size: 0.85rem; font-weight: 600; color: var(--p-primary-color);
}
.queued-failed-hint { margin: 0; color: var(--p-text-muted-color); }
</style>
