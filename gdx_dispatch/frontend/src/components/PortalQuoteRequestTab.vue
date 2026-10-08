<template>
  <div class="qr-tab" data-testid="quote-request-tab">
    <Card ref="formCard" class="qr-form-card">
      <template #title>
        <span v-if="editingId" data-testid="qr-form-title">Change “{{ editingName }}”</span>
        <span v-else data-testid="qr-form-title">Request a quote for new doors</span>
      </template>
      <template #subtitle>Tell us about each door. Sizes can be approximate; we measure before we order.</template>
      <template #content>
        <div class="qr-form">
          <label class="field">
            <span>Job name</span>
            <InputText v-model="form.job_name" maxlength="200" placeholder="e.g. Lake cabin, front garage" data-testid="qr-job-name" />
            <small v-if="errors.job_name" class="field-error" data-testid="qr-job-name-error">{{ errors.job_name }}</small>
          </label>
          <label class="field">
            <span>Where are the doors?</span>
            <InputText v-model="form.site_address" maxlength="500" placeholder="Leave blank if it's your address on file" data-testid="qr-site-address" />
          </label>

          <div
            v-for="(door, i) in doors"
            :key="door._key"
            class="door-card"
            :data-testid="`qr-door-${i}`"
          >
            <div class="door-card-head">
              <span class="door-card-title">Door {{ i + 1 }}</span>
              <Button
                v-if="doors.length > 1"
                label="Remove"
                icon="pi pi-trash"
                text
                size="small"
                severity="danger"
                :data-testid="`qr-door-remove-${i}`"
                @click="removeDoor(i)"
              />
            </div>

            <div class="size-row">
              <div class="size-group">
                <span class="size-label">Width</span>
                <div class="size-inputs">
                  <InputNumber v-model="door.width_ft" :min="4" :max="30" :use-grouping="false" suffix=" ft" placeholder="ft" input-class="size-input" :data-testid="`qr-door-width-ft-${i}`" />
                  <InputNumber v-model="door.width_in" :min="0" :max="11" :use-grouping="false" suffix=" in" placeholder="in" input-class="size-input" :data-testid="`qr-door-width-in-${i}`" />
                </div>
              </div>
              <div class="size-group">
                <span class="size-label">Height</span>
                <div class="size-inputs">
                  <InputNumber v-model="door.height_ft" :min="5" :max="20" :use-grouping="false" suffix=" ft" placeholder="ft" input-class="size-input" :data-testid="`qr-door-height-ft-${i}`" />
                  <InputNumber v-model="door.height_in" :min="0" :max="11" :use-grouping="false" suffix=" in" placeholder="in" input-class="size-input" :data-testid="`qr-door-height-in-${i}`" />
                </div>
              </div>
              <div class="size-group qty-group">
                <span class="size-label">How many like this?</span>
                <InputNumber v-model="door.quantity" :min="1" :max="10" :use-grouping="false" input-class="size-input" :data-testid="`qr-door-qty-${i}`" />
              </div>
            </div>
            <small v-if="errors.doors[i]" class="field-error" :data-testid="`qr-door-error-${i}`">{{ errors.doors[i] }}</small>

            <div class="question-grid">
              <label v-for="q in DOOR_QUESTIONS" :key="q.key" class="field">
                <span>{{ q.label }}</span>
                <Select
                  v-model="door[q.key]"
                  :options="q.options"
                  option-label="label"
                  option-value="value"
                  :data-testid="`qr-door-${q.key}-${i}`"
                />
              </label>
              <label class="field">
                <span>Color</span>
                <InputText v-model="door.color" maxlength="60" placeholder="e.g. white, almond, match the trim" :data-testid="`qr-door-color-${i}`" />
              </label>
            </div>
            <label class="field">
              <span>Anything else about this door?</span>
              <Textarea v-model="door.notes" rows="2" maxlength="1000" auto-resize :data-testid="`qr-door-notes-${i}`" />
            </label>

            <div class="photo-row">
              <span v-if="door._existingPhotos" class="meta" :data-testid="`qr-door-existing-photos-${i}`">
                {{ door._existingPhotos }} photo{{ door._existingPhotos === 1 ? '' : 's' }} already sent
              </span>
              <Button
                v-if="door._existingPhotos + door._files.length < MAX_PHOTOS_PER_DOOR"
                label="Add photos"
                icon="pi pi-camera"
                outlined
                size="small"
                :data-testid="`qr-door-photos-${i}`"
                @click="pickPhotos({ kind: 'form', door })"
              />
              <span class="meta">A photo of the opening from inside and outside helps.</span>
              <ul v-if="door._files.length" class="file-list">
                <li v-for="(f, fi) in door._files" :key="fi" :data-testid="`qr-door-file-${i}-${fi}`">
                  <i class="pi pi-image" /> <span class="file-name">{{ f.name }}</span>
                  <Button icon="pi pi-times" text rounded size="small" :aria-label="`Remove ${f.name}`" @click="door._files.splice(fi, 1)" />
                </li>
              </ul>
            </div>
          </div>

          <Button
            v-if="doors.length < MAX_DOORS"
            label="Add another door"
            icon="pi pi-plus"
            outlined
            class="add-door-btn"
            data-testid="qr-add-door"
            @click="addDoor"
          />

          <label class="field">
            <span>Anything else we should know?</span>
            <Textarea v-model="form.notes" rows="3" maxlength="5000" auto-resize data-testid="qr-notes" />
          </label>

          <Message v-if="editingId && removesPhotos" severity="warn" :closable="false" data-testid="qr-edit-photo-warning">
            Removing a door also removes the photos you sent of it.
          </Message>
          <Message v-if="submitError" severity="error" :closable="false" data-testid="qr-submit-error">{{ submitError }}</Message>
          <div class="submit-row">
            <Button
              :label="editingId ? 'Save changes' : 'Send request'"
              :icon="editingId ? 'pi pi-check' : 'pi pi-send'"
              class="submit-btn"
              :loading="submitting"
              data-testid="qr-submit"
              @click="submit"
            />
            <Button
              v-if="editingId"
              label="Cancel"
              severity="secondary"
              text
              class="submit-btn"
              data-testid="qr-edit-cancel"
              @click="cancelEdit"
            />
          </div>
        </div>
      </template>
    </Card>

    <Message v-if="doneMessage" severity="success" :closable="true" data-testid="qr-sent" @close="doneMessage = ''">
      {{ doneMessage }}
    </Message>

    <div v-if="failedUploads.length" class="upload-failures" data-testid="qr-upload-failures">
      <Message severity="warn" :closable="false">
        {{ failedUploads.length }} photo{{ failedUploads.length === 1 ? '' : 's' }} didn't upload. Your request was still sent.
      </Message>
      <ul class="file-list">
        <li v-for="u in failedUploads" :key="u.id" :data-testid="`qr-upload-failed-${u.id}`">
          <i class="pi pi-exclamation-triangle" />
          <span class="file-name">{{ u.file.name }} — {{ u.error }}</span>
          <Button label="Retry" size="small" text :loading="u.busy" @click="retryUpload(u)" />
        </li>
      </ul>
    </div>

    <section class="qr-list" data-testid="qr-list">
      <h3 class="qr-list-title">Your requests</h3>
      <Message v-if="actionError" severity="error" :closable="true" data-testid="qr-action-error" @close="actionError = ''">{{ actionError }}</Message>
      <div v-if="listError" class="empty-msg">{{ listError }}</div>
      <div v-else-if="!requests.length" class="empty-msg" data-testid="qr-list-empty">You haven't sent a quote request yet.</div>
      <div v-else class="qr-list-cards">
        <Card v-for="r in requests" :key="r.id" class="qr-request" :data-testid="`qr-request-${r.id}`">
          <template #title>
            <div class="card-title-row">
              <span>{{ r.job_name }}</span>
              <Tag :value="r.status" :severity="statusSeverity(r.status)" :data-testid="`qr-request-status-${r.id}`" />
            </div>
          </template>
          <template #content>
            <p class="meta" :data-testid="`qr-request-sent-${r.id}`">Sent {{ formatDateTime(r.created_at) }}<span v-if="r.site_address"> · {{ r.site_address }}</span></p>
            <p v-if="r.withdrawn_at" class="meta stamp-withdrawn" :data-testid="`qr-request-withdrawn-${r.id}`">
              <i class="pi pi-ban" /> Withdrawn {{ formatDateTime(r.withdrawn_at) }}
            </p>
            <p v-if="r.edited_at" class="meta" :data-testid="`qr-request-edited-${r.id}`">
              <i class="pi pi-pencil" /> Changed {{ formatDateTime(r.edited_at) }}
            </p>
            <ul class="door-summary">
              <li v-for="d in r.doors" :key="d.index" :data-testid="`qr-request-door-${r.id}-${d.index}`">
                <div class="door-summary-main">
                  <strong>{{ doorTitle(d) }}</strong>
                  <span v-if="doorAnswers(d).length" class="meta">{{ doorAnswers(d).map((a) => a.answer).join(' · ') }}</span>
                </div>
                <div class="door-summary-photos">
                  <span class="meta">{{ d.photo_ids.length }} photo{{ d.photo_ids.length === 1 ? '' : 's' }}</span>
                  <Button
                    v-if="!r.withdrawn_at && d.photo_ids.length < MAX_PHOTOS_PER_DOOR"
                    label="Add photos"
                    icon="pi pi-camera"
                    text
                    size="small"
                    :loading="addingTo === `${r.id}:${d.index}`"
                    :data-testid="`qr-request-add-photos-${r.id}-${d.index}`"
                    @click="pickPhotos({ kind: 'existing', request: r, door: d })"
                  />
                </div>
              </li>
            </ul>
            <p v-if="r.status === 'Quote ready'" class="meta" :data-testid="`qr-request-quote-ready-${r.id}`">
              Your quote is on the Estimates tab — accept or decline it there.
            </p>
            <div v-if="r.can_edit || r.can_withdraw" class="request-actions">
              <Button
                v-if="r.can_edit"
                label="Change request"
                icon="pi pi-pencil"
                outlined
                size="small"
                :disabled="Boolean(editingId)"
                :data-testid="`qr-request-edit-${r.id}`"
                @click="startEdit(r)"
              />
              <Button
                v-if="r.can_withdraw"
                label="Withdraw"
                icon="pi pi-ban"
                severity="danger"
                text
                size="small"
                :loading="withdrawing === r.id"
                :data-testid="`qr-request-withdraw-${r.id}`"
                @click="confirmWithdraw(r)"
              />
            </div>
          </template>
        </Card>
      </div>
    </section>

    <!-- One picker for every "Add photos" button. The explicit types (not
         image/*) make an iPhone hand over a JPEG instead of a HEIC the server
         would refuse. -->
    <input
      ref="fileInput"
      type="file"
      accept="image/jpeg,image/png,image/webp"
      multiple
      class="hidden-input"
      data-testid="qr-file-input"
      @change="onFilesPicked"
    />
    <!-- The portal renders without AppLayout, which mounts the app's only
         ConfirmDialog; the withdraw confirm needs one here. -->
    <ConfirmDialog />
  </div>
</template>

<script setup>
/**
 * The customer portal's "Request a quote" tab: a request names a job and
 * lists any number of doors, each with optional photos. Submitting opens a
 * Lead for the office (routers/quote_requests.py). Photos upload one at a
 * time AFTER the request is saved, so a dropped connection on a phone costs
 * a photo, never the request — and a failed photo can be retried here or
 * added later from "Your requests".
 */
import { computed, nextTick, onMounted, reactive, ref } from 'vue';
import Button from 'primevue/button';
import Card from 'primevue/card';
import ConfirmDialog from 'primevue/confirmdialog';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Select from 'primevue/select';
import Tag from 'primevue/tag';
import Textarea from 'primevue/textarea';
import { formatDateTime } from '../composables/useFormatters';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
import {
  DOOR_QUESTIONS,
  MAX_DOORS,
  MAX_PHOTOS_PER_DOOR,
  blankDoor,
  doorAnswers,
  doorTitle,
} from './quoteRequestOptions';

const props = defineProps({
  // The portal view's authenticated fetch: adds the portal Bearer token,
  // throws an Error carrying the server's detail on a non-2xx.
  fetcher: { type: Function, required: true },
});

let nextKey = 0;
function newDoor(from = null) {
  // source_index: where this door sat in the request being changed (null for
  // a door added now), so its photos follow it on save.
  return reactive({
    ...blankDoor(),
    ...(from || {}),
    source_index: from ? from.index : null,
    _key: nextKey++,
    _files: [],
    _existingPhotos: from ? from.photo_ids.length : 0,
  });
}

const form = reactive({ job_name: '', site_address: '', notes: '' });
const doors = ref([newDoor()]);
const errors = reactive({ job_name: '', doors: {} });
const submitting = ref(false);
const submitError = ref('');
const doneMessage = ref('');
const editingId = ref('');
const editingName = ref('');
const withdrawing = ref('');
const actionError = ref('');
const formCard = ref(null);
const { confirmDestructive } = useDestructiveConfirm();
const failedUploads = ref([]);
const requests = ref([]);
const listError = ref('');
const addingTo = ref('');
const fileInput = ref(null);
let pickTarget = null;
let uploadSeq = 0;

function statusSeverity(s) {
  return {
    Received: 'info',
    'Being priced': 'info',
    'Quote ready': 'success',
    Accepted: 'success',
    Declined: 'secondary',
    Expired: 'secondary',
    Closed: 'secondary',
    Withdrawn: 'warn',
  }[s] || 'secondary';
}

// Doors of the request being changed that are no longer in the form and had
// photos — saving soft-deletes those photos.
const removesPhotos = computed(() => {
  const kept = new Set(doors.value.map((d) => d.source_index).filter((x) => x !== null));
  const original = requests.value.find((r) => r.id === editingId.value);
  return Boolean(original?.doors.some((d) => d.photo_ids.length && !kept.has(d.index)));
});

const DOOR_FIELDS = [
  'quantity', 'width_ft', 'width_in', 'height_ft', 'height_in', 'placement', 'material',
  'style', 'insulation', 'windows', 'color', 'opener', 'notes',
];

function startEdit(r) {
  editingId.value = r.id;
  editingName.value = r.job_name;
  form.job_name = r.job_name;
  form.site_address = r.site_address || '';
  form.notes = r.notes || '';
  doors.value = r.doors.map((d) => newDoor({
    ...Object.fromEntries(DOOR_FIELDS.map((k) => [k, d[k] ?? null])),
    index: d.index,
    photo_ids: d.photo_ids,
  }));
  errors.job_name = '';
  errors.doors = {};
  submitError.value = '';
  doneMessage.value = '';
  nextTick(() => formCard.value?.$el?.scrollIntoView?.({ behavior: 'smooth', block: 'start' }));
}

function cancelEdit() {
  resetForm();
}

function confirmWithdraw(r) {
  confirmDestructive({
    header: 'Withdraw this request?',
    message: `“${r.job_name}” will be withdrawn and we'll stop working on it. To ask again later, send a new request.`,
    acceptLabel: 'Withdraw',
    rejectLabel: 'Keep it',
    accept: () => withdraw(r),
  });
}

async function withdraw(r) {
  actionError.value = '';
  withdrawing.value = r.id;
  try {
    await props.fetcher(`/portal/quote-requests/${r.id}/withdraw`, { method: 'POST' });
    if (editingId.value === r.id) resetForm();
    doneMessage.value = `“${r.job_name}” was withdrawn.`;
  } catch (e) {
    doneMessage.value = '';
    actionError.value = e?.message || 'The request could not be withdrawn.';
  } finally {
    withdrawing.value = '';
    await loadRequests();
  }
}

function addDoor() {
  if (doors.value.length < MAX_DOORS) doors.value.push(newDoor());
}
function removeDoor(i) {
  doors.value.splice(i, 1);
  errors.doors = {};
}

function pickPhotos(target) {
  pickTarget = target;
  if (fileInput.value) {
    fileInput.value.value = '';
    fileInput.value.click();
  }
}

async function onFilesPicked(event) {
  const files = Array.from(event.target?.files || []);
  const target = pickTarget;
  pickTarget = null;
  if (!files.length || !target) return;
  if (target.kind === 'form') {
    const room = MAX_PHOTOS_PER_DOOR - target.door._existingPhotos - target.door._files.length;
    target.door._files.push(...files.slice(0, Math.max(room, 0)));
    return;
  }
  // A door on a request already sent: upload now.
  const { request, door } = target;
  const room = MAX_PHOTOS_PER_DOOR - door.photo_ids.length;
  addingTo.value = `${request.id}:${door.index}`;
  try {
    for (const file of files.slice(0, Math.max(room, 0))) {
      const u = { id: uploadSeq++, requestId: request.id, doorIndex: door.index, file, error: '', busy: false };
      if (!(await upload(u))) failedUploads.value.push(u);
    }
  } finally {
    addingTo.value = '';
    await loadRequests();
  }
}

async function upload(u) {
  const body = new FormData();
  body.append('door_index', String(u.doorIndex));
  body.append('file', u.file);
  try {
    await props.fetcher(`/portal/quote-requests/${u.requestId}/photos`, { method: 'POST', body });
    return true;
  } catch (e) {
    u.error = e?.message || 'upload failed';
    return false;
  }
}

async function retryUpload(u) {
  u.busy = true;
  const ok = await upload(u);
  u.busy = false;
  if (ok) {
    failedUploads.value = failedUploads.value.filter((x) => x.id !== u.id);
    await loadRequests();
  }
}

function validate() {
  errors.job_name = form.job_name.trim() ? '' : 'Give the job a name so we can tell your requests apart.';
  const doorErrors = {};
  doors.value.forEach((d, i) => {
    if (!d.width_ft || !d.height_ft) doorErrors[i] = 'Enter a width and height in feet (approximate is fine).';
    else if (!d.quantity) doorErrors[i] = 'Say how many doors like this (1 or more).';
  });
  errors.doors = doorErrors;
  return !errors.job_name && !Object.keys(doorErrors).length;
}

function payload() {
  return {
    job_name: form.job_name.trim(),
    site_address: form.site_address,
    notes: form.notes,
    doors: doors.value.map(({ _key, _files, _existingPhotos, source_index, ...d }) => ({
      ...d,
      width_in: d.width_in ?? 0,
      height_in: d.height_in ?? 0,
      quantity: d.quantity,
      ...(editingId.value ? { source_index } : {}),
    })),
  };
}

function resetForm() {
  form.job_name = '';
  form.site_address = '';
  form.notes = '';
  editingId.value = '';
  editingName.value = '';
  doors.value = [newDoor()];
  errors.job_name = '';
  errors.doors = {};
}

async function submit() {
  submitError.value = '';
  if (!validate()) return;
  submitting.value = true;
  const editing = editingId.value;
  try {
    const created = await props.fetcher(editing ? `/portal/quote-requests/${editing}` : '/portal/quote-requests', {
      method: editing ? 'PATCH' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload()),
    });
    const pending = [];
    doors.value.forEach((d, i) => {
      for (const file of d._files) {
        pending.push({ id: uploadSeq++, requestId: created.id, doorIndex: i, file, error: '', busy: false });
      }
    });
    const failed = [];
    for (const u of pending) {
      if (!(await upload(u))) failed.push(u);
    }
    failedUploads.value = failed;
    doneMessage.value = editing
      ? `Changes to “${created.job_name}” saved. We've let the office know.`
      : `Request for “${created.job_name}” sent. We'll be in touch.`;
    resetForm();
    await loadRequests();
  } catch (e) {
    const what = editing ? 'Your changes could not be saved.' : 'Your request could not be sent.';
    submitError.value = e?.message?.startsWith('request failed')
      ? `${what} Check the doors and try again.`
      : e?.message || what;
  } finally {
    submitting.value = false;
  }
}

