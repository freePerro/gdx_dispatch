<template>
  <!-- Sales-tax exemption for one customer (GDXA-624). Before this card the
       POST/DELETE /api/tax/exemptions routes had no UI caller: the rows were
       read by /api/tax/resolve and seeded every invoice's tax rate, but nobody
       could create one from the app. Admin/owner only — the same gate the tax
       router enforces (`_require_admin`), so nobody sees a control whose only
       outcome is a 403. -->
  <Card v-if="canManage" class="info-card tax-exemption-card" data-testid="customer-tax-exemption-card">
    <template #content>
      <div class="tax-header">
        <div class="tax-title">
          <i class="pi pi-percentage"></i>
          <strong>Sales tax</strong>
          <Tag
            v-if="loaded && !loadError"
            :value="statusLabel"
            :severity="statusSeverity"
            data-testid="tax-exemption-status"
          />
        </div>
        <!-- Only one exemption per customer: the rate resolver reads it with
             scalar_one_or_none, so a second exempt row makes the rate lookup
             raise. The server does NOT refuse a second row today, and
             customer merge re-points every customer_id column, so merging two
             exempt customers leaves two rows. This button and the
             reload-on-error in save() only stop the card from adding one; the
             card still lists every row so a duplicate can be removed. -->
        <Button
          v-if="loaded && !loadError && !exemptions.length"
          label="Mark tax-exempt"
          icon="pi pi-plus"
          size="small"
          outlined
          data-testid="add-tax-exemption-btn"
          @click="openDialog"
        />
      </div>

      <p v-if="!loaded" class="muted tax-line">Loading…</p>
      <p v-else-if="loadError" class="inline-error tax-line" data-testid="tax-exemption-error">{{ loadError }}</p>
      <p v-else-if="!exemptions.length" class="muted tax-line" data-testid="tax-exemption-none">
        Invoices for this customer charge the default sales-tax rate.
      </p>

      <div v-for="e in exemptions" :key="e.id" class="tax-row" :data-testid="`tax-exemption-${e.id}`">
        <div class="tax-details">
          <span><span class="muted">Reason:</span> {{ e.reason || '—' }}</span>
          <span><span class="muted">Certificate:</span> {{ e.certificate_id || '—' }}</span>
          <span v-if="e.exempt_from || e.exempt_until">
            <span class="muted">Valid:</span>
            {{ e.exempt_from ? formatDate(e.exempt_from) : 'any time' }}
            –
            {{ e.exempt_until ? formatDate(e.exempt_until) : 'no end date' }}
          </span>
          <span v-if="e.notes" class="muted">{{ e.notes }}</span>
        </div>
        <Button
          v-tooltip="'Remove exemption'"
          icon="pi pi-trash"
          aria-label="Remove tax exemption"
          text
          rounded
          size="small"
          severity="danger"
          :loading="removingId === e.id"
          :data-testid="`remove-tax-exemption-${e.id}`"
          @click="askRemove(e)"
        />
      </div>
    </template>
  </Card>

  <Dialog
    v-model:visible="showDialog"
    header="Mark customer tax-exempt"
    modal
    :style="{ width: '440px', maxWidth: '95vw' }"
    data-testid="tax-exemption-dialog"
  >
    <p class="muted dialog-hint">
      New invoices for this customer will default to no sales tax while the exemption is in effect.
    </p>
    <div class="form-field">
      <label for="tax-ex-reason">Reason</label>
      <InputText id="tax-ex-reason" v-model="form.reason" maxlength="200" placeholder="e.g. Reseller, non-profit, government" data-testid="tax-ex-reason" />
    </div>
    <div class="form-field">
      <label for="tax-ex-cert">Certificate ID</label>
      <InputText id="tax-ex-cert" v-model="form.certificate_id" maxlength="100" data-testid="tax-ex-cert" />
    </div>
    <div class="form-row">
      <div class="form-field">
        <label for="tax-ex-from">Valid from (optional)</label>
        <InputText id="tax-ex-from" v-model="form.exempt_from" type="date" data-testid="tax-ex-from" />
      </div>
      <div class="form-field">
        <label for="tax-ex-until">Valid until (optional)</label>
        <InputText id="tax-ex-until" v-model="form.exempt_until" type="date" data-testid="tax-ex-until" />
      </div>
    </div>
    <div class="form-field">
      <label for="tax-ex-notes">Notes</label>
      <Textarea id="tax-ex-notes" v-model="form.notes" rows="2" autoResize data-testid="tax-ex-notes" />
    </div>
    <p v-if="formError" class="inline-error" data-testid="tax-ex-form-error">{{ formError }}</p>
    <template #footer>
      <Button label="Cancel" text severity="secondary" @click="showDialog = false" />
      <Button label="Save exemption" icon="pi pi-check" :loading="saving" :disabled="saving" data-testid="save-tax-exemption-btn" @click="save" />
    </template>
  </Dialog>
