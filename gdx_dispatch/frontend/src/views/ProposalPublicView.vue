<template>
  <div class="proposal-wrapper" data-testid="proposal-public-root">
    <header class="proposal-header">
      <div class="logo-container">
        <i class="pi pi-building" style="font-size: 1.5rem" />
        <span class="company-name">{{ company.name || "Estimate" }}</span>
      </div>
    </header>

    <main class="proposal-content">
      <div v-if="loading" class="loading-wrap"><ProgressSpinner /></div>

      <!-- Wrong/expired-link 404: friendly dead end, no probe surface. -->
      <Card v-else-if="notFound" class="state-card" data-testid="proposal-not-found">
        <template #title>This link isn't available</template>
        <template #content>
          <p class="meta">
            This estimate link is invalid or no longer available. Reply to the
            email you received<template v-if="company.phone"> or call us at
            <a :href="`tel:${company.phone}`">{{ company.phone }}</a></template> and
            we'll sort it out.
          </p>
        </template>
      </Card>

      <template v-else-if="data">
        <Card class="estimate-card" data-testid="proposal-card">
          <template #title>
            <div class="card-title-row">
              <span>{{ est.label || `Estimate ${est.estimate_number}` }}</span>
              <Tag :value="statusLabel" :severity="statusSeverity" data-testid="proposal-status" />
            </div>
          </template>
          <template #subtitle>
            <span v-if="est.estimate_number">Estimate #{{ est.estimate_number }}</span>
          </template>
          <template #content>
            <p v-if="est.jobsite_address" class="meta">
              <i class="pi pi-map-marker" /> {{ est.jobsite_address }}
            </p>
            <p v-if="est.description" class="description">{{ est.description }}</p>

            <!-- Pictures of the doors being quoted (2026-08-18): the estimate's
                 photo attachments — the same set the PDF prints — inlined by
                 the backend as downscaled data URIs. Label = door size. -->
            <div v-if="photos.length" class="photo-grid" data-testid="proposal-photos">
              <button
                v-for="(p, i) in photos"
                :key="i"
                type="button"
                class="photo-tile"
                :data-testid="`proposal-photo-${i}`"
                @click="lightboxPhoto = p"
              >
                <img :src="p.src" :alt="p.label || 'Estimate photo'" loading="lazy" />
                <span v-if="p.label" class="photo-label" data-testid="proposal-photo-label">{{ p.label }}</span>
              </button>
            </div>

            <!-- Good/better/best: per-tier prices ONLY. There is deliberately
                 no combined total — the backend omits it because the totals
                 engine is tier-blind and est.total can be the highest tier. -->
            <div v-if="est.proposal_mode && tiers.length" class="tier-grid" data-testid="tier-grid">
              <button
                v-for="t in tiers"
                :key="t.id"
                type="button"
                class="tier-card"
                :class="{ selected: selectedTierId === t.id, accepted: est.accepted_tier_id === t.id }"
                :disabled="!isOpen"
                :data-testid="`tier-${t.tier_name}`"
                @click="selectedTierId = t.id"
              >
                <span class="tier-name">{{ tierLabel(t.tier_name) }}</span>
                <span class="tier-price">{{ currency(t.total_price) }}</span>
                <span v-if="t.description" class="meta">{{ t.description }}</span>
                <ul v-if="t.lines?.length" class="tier-included" :data-testid="`tier-lines-${t.tier_name}`">
                  <li v-for="(ln, i) in t.lines" :key="i" class="meta">
                    {{ ln.quantity > 1 ? `${ln.quantity}× ` : "" }}{{ ln.description }}<template
                      v-if="ln.line_total != null"> — {{ currency(ln.line_total) }}</template>
                  </li>
                </ul>
                <span v-if="t.warranty_months" class="meta">{{ t.warranty_months }} month warranty</span>
                <Tag v-if="est.accepted_tier_id === t.id" value="Selected" severity="success" />
              </button>
            </div>

            <template v-else>
              <DataTable v-if="lines.length" :value="lines" class="lines-table" data-testid="lines-table">
                <Column field="description" header="Description" />
                <Column field="quantity" header="Qty" style="width: 4.5rem" />
                <template v-if="!est.hide_line_prices">
                  <Column header="Price" style="width: 7rem">
                    <template #body="{ data: row }">{{ currency(row.unit_price) }}</template>
                  </Column>
                  <Column header="Total" style="width: 7rem">
                    <template #body="{ data: row }">{{ currency(row.line_total) }}</template>
                  </Column>
                </template>
              </DataTable>
              <div v-if="totals" class="totals-block" data-testid="totals-block">
                <div class="totals-row"><span>Subtotal</span><span>{{ currency(totals.subtotal) }}</span></div>
                <div v-if="totals.discount" class="totals-row"><span>Discount</span><span>−{{ currency(totals.discount) }}</span></div>
                <div v-if="totals.tax" class="totals-row"><span>Tax ({{ totals.tax_rate_pct }}%)</span><span>{{ currency(totals.tax) }}</span></div>
                <div class="totals-row grand"><span>Total</span><span data-testid="grand-total">{{ currency(totals.total) }}</span></div>
              </div>
            </template>

            <p v-if="est.valid_until && isOpen" class="meta valid-until">
              Valid until {{ formatDate(est.valid_until) }}
            </p>

            <!-- Open → the QuickBooks moment.
                 Sticky on phones (2026-08-26). A customer reported "I see the
                 number but I can't action anything": on a 390×664 iPhone
                 viewport this page runs ~985px tall once the door photos and
                 line prices render, so BOTH decision buttons — and the grand
                 total — sat ~180px below the fold, under a table that looks
                 like the end of the document. Nothing on the first screen said
                 there was anything to do, and the server logs bear it out: two
                 visits, zero accept/decline POSTs. The bar carries the total
                 with it so the amount being agreed to is never off-screen from
                 the button that agrees to it. -->
            <div v-if="isOpen" class="action-bar" data-testid="open-actions">
              <div v-if="confirmAmount !== null" class="action-bar-total" data-testid="action-bar-total">
                <span class="meta">Total</span>
                <span class="action-bar-amount">{{ currency(confirmAmount) }}</span>
              </div>
              <div class="action-row">
                <Button
                  label="Accept estimate"
                  icon="pi pi-check"
                  severity="success"
                  class="flex-1"
                  :disabled="needsTierPick || totalsMissing"
                  :loading="busy"
                  data-testid="accept-btn"
                  @click="confirmOpen = true"
                />
                <Button
                  label="Decline"
                  icon="pi pi-times"
                  severity="secondary"
                  outlined
                  :loading="busy"
                  data-testid="decline-btn"
                  @click="declineOpen = true"
                />
              </div>
            </div>
            <p v-if="needsTierPick" class="meta">Choose a package above to accept.</p>
            <p v-if="totalsMissing" class="meta" data-testid="totals-missing-msg">
              We can't display your total right now — reply to the email or call us to accept.
            </p>

            <Message v-else-if="est.status === 'accepted'" severity="success" :closable="false" data-testid="accepted-msg">
              Estimate accepted{{ est.accepted_at ? ` on ${formatDate(est.accepted_at)}` : "" }} — thank you!
              We'll be in touch to schedule the work.
            </Message>
            <Message v-else-if="est.status === 'declined'" severity="warn" :closable="false" data-testid="declined-msg">
              You declined this estimate. Changed your mind? Reply to the email
              you received<template v-if="company.phone"> or call {{ company.phone }}</template>.
            </Message>
            <Message v-else-if="est.status === 'expired'" severity="warn" :closable="false" data-testid="expired-msg">
              This estimate has expired — contact us for a current quote<template v-if="company.phone">
              at {{ company.phone }}</template>.
            </Message>

            <!-- Accept-then-abandon recovery: a still-owed deposit invoice
                 resurfaces its pay button on every load, not just in the
                 accept dialog. -->
            <div v-if="est.status === 'accepted' && deposit?.pay_url" class="action-row deposit-row">
              <Button
                :label="`Pay ${currency(deposit.balance_due)} deposit`"
                icon="pi pi-credit-card"
                severity="success"
                class="flex-1"
                data-testid="pay-deposit-btn"
                @click="openPayUrl(deposit.pay_url)"
              />
            </div>
            <!-- Deposit ASK (2026-08-18): the amount we'd like — NO invoice
                 exists yet; clicking mints it via /deposit/pay and goes
                 straight to the payment page. Only online payment counts;
                 check/cash is recorded by the office when it arrives. -->
            <div v-else-if="est.status === 'accepted' && depositAsk" class="action-row deposit-row">
              <Button
                :label="`Pay ${currency(depositAsk.amount)} deposit`"
                icon="pi pi-credit-card"
                severity="success"
                class="flex-1"
                :loading="busy"
                data-testid="pay-deposit-ask-btn"
                @click="startDepositPay"
              />
            </div>
          </template>
        </Card>
      </template>

      <!-- Accept confirmation: quotes the exact amount being agreed to — the
           selected tier's price for proposals, the tax-inclusive total else. -->
      <Dialog v-model:visible="confirmOpen" header="Accept this estimate?" :modal="true"
        :style="{ width: 'min(440px, 94vw)' }" data-testid="accept-confirm-dialog">
        <p class="meta">
          You're accepting estimate <b>#{{ est?.estimate_number }}</b>
          <template v-if="confirmAmount !== null"> for <b>{{ currency(confirmAmount) }}</b></template>
          <template v-if="selectedTier"> ({{ tierLabel(selectedTier.tier_name) }} package)</template>.
          We'll get your job scheduled once it's accepted.
        </p>
        <template #footer>
          <Button label="Cancel" text @click="confirmOpen = false" data-testid="accept-cancel" />
          <Button label="Accept" icon="pi pi-check" severity="success" :loading="busy"
            data-testid="accept-confirm" @click="accept" />
        </template>
      </Dialog>

      <Dialog v-model:visible="declineOpen" header="Decline this estimate?" :modal="true"
        :style="{ width: 'min(440px, 94vw)' }" data-testid="decline-dialog">
        <p class="meta">Optional: let us know why, so we can do better.</p>
        <Textarea v-model="declineReason" rows="3" class="w-full" maxlength="2000"
          placeholder="Your reason (optional)" data-testid="decline-reason" />
        <template #footer>
          <Button label="Cancel" text @click="declineOpen = false" data-testid="decline-cancel" />
          <Button label="Decline estimate" icon="pi pi-times" severity="danger" :loading="busy"
            data-testid="decline-confirm" @click="decline" />
        </template>
      </Dialog>

      <!-- Deposit prompt right after accepting. Two shapes: a legacy/live
           invoice (has invoice_number + pay_url) or the ASK (amount only —
           the invoice is minted when "Pay deposit now" is clicked). -->
      <Dialog v-model:visible="depositPromptOpen" header="Deposit Due" :modal="true"
        :style="{ width: 'min(440px, 94vw)' }" data-testid="deposit-pay-dialog">
        <p class="meta">
          Thanks for accepting! A deposit of <b>{{ currency(depositPrompt?.amount) }}</b> is due now
          to get your job on the schedule<template v-if="depositPrompt?.invoice_number">
          (invoice {{ depositPrompt.invoice_number }})</template>.
        </p>
        <p class="meta">You can pay securely online by card — or pay later; the button stays on this page.</p>
        <details class="pay-by-check" data-testid="deposit-pay-by-check">
          <summary>Paying by check?</summary>
          <p class="meta">
            Make it out to <b>{{ company.name }}</b> and write
            <template v-if="depositPrompt?.invoice_number">invoice
              <b>{{ depositPrompt.invoice_number }}</b></template>
            <template v-else>estimate <b>#{{ est?.estimate_number }}</b></template>
            on the memo line.
          </p>
          <p class="meta">We'll record it when it arrives — no need to do anything else here.</p>
        </details>
        <template #footer>
          <Button label="Pay later" text @click="depositPrompt = null" data-testid="deposit-pay-later" />
          <Button label="Pay deposit now" icon="pi pi-credit-card" severity="success" :loading="busy"
            data-testid="deposit-pay-now" @click="payDepositNow" />
        </template>
      </Dialog>

      <!-- Tap a photo → full size. The data URI is already downscaled server-
           side, so this is a zoom, not a second fetch. -->
      <Dialog v-model:visible="lightboxOpen" :header="lightboxPhoto?.label || 'Photo'" :modal="true"
        :dismissable-mask="true" :style="{ width: 'min(940px, 96vw)' }" data-testid="photo-lightbox">
        <img v-if="lightboxPhoto" :src="lightboxPhoto.src" class="lightbox-img"
          :alt="lightboxPhoto.label || 'Estimate photo'" />
      </Dialog>
    </main>
  </div>
