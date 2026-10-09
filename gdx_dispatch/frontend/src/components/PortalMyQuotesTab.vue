<template>
  <div class="myq-tab" data-testid="my-quotes-tab">
    <div v-if="loading" class="empty-msg">Loading…</div>
    <Message v-else-if="locked" severity="info" data-testid="my-quotes-locked">
      Agree to the reseller terms on My Branding to start making quotes.
      <div class="mt-2">
        <Button label="Go to My Branding" icon="pi pi-arrow-right" size="small" data-testid="my-quotes-goto-branding" @click="emit('goto-branding')" />
      </div>
    </Message>
    <Message v-else-if="listError" severity="warn" data-testid="my-quotes-error">{{ listError }}</Message>
    <template v-else>
      <Message v-if="actionError" severity="error" :closable="true" data-testid="my-quotes-action-error" @close="actionError = ''">{{ actionError }}</Message>
      <div v-if="!quotes.length" class="empty-msg" data-testid="my-quotes-empty">
        No quotes yet. Open one of your estimates and choose “Resell this” to make a quote under your own name.
        <div class="mt-2">
          <Button label="See my estimates" icon="pi pi-file" text data-testid="my-quotes-goto-estimates" @click="emit('goto-estimates')" />
        </div>
      </div>
      <div v-else class="myq-cards">
        <Card v-for="q in quotes" :key="q.id" :data-testid="`my-quote-${q.id}`">
          <template #title>
            <div class="card-title-row">
              <span>{{ q.reference }}</span>
              <span class="meta">{{ formatDate(q.created_at) }}</span>
            </div>
          </template>
          <template #subtitle>
            <span v-if="q.end_customer_name">For {{ q.end_customer_name }} · </span>
            <span>From our estimate {{ q.estimate_number || '—' }}</span>
          </template>
          <template #content>
            <dl v-if="!q.options" class="price-grid">
              <div><dt>Our price before tax</dt><dd :data-testid="`my-quote-base-${q.id}`">{{ money(q.base_subtotal) }}</dd></div>
              <div><dt>Your price</dt><dd :data-testid="`my-quote-price-${q.id}`">{{ money(q.resale_subtotal) }}</dd></div>
              <div><dt>Markup</dt><dd :data-testid="`my-quote-markup-${q.id}`">{{ pct(q.markup_pct) }} · {{ money(q.markup_amount) }}</dd></div>
            </dl>
            <template v-else>
              <p class="meta">Markup {{ pct(q.markup_pct) }} on each option.</p>
              <ul class="option-list" :data-testid="`my-quote-options-${q.id}`">
                <li v-for="o in q.options" :key="o.name">
                  <span class="option-name">{{ o.name }}</span>
                  <span class="meta">ours before tax {{ money(o.base_price) }}</span>
                  <span class="option-price">{{ money(o.price) }}</span>
                </li>
              </ul>
            </template>
            <p v-if="q.hide_line_prices" class="meta">Line prices are hidden on this quote, as on our estimate.</p>
            <div class="action-row">
              <Button
                label="Download PDF"
                icon="pi pi-download"
                severity="secondary"
                outlined
                :loading="busy[q.id] === 'pdf'"
                :disabled="!!busy[q.id] && busy[q.id] !== 'pdf'"
                :data-testid="`my-quote-pdf-${q.id}`"
                @click="download(q)"
              />
              <Button
                label="Delete"
                icon="pi pi-trash"
                severity="danger"
                text
                :disabled="!!busy[q.id]"
                :data-testid="`my-quote-delete-${q.id}`"
                @click="confirmDelete(q)"
              />
            </div>
          </template>
        </Card>
      </div>
    </template>
  </div>
</template>

<script setup>
/**
 * The customer portal's "My Quotes" tab, for contractor and wholesale
 * accounts: the resale quotes they have made from our estimates, with what
 * we charge them (before tax: the snapshot carries none) beside what they charge. Private to them (routers/portal_resale.py).
 */
