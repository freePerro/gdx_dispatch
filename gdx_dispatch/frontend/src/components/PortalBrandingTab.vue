<template>
  <div class="brand-tab" data-testid="branding-tab">
    <div v-if="loading" class="empty-msg">Loading…</div>
    <Message v-else-if="loadError" severity="warn" data-testid="branding-load-error">{{ loadError }}</Message>
    <template v-else>
      <Card data-testid="branding-disclaimer-card">
        <template #title>Your branding is private</template>
        <template #content>
          <p class="disclaimer-text" data-testid="branding-disclaimer-text">{{ disclaimer.text }}</p>
          <p v-if="disclaimer.accepted" class="meta" data-testid="branding-disclaimer-accepted">
            <i class="pi pi-check-circle" /> You agreed to this on {{ formatDateTime(disclaimer.accepted_at) }}.
          </p>
          <div v-else class="action-row">
            <Button
              label="I agree"
              icon="pi pi-check"
              :loading="accepting"
              data-testid="branding-accept-btn"
              @click="accept"
            />
          </div>
          <Message v-if="acceptError" severity="error" class="mt-2" data-testid="branding-accept-error">{{ acceptError }}</Message>
        </template>
      </Card>

      <Card data-testid="branding-form-card">
        <template #title>Your company on the quote</template>
        <template #subtitle>
          This is what your customer sees on the PDF. Our name, notes and terms never appear on it.
        </template>
        <template #content>
          <p v-if="!disclaimer.accepted" class="meta" data-testid="branding-locked-msg">
            Agree to the terms above to set up your branding.
          </p>
          <fieldset class="brand-form" :disabled="!disclaimer.accepted">
            <div class="logo-row">
              <div class="logo-box" data-testid="branding-logo-box">
                <img v-if="logoUrl" :src="logoUrl" alt="Your logo" data-testid="branding-logo-img" />
                <span v-else class="logo-empty">No logo yet</span>
              </div>
              <div class="logo-actions">
                <Button
                  :label="logoUrl ? 'Change logo' : 'Upload logo'"
                  icon="pi pi-upload"
                  severity="secondary"
                  outlined
                  :loading="uploading"
                  :disabled="!disclaimer.accepted"
                  data-testid="branding-logo-btn"
                  @click="pickLogo"
                />
                <small class="meta">PNG, JPEG or WebP, up to 5 MB.</small>
                <small v-if="logoError" class="field-error" data-testid="branding-logo-error">{{ logoError }}</small>
              </div>
            </div>

            <div class="form-grid">
              <label class="field">
                <span>Company name</span>
                <InputText v-model="form.company_name" maxlength="200" data-testid="branding-company-name" />
              </label>
              <label class="field">
                <span>Phone</span>
                <InputText v-model="form.phone" maxlength="50" data-testid="branding-phone" />
              </label>
              <label class="field">
                <span>Email</span>
                <InputText v-model="form.email" type="email" maxlength="200" data-testid="branding-email" />
              </label>
              <label class="field">
                <span>Website</span>
                <InputText v-model="form.website" maxlength="200" data-testid="branding-website" />
              </label>
              <label class="field">
                <span>License number</span>
                <InputText v-model="form.license_no" maxlength="100" data-testid="branding-license" />
              </label>
              <label class="field">
                <span>Default markup</span>
                <InputNumber
                  v-model="form.default_markup_pct"
                  :min="0"
                  :max="500"
                  :max-fraction-digits="2"
                  suffix=" %"
                  data-testid="branding-markup"
                />
                <small v-if="form.default_markup_pct == null" class="field-error" data-testid="branding-markup-required">Enter a default markup; 0 means none.</small>
              </label>
            </div>
            <label class="field">
              <span>Address</span>
              <Textarea v-model="form.address" rows="2" maxlength="500" auto-resize data-testid="branding-address" />
            </label>
            <label class="field">
              <span>Your terms</span>
              <Textarea
                v-model="form.terms_text"
                rows="4"
                maxlength="10000"
                auto-resize
                placeholder="Printed at the bottom of every quote, e.g. payment terms or warranty"
                data-testid="branding-terms"
              />
            </label>
            <div class="action-row">
              <Button
                label="Save branding"
                icon="pi pi-save"
                :loading="saving"
                :disabled="!disclaimer.accepted || form.default_markup_pct == null"
                data-testid="branding-save-btn"
                @click="save"
              />
            </div>
            <Message v-if="saveError" severity="error" data-testid="branding-save-error">{{ saveError }}</Message>
            <Message v-if="savedMsg" severity="success" :closable="true" data-testid="branding-saved" @close="savedMsg = ''">{{ savedMsg }}</Message>
          </fieldset>
        </template>
      </Card>
    </template>

    <input
      ref="fileInput"
      type="file"
      accept="image/png,image/jpeg,image/webp"
      class="hidden-input"
      data-testid="branding-logo-input"
      @change="onLogoPicked"
    />
  </div>
</template>

<script setup>
/**
 * The customer portal's "My Branding" tab, shown to contractor and wholesale
 * accounts only (routers/portal_resale.py). They agree to the privacy terms,
 * then set the name, contact details, logo, terms and default markup that
 * their resale quotes print under. Nothing here is ours: the PDF carries
 * only what they type.
 */
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue';
import Button from 'primevue/button';
import Card from 'primevue/card';
import InputNumber from 'primevue/inputnumber';
import InputText from 'primevue/inputtext';
import Message from 'primevue/message';
import Textarea from 'primevue/textarea';
import { formatDateTime } from '../composables/useFormatters';