</template>

<script setup>
import { computed, ref, onMounted } from "vue";
import { useRoute } from "vue-router";
import { useToast } from "primevue/usetoast";
import Button from "primevue/button";
import Card from "primevue/card";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Message from "primevue/message";
import ProgressSpinner from "primevue/progressspinner";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
import { formatDate, formatMoney } from "../composables/useFormatters";

const route = useRoute();
const toast = useToast();

const token = computed(() => String(route.params.token || ""));
const loading = ref(true);
const notFound = ref(false);
const data = ref(null);
const busy = ref(false);
const selectedTierId = ref(null);
const confirmOpen = ref(false);
const declineOpen = ref(false);
const declineReason = ref("");
const depositPrompt = ref(null);
const depositPromptOpen = computed({
  get: () => !!depositPrompt.value,
  set: (v) => { if (!v) depositPrompt.value = null; },
});
const lightboxPhoto = ref(null);
const lightboxOpen = computed({
  get: () => !!lightboxPhoto.value,
  set: (v) => { if (!v) lightboxPhoto.value = null; },
});

const est = computed(() => data.value?.estimate || {});
const photos = computed(() => data.value?.photos || []);
const tiers = computed(() => data.value?.tiers || []);
const lines = computed(() => data.value?.lines || []);
const totals = computed(() => data.value?.totals || null);
const deposit = computed(() => data.value?.deposit || null);
// The ASK: amount we'd like, no invoice behind it yet (2026-08-18).
const depositAsk = computed(() => data.value?.deposit_ask || null);
const company = computed(() => data.value?.company || { name: "", phone: "" });
// Backend masks bounced-internal "rejected" as "sent", so "sent" is the one
// open state this page can see.
const isOpen = computed(() => est.value.status === "sent");
const needsTierPick = computed(
  () => isOpen.value && est.value.proposal_mode && tiers.value.length > 0 && !selectedTierId.value,
);
// The backend omits `totals` on a line estimate only when the totals engine
// failed. Never let a customer bind a contract with no stated amount.
const totalsMissing = computed(
  () => isOpen.value && !est.value.proposal_mode && !totals.value,
);
const selectedTier = computed(
  () => tiers.value.find((t) => t.id === selectedTierId.value) || null,
);
const confirmAmount = computed(() => {
  if (selectedTier.value) return selectedTier.value.total_price;
  if (totals.value) return totals.value.total;
  return null;
});
const statusLabel = computed(() => (isOpen.value ? "awaiting your reply" : est.value.status));
const statusSeverity = computed(() => {
  const map = { sent: "info", accepted: "success", declined: "danger", expired: "secondary" };
  return map[est.value.status] || "secondary";
});

