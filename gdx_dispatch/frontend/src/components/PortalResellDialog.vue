<template>
  <Dialog
    :visible="visible"
    :header="estimate ? `Resell ${estimate.estimate_number || 'this estimate'}` : 'Resell this'"
    :modal="true"
    :style="{ width: 'min(560px, 94vw)' }"
    data-testid="resell-dialog"
    @update:visible="(v) => emit('update:visible', v)"
  >
    <div v-if="loading" class="empty-msg">Loading…</div>
    <div v-else-if="!accepted" class="resell-body" data-testid="resell-locked">
      <p>Before you make a quote, agree to the reseller terms on My Branding and add your company details.</p>
      <div class="action-row">
        <Button label="Go to My Branding" icon="pi pi-arrow-right" data-testid="resell-goto-branding" @click="emit('goto-branding')" />
      </div>
    </div>
    <div v-else-if="created" class="resell-body" data-testid="resell-done">
      <Message severity="success" :closable="false">
        Quote {{ created.reference }} is ready: {{ priceLine(created) }}
      </Message>
      <div class="action-row">
        <Button
          label="Download PDF"
          icon="pi pi-download"
          :loading="downloading"
          data-testid="resell-download-btn"
          @click="downloadCreated"
        />
        <Button label="See My Quotes" icon="pi pi-list" severity="secondary" outlined data-testid="resell-goto-quotes" @click="emit('goto-quotes')" />
      </div>
    </div>
    <form v-else :id="formId" class="resell-body" @submit.prevent="submit">
      <p class="meta">
        Your customer sees your name and your price, never ours. Our estimate is not changed.
        <template v-if="!setUp"> You have not added your company details yet; the quote prints without them.</template>
      </p>
      <label class="field">
        <span>Markup</span>
        <InputNumber
          v-model="form.markup_pct"
          :min="0"
          :max="500"
          :max-fraction-digits="2"
          suffix=" %"
          data-testid="resell-markup"
        />
        <small v-if="form.markup_pct == null" class="field-error" data-testid="resell-markup-required">Enter a markup; 0 sells at our price.</small>
      </label>
      <label class="field">
        <span>Quote number</span>
        <InputText v-model="form.reference" maxlength="60" placeholder="Leave blank to number it for you" data-testid="resell-reference" />
      </label>
      <label class="field">
        <span>Your customer's name</span>
        <InputText v-model="form.end_customer_name" maxlength="200" data-testid="resell-customer-name" />
      </label>
      <label class="field">
        <span>Your customer's address</span>
        <Textarea v-model="form.end_customer_address" rows="2" maxlength="500" auto-resize data-testid="resell-customer-address" />
      </label>
      <label class="field">
        <span>Notes for your customer</span>
        <Textarea v-model="form.notes" rows="3" maxlength="5000" auto-resize data-testid="resell-notes" />
      </label>
      <Message v-if="submitError" ref="errorEl" severity="error" data-testid="resell-error">{{ submitError }}</Message>
    </form>
    <!-- Footer, not the end of the form: the content scrolls, and on a phone a
         row at its end starts below the dialog's edge (GDXA-872). -->
    <template v-if="showForm" #footer>
      <div class="action-row footer-row">
        <Button type="submit" :form="formId" label="Make my quote" icon="pi pi-check" :loading="submitting" :disabled="form.markup_pct == null" data-testid="resell-submit-btn" />
        <Button label="Cancel" severity="secondary" text @click="emit('update:visible', false)" />
      </div>
    </template>
  </Dialog>
</template>

<script setup>
/**
 * "Resell this" on a portal estimate, for contractor and wholesale accounts:
 * freezes a marked-up copy under the reseller's own brand
 * (POST /portal/estimates/{id}/resale) and offers its PDF straight away.
 * The markup starts from the default on their branding.
 */
import { computed, nextTick, reactive, ref, useId, watch } from 'vue';
import Button from 'primevue/button';
import Dialog from 'primevue/dialog';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Textarea from 'primevue/textarea';
import { formatMoney } from '../composables/useFormatters';

