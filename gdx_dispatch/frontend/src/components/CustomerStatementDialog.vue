<!--
  Customer statement — preview, download and email from the customer record.

  The server computes everything: presets resolve on the shop's calendar and
  the figures shown here are the same ones in the PDF and the email. Warnings
  are for the office only — they never reach the customer's PDF — and are
  recorded in the audit row when the statement is sent.
-->
<template>
  <Dialog
    :visible="visible"
    modal
    header="Statement"
    :style="{ width: 'min(1100px, 96vw)' }"
    :breakpoints="{ '640px': '100vw' }"
    data-testid="statement-dialog"
    @update:visible="$emit('update:visible', $event)"
  >
    <div class="statement-controls">
      <label class="field">
        <span>Period</span>
        <Select
          v-model="preset"
          :options="presetOptions"
          option-label="label"
          option-value="key"
          data-testid="statement-preset"
          @change="onPresetChange"
        />
      </label>
      <template v-if="preset === 'custom'">
        <label class="field">
          <span>From</span>
          <InputText v-model="customStart" type="date" :min="FLOOR" :max="todayIso" data-testid="statement-start" />
        </label>
        <label class="field">
          <span>To</span>
          <InputText v-model="customEnd" type="date" :min="FLOOR" :max="todayIso" data-testid="statement-end" />
        </label>
        <Button
          label="Show"
          icon="pi pi-refresh"
          :disabled="!customStart || !customEnd"
          data-testid="statement-apply"
          @click="load"
        />
      </template>
      <span class="floor-note">Statements start from {{ floorLabel }}.</span>
    </div>

    <Message v-if="error" severity="error" :closable="false" data-testid="statement-error">{{ error }}</Message>

    <div v-if="loading" class="statement-loading"><ProgressSpinner style="width: 40px; height: 40px" /></div>

    <template v-else-if="statement">
      <Message
        v-for="w in statement.warnings"
        :key="w.kind + w.invoice_id"
        severity="warn"
        :closable="false"
        data-testid="statement-warning"
      >
        Invoice {{ w.invoice_number }}: {{ w.message }}
      </Message>

      <div class="statement-summary" data-testid="statement-summary">
        <div>
          <span class="label">Total unpaid</span>
          <strong data-testid="statement-total-unpaid">{{ formatMoney(statement.total_unpaid) }}</strong>
        </div>
        <div v-if="statement.credit_on_account > 0">
          <span class="label">Credit on account</span>
          <strong>{{ formatMoney(statement.credit_on_account) }}</strong>
        </div>
        <div>
          <span class="label">Previous balance</span>
          <strong data-testid="statement-previous">{{ formatMoney(statement.previous_balance) }}</strong>
        </div>
        <div>
          <span class="label">{{ statement.ends_today ? 'Ending balance' : 'Balance at end of period' }}</span>
          <strong data-testid="statement-ending">{{ formatMoney(statement.ending_balance) }}</strong>
        </div>
        <div>
          <span class="label">Open invoices</span>
          <strong>{{ statement.open_invoices.length }}</strong>
        </div>
      </div>

      <Message v-if="stale" severity="info" :closable="false" data-testid="statement-stale">
        Press Show to preview this range. Download and Email stay off until it's previewed.
      </Message>

      <iframe
        v-if="pdfUrl"
        :src="pdfUrl"
        class="statement-preview"
        title="Statement preview"
        data-testid="statement-preview"
      />

      <div class="statement-send">
        <label class="field grow">
          <span>Email to</span>
          <InputText v-model="toEmail" type="email" placeholder="customer@example.com" data-testid="statement-to-email" />
        </label>
        <Button
          label="Download PDF"
          icon="pi pi-download"
          severity="secondary"
          outlined
          :disabled="stale"
          data-testid="statement-download"
          @click="download"
        />
        <Button
          v-if="canSend"
          label="Email statement"
          icon="pi pi-send"
          :loading="sending"
          :disabled="stale || !toEmail"
          data-testid="statement-send"
          @click="send"
        />
      </div>

      <Message v-if="result" :severity="result.email_sent ? 'success' : 'error'" :closable="false" data-testid="statement-result">
        <template v-if="result.email_sent">
          Statement emailed to {{ result.to_email }}{{ result.pdf_attached ? ' with the PDF attached' : '' }}.
        </template>
        <template v-else>{{ skipReasonMessage(result.email_skip_reason) }}</template>
      </Message>
    </template>
  </Dialog>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import Button from 'primevue/button';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import { useApi } from '../composables/useApi';
import { createAuthedBlobUrl, downloadAuthedFile } from '../composables/useAuthedFile';
import { formatMoney } from '../composables/useFormatters';
import { usePermission } from '../composables/usePermission';
import {
  DEFAULT_PRESET,
  skipReasonMessage,
  statementParams,
  recipientPayload,
  statementQuery,
  toIsoDay,
} from '../utils/customerStatement';

const props = defineProps({
  visible: { type: Boolean, default: false },
  customerId: { type: [String, Number], required: true },
});
defineEmits(['update:visible']);

const api = useApi();
const { hasPermission } = usePermission();
const canSend = computed(() => hasPermission('invoices.send'));

// Statements start on 2026-01-01 until the QuickBooks-era payment rows are
// repaired. The server enforces it; this only keeps the inputs from offering
// refused days.
const FLOOR = '2026-01-01';
const floorLabel = 'January 1, 2026';
const todayIso = toIsoDay(new Date());

