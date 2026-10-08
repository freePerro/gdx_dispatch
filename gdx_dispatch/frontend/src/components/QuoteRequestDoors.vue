<template>
  <section v-if="request" class="qr-doors" data-testid="quote-request-doors">
    <div class="qr-head">
      <span class="qr-label">Portal quote request</span>
      <Tag :value="request.status" :severity="request.withdrawn_at ? 'warn' : 'info'" data-testid="quote-request-doors-status" />
    </div>
    <div class="qr-job" data-testid="quote-request-doors-job">{{ request.job_name }}</div>
    <div v-if="request.site_address" class="qr-meta">{{ request.site_address }}</div>
    <div class="qr-meta">Sent {{ formatDateTime(request.created_at) }}</div>
    <div v-if="request.withdrawn_at" class="qr-meta qr-withdrawn" data-testid="quote-request-doors-withdrawn">
      <i class="pi pi-ban" /> Withdrawn by the customer {{ formatDateTime(request.withdrawn_at) }}
    </div>
    <div v-if="request.edited_at" class="qr-meta" data-testid="quote-request-doors-edited">
      <i class="pi pi-pencil" /> Changed by the customer {{ formatDateTime(request.edited_at) }}
    </div>

    <ol class="qr-door-list">
      <li
        v-for="d in request.doors"
        :key="d.index"
        class="qr-door"
        :data-testid="`quote-request-door-${d.index}`"
      >
        <div class="qr-door-title">{{ doorTitle(d) }}</div>
        <dl v-if="doorAnswers(d).length || d.color" class="qr-answers">
          <div v-for="a in doorAnswers(d)" :key="a.key" class="qr-answer">
            <dt>{{ a.question }}</dt><dd>{{ a.answer }}</dd>
          </div>
          <div v-if="d.color" class="qr-answer"><dt>Color</dt><dd>{{ d.color }}</dd></div>
        </dl>
        <p v-if="d.notes" class="qr-notes">{{ d.notes }}</p>
        <div v-if="d.photo_ids.length" class="qr-photos">
          <a
            v-for="pid in d.photo_ids"
            :key="pid"
            href="#"
            class="qr-photo"
            :data-testid="`quote-request-photo-${pid}`"
            @click.prevent="openPhoto(pid)"
          >
            <AuthedImage :src="photoUrl(pid)" :alt="`Door ${d.index + 1} photo`">
              <template #fallback><span class="qr-photo-missing">Photo unavailable</span></template>
            </AuthedImage>
          </a>
        </div>
      </li>
    </ol>
    <p v-if="request.notes" class="qr-notes" data-testid="quote-request-doors-notes">{{ request.notes }}</p>

    <Dialog v-model:visible="photoVisible" modal header="Photo" :style="{ width: 'min(92vw, 900px)' }">
      <AuthedImage v-if="openPhotoId" :src="photoUrl(openPhotoId)" alt="Door photo" class="qr-photo-large" />
    </Dialog>
  </section>
</template>

<script setup>
/**
 * The doors a customer asked about from the portal, on the lead that request
 * opened (and so on any estimate started from it). The request row is the
 * only copy of the doors — the lead's notes just point here. Nothing renders
 * for a lead that did not come from a portal request.
 */
import { computed, ref, watch } from 'vue';
import Dialog from 'primevue/dialog';
import Tag from 'primevue/tag';
import AuthedImage from './AuthedImage.vue';
import { useApi } from '../composables/useApi';
import { useAuthStore } from '../stores/auth';
import { formatDateTime } from '../composables/useFormatters';
import { doorAnswers, doorTitle } from './quoteRequestOptions';

const props = defineProps({
  leadId: { type: [String, Number], default: null },
});

const api = useApi();
const auth = useAuthStore();
const request = ref(null);
const photoVisible = ref(false);
const openPhotoId = ref(null);

const canReadLeads = computed(() => auth.hasPermission('leads.read'));

function photoUrl(pid) {
  return `/api/quote-requests/${request.value.id}/photos/${pid}`;
}

function openPhoto(pid) {
  openPhotoId.value = pid;
  photoVisible.value = true;
}

async function load(leadId) {
  request.value = null;
  if (!leadId || !canReadLeads.value) return;
  try {
    // null is the normal answer — most leads did not come from the portal.
    const data = await api.get(`/api/quote-requests/by-lead/${leadId}`, { suppressErrorToast: true });
    request.value = data && data.id && Array.isArray(data.doors) ? data : null;
  } catch {
    request.value = null;
  }
}

watch(() => props.leadId, load, { immediate: true });
</script>

<style scoped>
.qr-doors {
  display: flex; flex-direction: column; gap: 0.35rem;
  padding: 0.75rem; border-radius: 8px;
  border: 1px solid var(--p-content-border-color);
  background: var(--p-content-background);
  font-size: 0.85rem;
}
.qr-head { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; }
.qr-label {
  font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em;
  font-weight: 700; color: var(--p-text-muted-color);
}
.qr-job { font-weight: 700; font-size: 1rem; color: var(--p-text-color); }
.qr-meta { color: var(--p-text-muted-color); }
.qr-withdrawn { color: var(--p-orange-500); font-weight: 600; }
.qr-door-list { margin: 0.35rem 0 0; padding-left: 1.25rem; display: flex; flex-direction: column; gap: 0.75rem; }
.qr-door-title { font-weight: 600; color: var(--p-text-color); }
.qr-answers { margin: 0.25rem 0 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 0.15rem 1rem; }
.qr-answer { display: flex; gap: 0.35rem; min-width: 0; }
.qr-answer dt { color: var(--p-text-muted-color); }
.qr-answer dt::after { content: ':'; }
.qr-answer dd { margin: 0; color: var(--p-text-color); }
.qr-notes { margin: 0.25rem 0 0; white-space: pre-wrap; color: var(--p-text-color); }
.qr-photos { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-top: 0.4rem; }
.qr-photo { width: 88px; height: 88px; border-radius: 6px; overflow: hidden; display: block; border: 1px solid var(--p-content-border-color); }
.qr-photo :deep(img) { width: 100%; height: 100%; object-fit: cover; display: block; }
.qr-photo-missing { display: flex; align-items: center; justify-content: center; height: 100%; font-size: 0.7rem; text-align: center; color: var(--p-text-muted-color); }
/* AuthedImage's root is the <img> and carries this component's scope id, so a
   plain scoped selector reaches it inside the teleported Dialog; :deep() would
   need a scoped ancestor, which the teleport removes. */
.qr-photo-large { max-width: 100%; max-height: 75vh; height: auto; display: block; margin: 0 auto; }
</style>