const props = defineProps({
  visible: { type: Boolean, default: false },
  // The estimate being resold (the portal's estimate detail).
  estimate: { type: Object, default: null },
  fetcher: { type: Function, required: true },
  downloader: { type: Function, required: true },
});
const emit = defineEmits(['update:visible', 'created', 'goto-branding', 'goto-quotes']);

const loading = ref(false);
const accepted = ref(false);
const setUp = ref(false);
const form = reactive({ markup_pct: 0, reference: '', end_customer_name: '', end_customer_address: '', notes: '' });
const submitting = ref(false);
const submitError = ref('');
const created = ref(null);
const downloading = ref(false);
const formId = `resell-form-${useId()}`;
const errorEl = ref(null);
// Save is pinned in the footer, so a refusal can land below the fold of the
// scrolled form (GDXA-872): bring it into view.
watch(submitError, async (msg) => {
  if (!msg) return;
  await nextTick();
  // A component ref: Message's root element is what scrolls.
  errorEl.value?.$el?.scrollIntoView?.({ block: 'nearest' });
});
// The same three branches the template walks; the form is the last.
const showForm = computed(() => !loading.value && accepted.value && !created.value);

function priceLine(q) {
  if (q.options) return `${q.options.length} options at ${Number(q.markup_pct)}% markup.`;
  return `your price ${formatMoney(Number(q.resale_subtotal))}, our price before tax ${formatMoney(Number(q.base_subtotal))}.`;
}

async function open() {
  loading.value = true;
  created.value = null;
  submitError.value = '';
  Object.assign(form, { reference: '', end_customer_name: '', end_customer_address: '', notes: '' });
  try {
    const profile = await props.fetcher('/portal/reseller/profile');
    accepted.value = !!profile.disclaimer?.accepted;
    setUp.value = !!profile.company_name;
    form.markup_pct = Number(profile.default_markup_pct) || 0;
  } catch {
    accepted.value = false;
  } finally {
    loading.value = false;
  }
}

async function submit() {
  // A cleared markup is not 0: it would sell at our price without saying so.
  if (!props.estimate || form.markup_pct == null) return;
  submitting.value = true;
  submitError.value = '';
  try {
    const row = await props.fetcher(`/portal/estimates/${props.estimate.id}/resale`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(form),
    });
    created.value = row;
    emit('created', row);
  } catch (e) {
    submitError.value = e?.message?.startsWith('request failed') ? 'Your quote could not be made. Check the fields and try again.' : (e?.message || 'Your quote could not be made.');
  } finally {
    submitting.value = false;
  }
}

async function downloadCreated() {
  downloading.value = true;
  try {
    await props.downloader(`/portal/resale-quotes/${created.value.id}/pdf`, `quote-${created.value.reference}.pdf`);
  } finally {
    downloading.value = false;
  }
}

watch(() => props.visible, (v) => { if (v) open(); });
</script>

<style scoped>
.resell-body { display: flex; flex-direction: column; gap: 0.85rem; }
.resell-body p { margin: 0; }
.field { display: flex; flex-direction: column; gap: 0.35rem; min-width: 0; }
.field > span { font-size: 0.85rem; font-weight: 600; color: var(--p-text-color, #374151); }
.field :deep(.p-inputtext), .field :deep(.p-inputnumber), .field :deep(.p-textarea) { width: 100%; }
.action-row { display: flex; flex-wrap: wrap; gap: 0.5rem; }
/* In the dialog footer (a flex row) the row must span it, or the phone rule
   below has no width to stretch its buttons into. */
.footer-row { flex: 1 1 auto; }
.field-error { color: var(--p-red-500, #ef4444); }
.meta { font-size: 0.85rem; color: var(--p-text-muted-color, #6b7280); }
.empty-msg { text-align: center; padding: 1.5rem; color: var(--p-text-muted-color, #6b7280); }
@media (max-width: 640px) {
  .action-row :deep(.p-button) { flex: 1 1 100%; }
}
</style>