const preset = ref(DEFAULT_PRESET);
const presetOptions = ref([
  { key: 'last_30', label: 'Last 30 days' },
  { key: 'last_60', label: 'Last 60 days' },
  { key: 'last_90', label: 'Last 90 days' },
  { key: 'ytd', label: 'Year to date' },
  { key: 'custom', label: 'Custom range' },
]);
// Native date inputs: they hand back YYYY-MM-DD with no parsing mid-typing.
// A PrimeVue DatePicker re-formatted a half-typed "2026-07-3" to July 3 and
// dropped the next keystroke (browser walk, 2026-09-15).
const customStart = ref('');
const customEnd = ref('');
const statement = ref(null);
const pdfUrl = ref('');
const loading = ref(false);
const error = ref('');
const toEmail = ref('');
const defaultEmail = ref('');
const sending = ref(false);
const result = ref(null);
let loadSeq = 0;
// What the preview on screen actually covers. Download and Email act on its
// resolved dates, never on the preset key or controls changed since: an edited
// range was once emailed unseen, and a "year to date" previewed at 11:59pm on
// Dec 31 would have been re-resolved into a one-day statement after midnight.
const loadedControls = ref(null);
const loadedRange = ref(null);
const currentParams = computed(() => statementParams(preset.value, customStart.value, customEnd.value));
const stale = computed(
  () => !!statement.value && JSON.stringify(currentParams.value) !== JSON.stringify(loadedControls.value),
);

function base() {
  return `/api/customers/${props.customerId}/statement`;
}

function releasePdf() {
  if (pdfUrl.value) URL.revokeObjectURL(pdfUrl.value);
  pdfUrl.value = '';
}

async function load() {
  const params = statementParams(preset.value, customStart.value, customEnd.value);
  if (!params) return; // a custom range waits for both dates
  const seq = ++loadSeq;
  loading.value = true;
  error.value = '';
  result.value = null;
  try {
    const q = statementQuery(params);
    const data = await api.get(`${base()}?${q}`);
    if (seq !== loadSeq) return;
    statement.value = data;
    loadedControls.value = params;
    loadedRange.value = { start: data.range.start, end: data.range.end };
    if (Array.isArray(data.presets) && data.presets.length) presetOptions.value = data.presets;
    defaultEmail.value = data.default_recipient?.email || '';
    if (!toEmail.value && defaultEmail.value) toEmail.value = defaultEmail.value;
    const blobUrl = await createAuthedBlobUrl(`${base()}/pdf?${q}`);
    if (seq !== loadSeq) {
      URL.revokeObjectURL(blobUrl);
      return;
    }
    releasePdf();
    pdfUrl.value = blobUrl;
  } catch (err) {
    if (seq !== loadSeq) return;
    statement.value = null;
    loadedControls.value = null;
    loadedRange.value = null;
    releasePdf();
    error.value = err?.message || 'The statement could not be produced.';
  } finally {
    if (seq === loadSeq) loading.value = false;
  }
}

function onPresetChange() {
  if (preset.value !== 'custom') load();
}

async function download() {
  const params = loadedRange.value;
  if (!params || stale.value) return;
  try {
    await downloadAuthedFile(
      `${base()}/pdf?${statementQuery({ ...params, download: 'true' })}`,
      statement.value?.pdf_filename || 'statement.pdf',
    );
  } catch (err) {
    error.value = err?.message || 'The PDF could not be downloaded.';
  }
}

async function send() {
  const params = loadedRange.value;
  if (!params || stale.value) return;
  sending.value = true;
  result.value = null;
  try {
    result.value = await api.post(`${base()}/send`, { ...params, ...recipientPayload(toEmail.value, defaultEmail.value) });
  } catch (err) {
    result.value = { email_sent: false, email_skip_reason: err?.message || 'send_failed' };
  } finally {
    sending.value = false;
  }
}

watch(
  () => props.visible,
  (open) => {
    if (open) {
      preset.value = DEFAULT_PRESET;
      customStart.value = '';
      customEnd.value = '';
      toEmail.value = '';
      load();
    } else {
      loadSeq++;
      releasePdf();
      statement.value = null;
      loadedControls.value = null;
      loadedRange.value = null;
      result.value = null;
    }
  },
  { immediate: true },
);

// A custom range loads on the Show button, never on each keystroke.
onBeforeUnmount(releasePdf);
</script>

<style scoped>
.statement-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  align-items: flex-end;
  margin-bottom: 0.75rem;
}
.field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.875rem;
}
.field.grow {
  flex: 1 1 16rem;
}
.floor-note {
  color: var(--p-text-muted-color);
  font-size: 0.8125rem;
  padding-bottom: 0.5rem;
}
.statement-loading {
  display: flex;
  justify-content: center;
  padding: 2rem;
}
.statement-summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(10rem, 1fr));
  gap: 0.75rem;
  margin: 0.75rem 0;
}
.statement-summary .label {
  display: block;
  color: var(--p-text-muted-color);
  font-size: 0.8125rem;
}
.statement-preview {
  width: 100%;
  height: min(70vh, 900px);
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
  background: var(--p-content-background);
}
.statement-send {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
  align-items: flex-end;
  margin-top: 0.75rem;
}
</style>