function currency(v) { return formatMoney(Number(v) || 0); }
function tierLabel(name) {
  const map = { good: "Good", better: "Better", best: "Best" };
  return map[name] || name;
}
function openPayUrl(url) { window.location.assign(url); }

// Mint-at-pay-click (2026-08-18): the deposit invoice is created only when
// the customer actually moves to pay online. Idempotent server-side, so a
// double click or a re-visit lands on the same invoice.
async function startDepositPay() {
  busy.value = true;
  try {
    const res = await fetch(`/api/proposals/${encodeURIComponent(token.value)}/deposit/pay`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    const payload = await res.json().catch(() => null);
    if (!res.ok) {
      const msg = typeof payload?.detail === "string"
        ? payload.detail
        : "Something went wrong — please try again.";
      toast.add({ severity: "warn", summary: msg, life: 5000 });
      await load();
      return;
    }
    if (payload?.deposit?.pay_url) {
      openPayUrl(payload.deposit.pay_url);
    } else {
      // Stripe/base-URL not configured: the invoice now exists — reload so
      // the page shows its number for a manual payment.
      await load();
    }
  } catch {
    toast.add({ severity: "error", summary: "Network error — please try again.", life: 5000 });
  } finally {
    busy.value = false;
  }
}

async function payDepositNow() {
  const p = depositPrompt.value;
  depositPrompt.value = null;
  if (!p) return;
  if (p.pay_url) { openPayUrl(p.pay_url); return; }
  await startDepositPay();
}

async function load() {
  loading.value = true;
  notFound.value = false;
  try {
    const res = await fetch(`/api/proposals/${encodeURIComponent(token.value)}`);
    if (!res.ok) { notFound.value = true; return; }
    data.value = await res.json();
  } catch {
    notFound.value = true;
  } finally {
    loading.value = false;
  }
}

async function post(action, body) {
  busy.value = true;
  try {
    const res = await fetch(`/api/proposals/${encodeURIComponent(token.value)}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const payload = await res.json().catch(() => null);
    if (!res.ok) {
      // 409 carries a customer-readable reason ("no longer open…"); surface
      // it and re-load so the page reflects the real state. Pydantic 422s
      // carry a LIST in detail — never toast that shape at a customer.
      const msg = typeof payload?.detail === "string"
        ? payload.detail
        : "Something went wrong — please try again.";
      toast.add({ severity: "warn", summary: msg, life: 5000 });
      await load();
      return null;
    }
    data.value = payload;
    return payload;
  } catch {
    toast.add({ severity: "error", summary: "Network error — please try again.", life: 5000 });
    return null;
  } finally {
    busy.value = false;
  }
}

async function accept() {
  confirmOpen.value = false;
  const body = selectedTier.value ? { tier_id: selectedTier.value.id } : {};
  const resp = await post("accept", body);
  if (!resp) return;
  toast.add({ severity: "success", summary: "Estimate accepted — thank you!", life: 4000 });
  if (resp.deposit?.pay_url) depositPrompt.value = resp.deposit;
  else if (resp.deposit_ask) depositPrompt.value = { ...resp.deposit_ask };
}

async function decline() {
  declineOpen.value = false;
  const reason = declineReason.value.trim();
  const resp = await post("decline", reason ? { reason } : {});
  if (!resp) return;
  toast.add({ severity: "info", summary: "Estimate declined.", life: 4000 });
}

onMounted(load);
</script>

<style scoped>
/* PrimeVue v4 --p-* tokens flip with data-theme (portal convention). */
.proposal-wrapper { min-height: 100vh; background: color-mix(in srgb, var(--p-content-background, #f3f4f6) 96%, var(--p-text-color, #000)); color: var(--p-text-color, #1e293b); }
.proposal-header { background: var(--p-content-background, #fff); padding: 1rem 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); display: flex; justify-content: center; border-bottom: 1px solid var(--p-content-border-color, transparent); }
.logo-container { display: flex; align-items: center; gap: 0.75rem; }
.company-name { font-size: 1.25rem; font-weight: 700; color: var(--p-text-color, #1e293b); }
.proposal-content { max-width: 720px; margin: 0 auto; padding: 1rem; }
.loading-wrap { display: flex; justify-content: center; padding: 3rem; }
.state-card { max-width: 520px; margin: 2rem auto; }
.card-title-row { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; flex-wrap: wrap; }
.description { white-space: pre-wrap; margin: 0.75rem 0; }
.photo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 0.5rem; margin: 0.75rem 0; }
.photo-tile { position: relative; padding: 0; border: 1px solid var(--p-content-border-color, #e5e7eb); border-radius: 8px; overflow: hidden; background: var(--p-content-background, #fff); cursor: zoom-in; aspect-ratio: 4 / 3; }
.photo-tile img { width: 100%; height: 100%; object-fit: cover; display: block; }
/* Label sits ON the image, so its colors are fixed against the photo, not the
   theme — white on dark scrim reads in light and dark mode alike. */
.photo-label { position: absolute; left: 0.4rem; bottom: 0.4rem; max-width: calc(100% - 0.8rem); padding: 0.15rem 0.5rem; border-radius: 4px; background: rgba(0, 0, 0, 0.65); color: #fff; font-size: 0.8rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.lightbox-img { display: block; width: 100%; height: auto; border-radius: 6px; }
.meta { font-size: 0.85rem; color: var(--p-text-muted-color, #6b7280); }
.valid-until { margin-top: 0.75rem; }
.lines-table { margin: 0.75rem 0; }
.totals-block { margin-left: auto; min-width: 240px; display: flex; flex-direction: column; gap: 0.35rem; margin-top: 0.5rem; }
.totals-row { display: flex; justify-content: space-between; font-size: 0.95rem; }
.totals-row.grand { font-weight: 700; font-size: 1.1rem; border-top: 1px solid var(--p-content-border-color, #e5e7eb); padding-top: 0.35rem; color: var(--p-primary-color); }
.tier-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 0.75rem; margin: 0.75rem 0; }
.tier-card { display: flex; flex-direction: column; gap: 0.4rem; align-items: flex-start; text-align: left; padding: 1rem; border-radius: 8px; border: 2px solid var(--p-content-border-color, #e5e7eb); background: var(--p-content-background, #fff); color: inherit; cursor: pointer; font: inherit; }
.tier-card.selected { border-color: var(--p-primary-color); box-shadow: 0 0 0 1px var(--p-primary-color); }
.tier-card.accepted { border-color: var(--p-primary-color); }
.tier-card:disabled { cursor: default; opacity: 0.85; }
.tier-name { font-weight: 700; }
.tier-price { font-size: 1.25rem; font-weight: 700; color: var(--p-primary-color); }
.tier-included { margin: 0.25rem 0 0; padding-left: 1.1rem; }
.tier-included li { margin-bottom: 0.15rem; }
.action-row { display: flex; gap: 0.5rem; margin-top: 1rem; }
.flex-1 { flex: 1; }
.deposit-row { margin-top: 0.75rem; }
/* Desktop: the bar is an ordinary block — the buttons are already on the
   first screen, so nothing sticks and the total row stays hidden (the
   totals-block above it already states the amount). */
.action-bar-total { display: none; }
.pay-by-check { margin-top: 0.5rem; }
.w-full { width: 100%; }
@media (max-width: 640px) {
  .tier-grid { grid-template-columns: 1fr; }
  .totals-block { min-width: 100%; }
  .action-row { flex-direction: column; }
}

/* 767px, not the 640px above, on purpose. `assets/responsive.css` is loaded
   globally from main.js and treats <=767px as "phone" (touch targets,
   fullscreen dialogs); useViewMode() draws the same line at 768px in JS. The
   640px block above predates that and is local to this file. Anything that
   decides whether a customer can COMPLETE the estimate has to switch on the
   same devices the rest of the app already calls a phone — otherwise a
   641-767px viewport (landscape phone, small tablet) gets fullscreen dialogs
   and 48px touch targets but no sticky decision bar, and a landscape phone is
   exactly where the short viewport makes the bar matter most.
   There is no shared breakpoint token; 39 component files hand-roll their own.
   Filed as follow-up, not fixed here. */
@media (max-width: 767px) {
  /* Line prices on a phone. With Price and Total columns shown, the table's
     natural width (~353px) exceeds the card (~318px). PrimeVue's container
     does scroll horizontally, so the page never scrolls sideways — but the
     Total column is what lands under the cut, and a customer reading a quote
     sees a clipped "$500." and concludes the page is broken, not scrollable.
     Trimming the cell padding reclaims more than the overflow, so all four
     columns fit with nothing hidden. */
  .lines-table :deep(.p-datatable-thead > tr > th),
  .lines-table :deep(.p-datatable-tbody > tr > td) {
    padding-left: 0.35rem;
    padding-right: 0.35rem;
    font-size: 0.9rem;
  }
  .lines-table :deep(.p-datatable-thead > tr > th:first-child),
  .lines-table :deep(.p-datatable-tbody > tr > td:first-child) { padding-left: 0; }
  .lines-table :deep(.p-datatable-thead > tr > th:last-child),
  .lines-table :deep(.p-datatable-tbody > tr > td:last-child) { padding-right: 0; }

  /* The decision bar rides the bottom of the viewport until the page is
     scrolled far enough for it to sit in its natural place. `sticky` (not
     `fixed`) on purpose: it stays in normal flow, so it cannot cover the
     content below it, needs no spacer, and settles inline at the end of the
     document. z-index stays below PrimeVue's dialog mask (1100+) so the
     accept/decline dialogs still cover it. */
  .action-bar {
    position: sticky;
    bottom: 0;
    z-index: 5;
    margin: 1rem -1.25rem 0;
    padding: 0.6rem 1.25rem;
    /* iOS home-indicator gesture zone — without this the buttons sit under it. */
    padding-bottom: calc(0.6rem + env(safe-area-inset-bottom, 0px));
    background: var(--p-content-background, #fff);
    border-top: 1px solid var(--p-content-border-color, #e5e7eb);
    box-shadow: 0 -6px 16px rgba(0, 0, 0, 0.09);
  }
  /* The buttons stay STACKED here (the 640px block sets column direction and
     this deliberately does not undo it). Side by side would return ~70px of a
     664px viewport, but "Accept estimate" + "Decline" measure ~306px against
     288px of card at 360px-wide phones (iPhone SE), so they would wrap or
     clip on exactly the small screens this bar exists to serve. A full-width
     primary action is also the larger tap target. Cost is a ~150px bar; the
     quote still has ~510px to render into, and both controls are on the
     first screen, which is the point. */
  .action-bar .action-row { margin-top: 0; }

  .action-bar-total {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 0.5rem;
  }
  .action-bar-amount { font-weight: 700; font-size: 1.05rem; color: var(--p-primary-color); }
}
</style>