import { onMounted, ref, watch } from 'vue';
import Button from 'primevue/button';
import Card from 'primevue/card';
import Message from 'primevue/message';
import { formatDate, formatMoney } from '../composables/useFormatters';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';

const props = defineProps({
  // The portal view's authenticated fetch (JSON; throws with the server's detail).
  fetcher: { type: Function, required: true },
  // Saves an authenticated file under a name: (path, filename) => Promise.
  downloader: { type: Function, required: true },
  // Bumped by the view when a quote is made elsewhere, to reload the list.
  refreshKey: { type: Number, default: 0 },
});
const emit = defineEmits(['goto-branding', 'goto-estimates']);

const quotes = ref([]);
const loading = ref(true);
const locked = ref(false);
const listError = ref('');
const actionError = ref('');
// Per card, and one action at a time on a card: a download finishing must not
// re-enable a delete that is still in flight, on that card or another.
const busy = ref({});
const { confirmDestructive } = useDestructiveConfirm();

function money(v) { return v == null ? '—' : formatMoney(Number(v)); }
function clearBusy(id) {
  const { [id]: _done, ...rest } = busy.value;
  busy.value = rest;
}
function pct(v) { return `${Number(v) || 0}%`; }

async function load() {
  try {
    const data = await props.fetcher('/portal/resale-quotes');
    quotes.value = Array.isArray(data) ? data : [];
    locked.value = false;
    listError.value = '';
  } catch (e) {
    // 403 before the disclaimer: say where to go, not "could not load".
    locked.value = e?.status === 403;
    listError.value = locked.value ? '' : 'Could not load your quotes.';
  } finally {
    loading.value = false;
  }
}

async function download(q) {
  busy.value = { ...busy.value, [q.id]: 'pdf' };
  try {
    await props.downloader(`/portal/resale-quotes/${q.id}/pdf`, `quote-${q.reference}.pdf`);
  } finally {
    clearBusy(q.id);
  }
}

function confirmDelete(q) {
  confirmDestructive({
    header: `Delete ${q.reference}?`,
    message: 'The quote leaves your list and its PDF can no longer be downloaded. Our estimate is not changed.',
    acceptLabel: 'Delete',
    rejectLabel: 'Keep it',
    accept: async () => {
      busy.value = { ...busy.value, [q.id]: 'delete' };
      actionError.value = '';
      try {
        await props.fetcher(`/portal/resale-quotes/${q.id}`, { method: 'DELETE' });
        quotes.value = quotes.value.filter((x) => x.id !== q.id);
      } catch (e) {
        actionError.value = e?.message?.startsWith('request failed') ? 'Could not delete that quote.' : (e?.message || 'Could not delete that quote.');
      } finally {
        clearBusy(q.id);
      }
    },
  });
}

onMounted(load);
watch(() => props.refreshKey, load);
</script>

<style scoped>
.myq-tab { display: flex; flex-direction: column; gap: 1rem; }
.myq-cards { display: flex; flex-direction: column; gap: 0.75rem; }
.card-title-row { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.price-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.75rem; margin: 0 0 0.75rem; }
.price-grid dt { font-size: 0.8rem; color: var(--p-text-muted-color, #6b7280); }
.price-grid dd { margin: 0.15rem 0 0; font-weight: 700; }
.option-list { list-style: none; margin: 0 0 0.75rem; padding: 0; display: flex; flex-direction: column; gap: 0.35rem; }
.option-list li { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0.5rem; }
.option-name { font-weight: 600; flex: 1 1 auto; min-width: 0; }
.option-price { font-weight: 700; }
.action-row { display: flex; flex-wrap: wrap; gap: 0.5rem; }
.meta { font-size: 0.85rem; color: var(--p-text-muted-color, #6b7280); }
.empty-msg { text-align: center; padding: 1.5rem; color: var(--p-text-muted-color, #6b7280); }
@media (max-width: 640px) {
  .price-grid { grid-template-columns: 1fr 1fr; }
}
</style>