const props = defineProps({
  // The portal view's authenticated fetch (JSON; throws with the server's detail).
  fetcher: { type: Function, required: true },
  // The same, returning a Blob: the logo is behind the portal token.
  blobFetcher: { type: Function, required: true },
});
const emit = defineEmits(['changed']);

const FIELDS = ['company_name', 'phone', 'email', 'address', 'website', 'license_no', 'terms_text'];

const loading = ref(true);
const loadError = ref('');
const disclaimer = reactive({ text: '', version: '', accepted: false, accepted_at: null });
const form = reactive({ ...Object.fromEntries(FIELDS.map((f) => [f, ''])), default_markup_pct: 0 });
const accepting = ref(false);
const acceptError = ref('');
const saving = ref(false);
const saveError = ref('');
const savedMsg = ref('');
const uploading = ref(false);
const logoError = ref('');
const logoUrl = ref('');
const fileInput = ref(null);

function apply(profile) {
  Object.assign(disclaimer, profile.disclaimer || {});
  for (const f of FIELDS) form[f] = profile[f] || '';
  form.default_markup_pct = Number(profile.default_markup_pct) || 0;
}

function setLogoUrl(url) {
  if (logoUrl.value) URL.revokeObjectURL(logoUrl.value);
  logoUrl.value = url;
}

async function loadLogo() {
  try {
    setLogoUrl(URL.createObjectURL(await props.blobFetcher('/portal/reseller/logo')));
  } catch {
    setLogoUrl('');
  }
}

async function load() {
  loading.value = true;
  try {
    const profile = await props.fetcher('/portal/reseller/profile');
    apply(profile);
    loadError.value = '';
    if (profile.has_logo) await loadLogo();
  } catch (e) {
    loadError.value = e?.message?.startsWith('request failed') ? 'Could not load your branding.' : (e?.message || 'Could not load your branding.');
  } finally {
    loading.value = false;
  }
}

async function put(body) {
  return props.fetcher('/portal/reseller/profile', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function accept() {
  accepting.value = true;
  acceptError.value = '';
  try {
    apply(await put({ accept_disclaimer_version: disclaimer.version }));
    emit('changed');
  } catch (e) {
    acceptError.value = e?.message || 'Could not save. Try again.';
    // A 409 means the wording changed under them: show the new text.
    if (e?.status === 409) await load();
  } finally {
    accepting.value = false;
  }
}

async function save() {
  // A cleared markup is not 0: saving it would change the default without saying so.
  if (form.default_markup_pct == null) return;
  saving.value = true;
  saveError.value = '';
  savedMsg.value = '';
  try {
    const body = Object.fromEntries(FIELDS.map((f) => [f, form[f] ?? '']));
    body.default_markup_pct = form.default_markup_pct;
    apply(await put(body));
    savedMsg.value = 'Branding saved.';
    emit('changed');
  } catch (e) {
    saveError.value = e?.message?.startsWith('request failed') ? 'Could not save. Check the fields and try again.' : (e?.message || 'Could not save.');
  } finally {
    saving.value = false;
  }
}

function pickLogo() {
  logoError.value = '';
  fileInput.value?.click();
}

async function onLogoPicked(ev) {
  const file = ev.target.files?.[0];
  ev.target.value = '';
  if (!file) return;
  uploading.value = true;
  logoError.value = '';
  try {
    const fd = new FormData();
    fd.append('file', file);
    await props.fetcher('/portal/reseller/logo', { method: 'POST', body: fd });
    await loadLogo();
    emit('changed');
  } catch (e) {
    logoError.value = e?.message?.startsWith('request failed') ? 'That logo could not be uploaded.' : (e?.message || 'That logo could not be uploaded.');
  } finally {
    uploading.value = false;
  }
}

onMounted(load);
onBeforeUnmount(() => setLogoUrl(''));
</script>

<style scoped>
.brand-tab { display: flex; flex-direction: column; gap: 1rem; }
.disclaimer-text { margin: 0 0 0.75rem; line-height: 1.5; }
.brand-form { border: 0; margin: 0; padding: 0; min-width: 0; display: flex; flex-direction: column; gap: 1rem; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 0.85rem; }
.field { display: flex; flex-direction: column; gap: 0.35rem; min-width: 0; }
.field > span { font-size: 0.85rem; font-weight: 600; color: var(--p-text-color, #374151); }
.field :deep(.p-inputtext), .field :deep(.p-inputnumber), .field :deep(.p-textarea) { width: 100%; }
.field-error { color: var(--p-red-500, #ef4444); }
.logo-row { display: flex; flex-wrap: wrap; align-items: center; gap: 1rem; }
/* White in both themes on purpose: the logo prints on a white page, so this
   is how it will look on the quote. Its text colour is fixed to match. */
.logo-box {
  width: 160px; height: 80px; display: flex; align-items: center; justify-content: center;
  border: 1px dashed var(--p-content-border-color, #e5e7eb); border-radius: 8px;
  background: #fff; color: #4b5563; overflow: hidden;
}
.logo-empty { font-size: 0.85rem; }
.logo-box img { max-width: 100%; max-height: 100%; object-fit: contain; }
.logo-actions { display: flex; flex-direction: column; gap: 0.35rem; align-items: flex-start; }
.action-row { display: flex; flex-wrap: wrap; gap: 0.5rem; }
.meta { font-size: 0.85rem; color: var(--p-text-muted-color, #6b7280); }
.empty-msg { text-align: center; padding: 1.5rem; color: var(--p-text-muted-color, #6b7280); }
.hidden-input { display: none; }
@media (max-width: 640px) {
  .form-grid { grid-template-columns: 1fr; }
  .action-row :deep(.p-button) { flex: 1 1 100%; }
}
</style>