async function loadRequests() {
  try {
    const data = await props.fetcher('/portal/quote-requests');
    requests.value = Array.isArray(data) ? data : [];
    listError.value = '';
  } catch {
    listError.value = 'Could not load your requests.';
  }
}

onMounted(loadRequests);
</script>

<style scoped>
.qr-tab { display: flex; flex-direction: column; gap: 1rem; }
.qr-form { display: flex; flex-direction: column; gap: 1rem; }
.field { display: flex; flex-direction: column; gap: 0.35rem; min-width: 0; }
.field > span { font-size: 0.85rem; font-weight: 600; color: var(--p-text-color, #374151); }
.field :deep(.p-inputtext), .field :deep(.p-select), .field :deep(.p-textarea) { width: 100%; }
.field-error { color: var(--p-red-500, #ef4444); }
.door-card {
  display: flex; flex-direction: column; gap: 0.85rem;
  padding: 1rem; border-radius: 8px;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  background: var(--p-content-hover-background, transparent);
}
.door-card-head { display: flex; justify-content: space-between; align-items: center; }
.door-card-title { font-weight: 700; }
.size-row { display: flex; flex-wrap: wrap; gap: 1rem; }
.size-group { display: flex; flex-direction: column; gap: 0.35rem; }
.size-label { font-size: 0.85rem; font-weight: 600; }
.size-inputs { display: flex; gap: 0.5rem; }
.size-inputs :deep(.size-input), .qty-group :deep(.size-input) { width: 5.5rem; }
.question-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 0.85rem; }
.photo-row { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5rem; }
.file-list { list-style: none; margin: 0; padding: 0; width: 100%; display: flex; flex-direction: column; gap: 0.25rem; }
.file-list li { display: flex; align-items: center; gap: 0.4rem; font-size: 0.85rem; }
.file-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1; }
.add-door-btn { align-self: flex-start; }
.submit-row { display: flex; flex-wrap: wrap; gap: 0.5rem; }
.request-actions { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.75rem; }
.stamp-withdrawn { color: var(--p-orange-500, #f97316); font-weight: 600; }
.submit-btn { align-self: flex-start; }
.meta { font-size: 0.85rem; color: var(--p-text-muted-color, #6b7280); }
.empty-msg { text-align: center; padding: 1.5rem; color: var(--p-text-muted-color, #6b7280); }
.qr-list-title { margin: 0.5rem 0; font-size: 1.1rem; }
.qr-list-cards { display: flex; flex-direction: column; gap: 0.75rem; }
.card-title-row { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; }
.door-summary { list-style: none; margin: 0.5rem 0 0; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }
.door-summary li {
  display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; flex-wrap: wrap;
  padding-top: 0.5rem; border-top: 1px solid var(--p-content-border-color, #e5e7eb);
}
.door-summary-main { display: flex; flex-direction: column; gap: 0.15rem; min-width: 0; }
.door-summary-photos { display: flex; align-items: center; gap: 0.25rem; }
.hidden-input { display: none; }
@media (max-width: 640px) {
  .add-door-btn, .submit-btn { align-self: stretch; }
  .submit-row .submit-btn { flex: 1 1 100%; }
  .question-grid { grid-template-columns: 1fr; }
}
</style>