</template>

<script setup>
import { computed, ref, watch } from "vue";
import Button from "primevue/button";
import Card from "primevue/card";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
import { useApi } from "../composables/useApi";
import { useDestructiveConfirm } from "../composables/useDestructiveConfirm";
import { formatDate } from "../composables/useFormatters";
import { useAuthStore } from "../stores/auth";

const props = defineProps({
  customerId: { type: [String, Number], required: true },
});

const api = useApi();
const auth = useAuthStore();
const { confirmDestructive } = useDestructiveConfirm();

const canManage = computed(() => {
  const role = (auth.user?.role || auth.user?.user_role || "").toLowerCase();
  return role === "admin" || role === "owner";
});

const exemptions = ref([]);
const loaded = ref(false);
const loadError = ref("");
const showDialog = ref(false);
const saving = ref(false);
const formError = ref("");
const removingId = ref(null);
const form = ref(blankForm());

function blankForm() {
  return { reason: "", certificate_id: "", exempt_from: "", exempt_until: "", notes: "" };
}

function sameId(a, b) {
  return String(a || "").toLowerCase() === String(b || "").toLowerCase();
}

function todayIso() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Mirrors modules/tax/service.py::is_customer_exempt so the badge says what
// the rate resolver will do. The resolver uses the shop's calendar day; this
// uses the browser's, which only differs around midnight.
const statusLabel = computed(() => {
  const e = exemptions.value.find((x) => x.exempt);
  if (!e) return exemptions.value.length ? "Not exempt" : "Taxable";
  const today = todayIso();
  if (e.exempt_from && today < e.exempt_from) return "Exempt from " + formatDate(e.exempt_from);
  if (e.exempt_until && today > e.exempt_until) return "Exemption expired";
  return "Tax-exempt";
});
const statusSeverity = computed(() => {
  if (statusLabel.value === "Tax-exempt") return "success";
  if (statusLabel.value === "Taxable") return "secondary";
  return "warn";
});

async function load() {
  if (!canManage.value || !props.customerId) return;
  loadError.value = "";
  try {
    // The router has no per-customer filter, so this reads every exemption
    // and keeps this customer's. Exempt customers are few.
    const rows = await api.get("/api/tax/exemptions", { suppressErrorToast: true });
    exemptions.value = (Array.isArray(rows) ? rows : []).filter((r) => sameId(r.customer_id, props.customerId));
  } catch (e) {
    exemptions.value = [];
    loadError.value = "Could not load this customer's tax exemption.";
  } finally {
    loaded.value = true;
  }
}

function openDialog() {
  form.value = blankForm();
  formError.value = "";
  showDialog.value = true;
}

async function save() {
  formError.value = "";
  const f = form.value;
  if (f.exempt_from && f.exempt_until && f.exempt_until < f.exempt_from) {
    formError.value = "“Valid until” is before “Valid from”.";
    return;
  }
  const trimmed = (v) => (v && String(v).trim()) || null;
  saving.value = true;
  try {
    await api.post(
      "/api/tax/exemptions",
      {
        customer_id: String(props.customerId),
        exempt: true,
        reason: trimmed(f.reason),
        certificate_id: trimmed(f.certificate_id),
        exempt_from: f.exempt_from || null,
        exempt_until: f.exempt_until || null,
        notes: trimmed(f.notes),
      },
      { successMessage: "Customer marked tax-exempt" },
    );
    showDialog.value = false;
    await load();
  } catch {
    // useApi already toasted the server's reason. A POST that timed out may
    // still have committed: reload, and if the row is there, close the dialog
    // so a second click cannot create a duplicate.
    await load();
    if (exemptions.value.length) showDialog.value = false;
  } finally {
    saving.value = false;
  }
}

function askRemove(e) {
  confirmDestructive({
    header: "Remove tax exemption?",
    message:
      "This customer's future invoices will charge the default sales-tax rate. " +
      "Removal is permanent: the exemption is deleted, and the audit log is the only record that it existed.",
    acceptLabel: "Remove",
    accept: async () => {
      removingId.value = e.id;
      try {
        await api.del(`/api/tax/exemptions/${encodeURIComponent(e.id)}`, { successMessage: "Tax exemption removed" });
        await load();
      } catch {
        // toasted by useApi
      } finally {
        removingId.value = null;
      }
    },
  });
}

// canManage too: the auth store can fill in after the card mounts.
watch([() => props.customerId, canManage], load, { immediate: true });
</script>

<style scoped>
.tax-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
}
.tax-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.tax-line {
  margin: 0.5rem 0 0;
}
.tax-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 0.75rem;
  margin-top: 0.5rem;
}
.tax-details {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}
.dialog-hint {
  margin-top: 0;
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  margin-bottom: 0.75rem;
  flex: 1;
}
.form-row {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
}
</style>
