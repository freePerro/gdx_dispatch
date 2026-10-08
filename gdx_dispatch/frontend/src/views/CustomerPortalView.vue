<template>
  <div class="portal-wrapper" data-testid="customer-portal-root">
    <header class="portal-header">
      <div class="logo-container">
        <i class="pi pi-building" style="font-size: 1.5rem" />
        <span class="company-name">{{ company.name }}</span>
      </div>
      <div v-if="jwt && !error" class="header-actions">
        <Button icon="pi pi-key" label="Password" aria-label="Password" text size="small" @click="openSetPassword" data-testid="set-password-btn" />
        <Button icon="pi pi-sign-out" label="Sign out" aria-label="Sign out" text size="small" @click="signOut" data-testid="sign-out-btn" />
      </div>
    </header>

    <main class="portal-content">
      <div v-if="loading" class="loading-wrap"><ProgressSpinner /></div>

      <div v-else-if="!jwt" class="login-wrap" data-testid="portal-login">
        <Card class="login-card" data-testid="portal-login-card">
          <template #title>Sign in to your portal</template>
          <template #content>
            <Message v-if="error" severity="warn" class="mb-3" data-testid="login-notice">{{ error }}</Message>
            <div class="login-form">
              <label class="field">
                <span>Email</span>
                <InputText v-model="email" type="email" placeholder="you@example.com" data-testid="login-email" @keyup.enter="passwordLogin" />
              </label>
              <label class="field">
                <span>Password</span>
                <Password v-model="password" :feedback="false" toggleMask inputClass="w-full" placeholder="Your password" data-testid="login-password" @keyup.enter="passwordLogin" />
              </label>
              <label class="remember-row">
                <Checkbox v-model="remember" :binary="true" data-testid="login-remember" />
                <span>Keep me signed in on this device</span>
              </label>
              <Message v-if="loginError" severity="error" class="mb-2" data-testid="login-error">{{ loginError }}</Message>
              <Button label="Sign in" icon="pi pi-sign-in" :loading="signingIn" @click="passwordLogin" data-testid="login-submit" />
            </div>
            <Divider />
            <div class="magic-fallback">
              <p class="meta">Forgot your password, or don't have one yet?</p>
              <Button label="Email me a sign-in link" icon="pi pi-envelope" text :loading="requestSending" @click="requestNewLink" data-testid="request-link-btn" />
              <p v-if="requestSent" class="meta sent-note" data-testid="request-link-sent">If that email is on file, a sign-in link is on its way.</p>
            </div>
          </template>
        </Card>
      </div>

      <div v-else>
        <Message v-if="error" severity="warn" class="mb-3" data-testid="portal-error">{{ error }}</Message>
        <Message v-if="showSetPwPrompt" severity="info" :closable="true" class="mb-3" data-testid="set-pw-prompt" @close="showSetPwPrompt = false">
          Want faster sign-in next time? <a href="#" @click.prevent="openSetPassword">Set a password</a>.
        </Message>
        <Tabs value="estimates" data-testid="portal-tabs">
          <TabList>
            <Tab value="estimates">Estimates</Tab>
            <Tab value="invoices">Invoices</Tab>
            <Tab value="jobs">Jobs</Tab>
            <Tab value="contact">Contact</Tab>
          </TabList>
          <TabPanels>
          <TabPanel value="estimates">
            <div v-if="!estimates.length" class="empty-msg">No estimates available.</div>
            <div v-else class="card-grid">
              <Card v-for="est in estimates" :key="est.id" class="portal-card clickable" data-testid="estimate-card" @click="openEstimate(est.id)">
                <template #title>
                  <div class="card-title-row">
                    <span>{{ est.label || est.estimate_number }}</span>
                    <Tag :value="est.status" :severity="statusSeverity(est.status)" />
                  </div>
                </template>
                <template #content>
                  <p class="amount">{{ currency(est.total) }}</p>
                  <p v-if="est.description" class="meta">{{ est.description }}</p>
                  <p class="meta">Sent: {{ formatDate(est.sent_at || est.created_at) }}</p>
                  <p v-if="est.valid_until" class="meta">Valid until: {{ formatDate(est.valid_until) }}</p>
                  <p class="meta view-hint"><i class="pi pi-eye" /> View details</p>
                </template>
                <template #footer>
                  <div v-if="est.status === 'sent'" class="action-row">
                    <Button label="Accept" icon="pi pi-check" severity="success" class="flex-1" :loading="actionBusy[est.id]" @click.stop="acceptEstimate(est.id)" data-testid="accept-btn" />
                    <Button label="Decline" icon="pi pi-times" severity="danger" outlined class="flex-1" :loading="actionBusy[est.id]" @click.stop="declineEstimate(est.id)" data-testid="decline-btn" />
                  </div>
                  <div v-else-if="est.deposit?.pay_url" class="action-row">
                    <Button :label="`Pay ${currency(est.deposit.balance_due)} deposit`" icon="pi pi-credit-card" severity="success" class="flex-1" @click.stop="openPayUrl(est.deposit.pay_url)" data-testid="pay-deposit-btn" />
                  </div>
                  <!-- Deposit ASK (2026-08-18): no invoice exists yet — the
                       click mints it and opens the payment page. -->
                  <div v-else-if="est.deposit_ask" class="action-row">
                    <Button :label="`Pay ${currency(est.deposit_ask.amount)} deposit`" icon="pi pi-credit-card" severity="success" class="flex-1" :loading="actionBusy[est.id]" @click.stop="startDepositPay(est.id)" data-testid="pay-deposit-ask-btn" />
                  </div>
                </template>
              </Card>
            </div>
          </TabPanel>

          <TabPanel value="invoices">
            <!-- Cards, not a table (2026-10-07): the Pay button sat in a
                 trailing DataTable column, off-screen on a phone, and a row
                 led nowhere. A card opens the invoice and carries the Pay
                 button where a thumb can reach it. -->
            <div v-if="!invoices.length" class="empty-msg" data-testid="invoices-empty">No invoices found.</div>
            <div v-else class="card-grid" data-testid="invoices-list">
              <Card
                v-for="inv in invoices"
                :key="inv.id"
                class="portal-card clickable"
                data-testid="invoice-card"
                role="button"
                tabindex="0"
                @click="openInvoice(inv.id)"
                @keydown.enter.self="openInvoice(inv.id)"
              >
                <template #title>
                  <div class="card-title-row">
                    <span>
                      {{ inv.invoice_number }}
                      <Tag v-if="inv.billing_type === 'deposit'" value="Deposit" severity="info" data-testid="portal-deposit-tag" />
                    </span>
                    <Tag :value="invoiceStatus(inv)" :severity="statusSeverity(invoiceStatus(inv))" />
                  </div>
                </template>
                <template #content>
                  <p class="amount">{{ currency(inv.balance_due > 0 ? inv.balance_due : inv.total) }}</p>
                  <p class="meta">{{ inv.balance_due > 0 ? `Balance due of ${currency(inv.total)}` : 'Paid in full' }}</p>
                  <p v-if="inv.due_date && inv.balance_due > 0" class="meta">Due: {{ formatDate(inv.due_date) }}</p>
                  <p class="meta view-hint"><i class="pi pi-eye" /> View details</p>
                </template>
                <template v-if="inv.pay_url" #footer>
                  <div class="action-row">
                    <Button :label="`Pay ${currency(inv.balance_due)}`" icon="pi pi-credit-card" severity="success" class="flex-1" data-testid="invoice-pay-btn" @click.stop="openPayUrl(inv.pay_url)" />
                  </div>
                </template>
              </Card>
            </div>
          </TabPanel>

          <TabPanel value="jobs">
            <DataTable :value="jobs" responsiveLayout="stack" breakpoint="640px" data-testid="jobs-table">
              <template #empty>No jobs found.</template>
              <!-- Photos of the customer's own door (2026-08-12). The tech
                   shoots before/after on every job; until now the customer
                   never saw them.
                   The button lives INSIDE the first column, not in a trailing
                   one: this table does not stack on narrow screens (PrimeVue 4
                   dropped responsiveLayout="stack"), so a rightmost column is
                   off-screen on a phone — which is the device most customers
                   read this on. Caught on a real Pixel, 2026-08-12. -->
              <Column field="title" header="Job">
                <template #body="{ data }">
                  <div class="job-cell">
                    <span>{{ data.title }}</span>
                    <Button
                      v-if="data.photo_count"
                      :label="`${data.photo_count} photo${data.photo_count === 1 ? '' : 's'}`"
                      icon="pi pi-images"
                      size="small"
                      text
                      class="job-photos-btn"
                      :data-testid="`job-photos-${data.id}`"
                      @click="openJobPhotos(data)"
                    />
                  </div>
                </template>
              </Column>
              <Column field="lifecycle_stage" header="Status"><template #body="{ data }"><Tag :value="jobStatusLabel(data)" :severity="statusSeverity(data.lifecycle_stage)" /></template></Column>
              <Column field="scheduled_at" header="Scheduled"><template #body="{ data }">{{ formatDate(data.scheduled_at) }}</template></Column>
              <Column field="completed_at" header="Completed"><template #body="{ data }">{{ formatDate(data.completed_at) }}</template></Column>
            </DataTable>
          </TabPanel>

          <TabPanel value="contact">
            <Card data-testid="contact-card">
              <template #title>Contact Us</template>
              <template #content>
                <div class="contact-list">
                  <div v-if="company.phone"><i class="pi pi-phone" /> {{ company.phone }}</div>
                  <div v-if="company.email"><i class="pi pi-envelope" /> {{ company.email }}</div>
                  <div v-if="company.address"><i class="pi pi-map-marker" /> {{ company.address }}</div>
                  <div v-if="!company.phone && !company.email && !company.address" class="empty-msg">
                    Contact details are not available yet.
                  </div>
                </div>
              </template>
            </Card>
          </TabPanel>
          </TabPanels>
        </Tabs>

        <Dialog
          v-model:visible="detailVisible"
          :header="detail ? (detail.label || detail.estimate_number) : 'Estimate'"
          :modal="true"
          :style="{ width: 'min(640px, 94vw)' }"
          data-testid="estimate-detail-dialog"
        >
          <div v-if="detailLoading" class="loading-wrap"><ProgressSpinner /></div>
          <div v-else-if="detail" class="detail-body">
            <div class="detail-status-row">
              <Tag :value="detail.status" :severity="statusSeverity(detail.status)" />
              <span class="meta">Sent: {{ formatDate(detail.sent_at || detail.created_at) }}</span>
              <span v-if="detail.valid_until" class="meta">Valid until: {{ formatDate(detail.valid_until) }}</span>
            </div>
            <p v-if="detail.description" class="meta">{{ detail.description }}</p>
            <p v-if="detail.jobsite_address" class="meta"><i class="pi pi-map-marker" /> {{ detail.jobsite_address }}</p>

            <div v-if="detailImages.length" class="detail-images" data-testid="estimate-images">
              <Image v-for="img in detailImages" :key="img.id" :src="img.src" :alt="img.name" preview
                     image-style="height: 120px; border-radius: 6px; object-fit: cover" />
            </div>

            <!-- Category follows the estimate PDF's template setting
                 (detail.line_category) — the same rule as the public approval
                 page, so every copy of the estimate matches the PDF. -->
            <DataTable
              :value="detailRows"
              class="detail-lines"
              data-testid="estimate-lines-table"
              :row-group-mode="detailCatMode === 'grouped' ? 'subheader' : undefined"
              :group-rows-by="detailCatMode === 'grouped' ? '_category' : undefined"
            >
              <template #empty>No line items.</template>
              <template v-if="detailCatMode === 'grouped'" #groupheader="{ data: row }">
                <span v-if="row._category" class="line-cat-heading" data-testid="line-category-heading">{{ row._category }}</span>
                <span v-else class="line-cat-heading-empty" />
              </template>
              <!-- Never drawn; PrimeVue counts it when spanning the heading row. -->
              <Column v-if="detailCatMode === 'grouped'" field="_category" />
              <Column v-if="detailCatMode === 'column'" field="category" header="Category" header-class="line-cat-col" body-class="line-cat-col" />
              <Column header="Item">
                <template #body="{ data }">
                  <span v-if="detailCatMode === 'column' && data.category" class="line-cat-inline">{{ data.category }}</span>{{ data.description }}
                </template>
              </Column>
              <Column field="quantity" header="Qty" :style="{ width: '70px' }" />
              <Column v-if="!detail.hide_line_prices" field="unit_price" header="Price" header-class="line-price-col" body-class="line-price-col" :style="{ width: '110px' }"><template #body="{ data }">{{ currency(data.unit_price) }}</template></Column>
              <Column v-if="!detail.hide_line_prices" field="line_total" header="Total" :style="{ width: '110px' }"><template #body="{ data }">{{ currency(data.line_total) }}</template></Column>
            </DataTable>

            <div class="totals-block" v-if="detail.totals" data-testid="estimate-totals">
              <div class="totals-row"><span>Subtotal</span><span>{{ currency(detail.totals.subtotal) }}</span></div>
              <div class="totals-row" v-if="detail.totals.discount"><span>Discount</span><span>-{{ currency(detail.totals.discount) }}</span></div>
              <div class="totals-row" v-if="detail.totals.tax"><span>Tax ({{ detail.totals.tax_rate_pct }}%)</span><span>{{ currency(detail.totals.tax) }}</span></div>
              <div class="totals-row grand"><span>Total</span><span>{{ currency(detail.totals.total) }}</span></div>
              <p v-if="detail.totals.tax_unavailable" class="meta" data-testid="tax-unavailable-note">
                <i class="pi pi-info-circle" /> Tax could not be calculated — the final total may differ.
              </p>
            </div>

            <div v-if="detail.status === 'sent'" class="action-row detail-actions">
              <Button label="Accept" icon="pi pi-check" severity="success" class="flex-1" :loading="actionBusy[detail.id]" @click="acceptFromDetail" data-testid="detail-accept-btn" />
              <Button label="Decline" icon="pi pi-times" severity="danger" outlined class="flex-1" :loading="actionBusy[detail.id]" @click="declineFromDetail" data-testid="detail-decline-btn" />
            </div>
            <div v-else-if="detail.deposit?.pay_url" class="action-row detail-actions">
              <Button :label="`Pay ${currency(detail.deposit.balance_due)} deposit`" icon="pi pi-credit-card" severity="success" class="flex-1" @click="openPayUrl(detail.deposit.pay_url)" data-testid="detail-pay-deposit-btn" />
            </div>
            <div v-else-if="detail.deposit_ask" class="action-row detail-actions">
              <Button :label="`Pay ${currency(detail.deposit_ask.amount)} deposit`" icon="pi pi-credit-card" severity="success" class="flex-1" :loading="actionBusy[detail.id]" @click="startDepositPayFromDetail" data-testid="detail-pay-deposit-ask-btn" />
            </div>
            <p v-else-if="detail.status === 'declined' && detail.declined_reason" class="meta">Declined: {{ detail.declined_reason }}</p>
            <div class="action-row detail-actions">
              <Button label="Download PDF" icon="pi pi-download" severity="secondary" outlined class="flex-1" :loading="pdfBusy" data-testid="estimate-pdf-btn" @click="downloadPdf('estimates', detail.id, `estimate-${detail.estimate_number}`)" />
            </div>
          </div>
        </Dialog>

        <!-- Invoice detail (2026-10-07). Same numbers as the invoice PDF:
             lines, totals, paid to date, credits, balance due. Paying opens
             the public pay page — the portal has no card mint of its own. -->
        <Dialog
          v-model:visible="invoiceVisible"
          :header="invoiceDetail ? `Invoice ${invoiceDetail.invoice_number}` : 'Invoice'"
          :modal="true"
          :style="{ width: 'min(640px, 94vw)' }"
          data-testid="invoice-detail-dialog"
        >
          <div v-if="invoiceLoading" class="loading-wrap"><ProgressSpinner /></div>
          <div v-else-if="invoiceDetail" class="detail-body">
            <div class="detail-status-row">
              <Tag :value="invoiceStatus(invoiceDetail)" :severity="statusSeverity(invoiceStatus(invoiceDetail))" />
              <Tag v-if="invoiceDetail.billing_type === 'deposit'" value="Deposit" severity="info" />
              <span v-if="invoiceDetail.invoice_date" class="meta">Date: {{ formatDate(invoiceDetail.invoice_date) }}</span>
              <span v-if="invoiceDetail.due_date" class="meta">Due: {{ formatDate(invoiceDetail.due_date) }}</span>
            </div>

            <!-- Category follows the invoice PDF's template setting. -->
            <DataTable
              :value="invoiceRows"
              class="detail-lines"
              data-testid="invoice-lines-table"
              :row-group-mode="invoiceCatMode === 'grouped' ? 'subheader' : undefined"
              :group-rows-by="invoiceCatMode === 'grouped' ? '_category' : undefined"
            >
              <template #empty>No line items.</template>
              <template v-if="invoiceCatMode === 'grouped'" #groupheader="{ data: row }">
                <span v-if="row._category" class="line-cat-heading">{{ row._category }}</span>
                <span v-else class="line-cat-heading-empty" />
              </template>
              <Column v-if="invoiceCatMode === 'grouped'" field="_category" />
              <Column v-if="invoiceCatMode === 'column'" field="category" header="Category" header-class="line-cat-col" body-class="line-cat-col" />
              <Column header="Item">
                <template #body="{ data }">
                  <span v-if="invoiceCatMode === 'column' && data.category" class="line-cat-inline">{{ data.category }}</span>{{ data.description }}
                </template>
              </Column>
              <Column field="quantity" header="Qty" :style="{ width: '60px' }" />
              <Column v-if="!invoiceDetail.hide_line_prices" field="unit_price" header="Price" header-class="line-price-col" body-class="line-price-col" :style="{ width: '100px' }"><template #body="{ data }">{{ currency(data.unit_price) }}</template></Column>
              <Column v-if="!invoiceDetail.hide_line_prices" field="line_total" header="Total" :style="{ width: '100px' }"><template #body="{ data }">{{ currency(data.line_total) }}</template></Column>
            </DataTable>

            <div class="totals-block" data-testid="invoice-totals">
              <div v-if="invoiceDetail.totals.subtotal !== undefined" class="totals-row"><span>Subtotal</span><span>{{ currency(invoiceDetail.totals.subtotal) }}</span></div>
              <div v-if="invoiceDetail.totals.tax" class="totals-row"><span>Tax</span><span>{{ currency(invoiceDetail.totals.tax) }}</span></div>
              <div class="totals-row"><span>Total</span><span>{{ currency(invoiceDetail.totals.total) }}</span></div>
              <div v-if="invoiceDetail.totals.paid_to_date" class="totals-row"><span>Paid to date</span><span>-{{ currency(invoiceDetail.totals.paid_to_date) }}</span></div>
              <div v-if="invoiceDetail.totals.credits_applied" class="totals-row"><span>Credits applied</span><span>-{{ currency(invoiceDetail.totals.credits_applied) }}</span></div>
              <div class="totals-row grand" data-testid="invoice-balance-due"><span>Balance due</span><span>{{ currency(invoiceDetail.totals.balance_due) }}</span></div>
            </div>

            <p v-if="invoiceDetail.notes" class="meta invoice-notes">{{ invoiceDetail.notes }}</p>

            <div v-if="invoiceDetail.pay_url" class="action-row detail-actions">
              <Button :label="`Pay ${currency(invoiceDetail.balance_due)}`" icon="pi pi-credit-card" severity="success" class="flex-1" data-testid="invoice-detail-pay-btn" @click="openPayUrl(invoiceDetail.pay_url)" />
            </div>
            <!-- Owed but no online payment set up: say how to pay instead of
                 leaving the customer with a balance and no next step. -->
            <p v-else-if="invoiceDetail.balance_due > 0" class="meta" data-testid="invoice-pay-offline">
              <i class="pi pi-info-circle" /> Online payment isn't available for this invoice.
              <template v-if="company.phone || company.email">
                To pay, contact us{{ company.phone ? ` at ${company.phone}` : '' }}{{ company.phone && company.email ? ' or' : '' }}{{ company.email ? ` ${company.email}` : '' }}.
              </template>
            </p>
            <div class="action-row detail-actions">
              <Button label="Download PDF" icon="pi pi-download" severity="secondary" outlined class="flex-1" :loading="pdfBusy" data-testid="invoice-pdf-btn" @click="downloadPdf('invoices', invoiceDetail.id, `invoice-${invoiceDetail.invoice_number}`)" />
            </div>
          </div>
        </Dialog>

        <!-- Job photos (2026-08-12). Same blob-loading shape as the estimate
             images: an <img src> can't carry the portal Bearer token, so each
             photo is fetched authenticated and handed to the dialog as an
             object URL. -->
        <Dialog
          v-model:visible="jobPhotosVisible"
          :header="jobPhotosTitle"
          :modal="true"
          :style="{ width: 'min(720px, 94vw)' }"
          data-testid="job-photos-dialog"
        >
          <div v-if="jobPhotosLoading" class="loading-wrap"><ProgressSpinner /></div>
          <div v-else-if="jobPhotoImages.length" class="detail-images" data-testid="job-photo-images">
            <figure v-for="img in jobPhotoImages" :key="img.id" class="job-photo-figure">
              <Image :src="img.src" :alt="img.label" preview
                     image-style="height: 140px; border-radius: 6px; object-fit: cover" />
              <figcaption v-if="img.label" class="meta">{{ img.label }}</figcaption>
            </figure>
          </div>
          <p v-else class="meta" data-testid="job-photos-empty">
            No photos are available for this job.
          </p>
        </Dialog>

        <Dialog
          v-model:visible="depositPromptOpen"
          header="Deposit Due"
          :modal="true"
          :style="{ width: 'min(440px, 94vw)' }"
          data-testid="deposit-pay-dialog"
        >
          <p class="meta">
            Thanks for accepting! A deposit of <b>{{ currency(depositPrompt?.amount) }}</b> is due now
            to get your job on the schedule<template v-if="depositPrompt?.invoice_number">
            (invoice {{ depositPrompt.invoice_number }})</template>.
          </p>
          <p class="meta">You can pay securely online by card — or pay later; the button stays on the estimate.</p>
          <!-- Card was the only payment method this dialog acknowledged, which
               read as "card or nothing". A customer cannot record their own
               cash, and must not be able to — so this states the check option
               honestly and records NOTHING. The money becomes real when the
               office has it in hand. -->
          <details class="pay-by-check" data-testid="deposit-pay-by-check">
            <summary>Paying by check?</summary>
            <p class="meta">
              Make it out to <b>{{ company.name }}</b> and write
              <template v-if="depositPrompt?.invoice_number">invoice
                <b>{{ depositPrompt.invoice_number }}</b></template>
              <template v-else>estimate <b>#{{ depositPrompt?.estimate_number }}</b></template>
              on the memo line.
              <template v-if="company.address"> Mail to {{ company.address }}.</template>
              <template v-else> Use the remit-to address on your invoice.</template>
            </p>
            <p class="meta">
              We'll record it when it arrives — no need to do anything else here.
            </p>
          </details>
          <template #footer>
            <Button label="Pay later" text @click="depositPrompt = null" data-testid="deposit-pay-later" />
            <Button label="Pay deposit now" icon="pi pi-credit-card" severity="success"
              data-testid="deposit-pay-now" @click="payDepositNow" />
          </template>
        </Dialog>

        <Dialog
          v-model:visible="setPwVisible"
          header="Set a password"
          :modal="true"
          :style="{ width: 'min(420px, 94vw)' }"
          data-testid="set-password-dialog"
        >
          <p class="meta">Set a password so you can sign in without waiting for an email link next time.</p>
          <label class="field">
            <span>New password</span>
            <Password v-model="newPassword" toggleMask inputClass="w-full" :feedback="true" data-testid="new-password" @keyup.enter="setPassword" />
          </label>
          <p class="meta">At least 8 characters.</p>
          <template #footer>
            <Button label="Cancel" text @click="setPwVisible = false" />
            <Button label="Save password" icon="pi pi-check" :loading="settingPw" :disabled="newPassword.length < 8" @click="setPassword" data-testid="save-password-btn" />
          </template>
        </Dialog>
      </div>
    </main>
  </div>
</template>

<script setup>
import { computed, reactive, ref, onMounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useToast } from "primevue/usetoast";
import Button from "primevue/button";
import Card from "primevue/card";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Image from "primevue/image";
import InputText from "primevue/inputtext";
import Checkbox from "primevue/checkbox";
import Divider from "primevue/divider";
import Message from "primevue/message";
import Password from "primevue/password";
import ProgressSpinner from "primevue/progressspinner";
import Tab from "primevue/tab";
import TabList from "primevue/tablist";
import TabPanel from "primevue/tabpanel";
import TabPanels from "primevue/tabpanels";
import Tabs from "primevue/tabs";
import Tag from "primevue/tag";
import { formatDate, formatMoney } from "../composables/useFormatters";
import { lineCategoryMode, rowsGroupedByCategory } from "../utils/lineCategories";

const JWT_STORAGE_KEY = "gdx_portal_jwt";

const route = useRoute();
const router = useRouter();
const toast = useToast();
const jwt = ref("");
const loading = ref(true);
const error = ref(null);
const company = ref({ name: "Customer Portal", phone: "", email: "", address: "" });
const estimates = ref([]);
const invoices = ref([]);
const jobs = ref([]);
const actionBusy = reactive({});
const detail = ref(null);
const detailVisible = ref(false);
const detailLoading = ref(false);
const detailImages = ref([]);
// 'off' | 'column' | 'grouped' — the estimate PDF's line-items setting.
const detailCatMode = computed(() => lineCategoryMode(detail.value?.line_category));
const detailRows = computed(() => {
  const rows = detail.value?.lines || [];
  return detailCatMode.value === "grouped" ? rowsGroupedByCategory(rows) : rows;
});
const invoiceDetail = ref(null);
const invoiceVisible = ref(false);
const invoiceLoading = ref(false);
// 'off' | 'column' | 'grouped' — the invoice PDF's line-items setting.
const invoiceCatMode = computed(() => lineCategoryMode(invoiceDetail.value?.line_category));
const invoiceRows = computed(() => {
  const rows = invoiceDetail.value?.lines || [];
  return invoiceCatMode.value === "grouped" ? rowsGroupedByCategory(rows) : rows;
});
const email = ref("");
const password = ref("");
const remember = ref(false);
const signingIn = ref(false);
const loginError = ref("");
const requestSending = ref(false);
const requestSent = ref(false);
const setPwVisible = ref(false);
const newPassword = ref("");
const settingPw = ref(false);
const showSetPwPrompt = ref(false);

// The sign-in endpoints share a strict per-IP limit. A 429 is not a wrong
// password and not a sent link, so it gets its own words — telling a
// customer with the right password that it is wrong is what this replaced.
const TOO_MANY_ATTEMPTS = "Too many sign-in attempts. Wait a minute, then try again.";

function currency(v) { return formatMoney(Number(v) || 0); }
function statusSeverity(s) {
  const map = { sent: "info", accepted: "success", paid: "success", declined: "danger", unpaid: "warn", overdue: "danger", expired: "secondary", scheduled: "info", in_progress: "info", completed: "success" };
  return map[(s || "").toLowerCase()] || "secondary";
}
// An overdue invoice says so; otherwise paid / unpaid.
function invoiceStatus(inv) {
  return inv.status === "overdue" && inv.balance_due > 0 ? "overdue" : inv.payment_status;
}
function jobStatusLabel(job) {
  return (job.lifecycle_stage || "").replace(/_/g, " ") || "-";
}

// "Remember me" persists the session in localStorage (survives a browser
// restart); otherwise it lives in sessionStorage and ends with the tab.
function storeJwt(token, rememberMe) {
  if (!token) { clearStoredJwt(); return; }
  jwt.value = token;
  if (rememberMe) {
    localStorage.setItem(JWT_STORAGE_KEY, token);
    sessionStorage.removeItem(JWT_STORAGE_KEY);
  } else {
    sessionStorage.setItem(JWT_STORAGE_KEY, token);
    localStorage.removeItem(JWT_STORAGE_KEY);
  }
}
function readStoredJwt() {
  return localStorage.getItem(JWT_STORAGE_KEY) || sessionStorage.getItem(JWT_STORAGE_KEY) || "";
}
function clearStoredJwt() {
  jwt.value = "";
  localStorage.removeItem(JWT_STORAGE_KEY);
  sessionStorage.removeItem(JWT_STORAGE_KEY);
}

async function authedFetch(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { ...(options.headers || {}), Authorization: `Bearer ${jwt.value}` },
  });
  if (res.status === 401) {
    clearStoredJwt();
    throw Object.assign(new Error("unauthorized"), { auth: true });
  }
  if (!res.ok) {
    // Surface the server's detail string — "already accepted" is a far
    // better message than "request failed: 409".
    let detail = `request failed: ${res.status}`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string" && body.detail) detail = body.detail;
    } catch { /* non-JSON error body */ }
    throw Object.assign(new Error(detail), { status: res.status });
  }
  return res.json();
}

async function fetchAll() {
  const [ctx, est, inv, job] = await Promise.all([
    authedFetch("/portal/context"),
    authedFetch("/portal/estimates"),
    authedFetch("/portal/invoices"),
    authedFetch("/portal/jobs"),
  ]);
  if (ctx?.company) company.value = ctx.company;
  estimates.value = Array.isArray(est) ? est : [];
  invoices.value = Array.isArray(inv) ? inv : [];
  jobs.value = Array.isArray(job) ? job : [];
}

async function init() {
  loading.value = true;
  const magicToken = route.query.token;
  if (magicToken) {
    // Exchange the one-time emailed token for a session JWT, then drop it
    // from the URL so a refresh doesn't retry the already-consumed token.
    try {
      const res = await fetch(`/portal/verify?token=${encodeURIComponent(magicToken)}`);
      if (res.ok) {
        const body = await res.json();
        storeJwt(body.access_token || "", false); // magic-link → per-session, not "remember"
        showSetPwPrompt.value = true; // nudge them to set a password for next time
      } else if (res.status === 401) {
        error.value = "This sign-in link is invalid or has expired.";
      } else if (res.status === 429) {
        // The link was not consumed; it still works once the minute passes.
        error.value = "Too many sign-in attempts. Wait a minute, then open your link again.";
      } else {
        error.value = "Could not sign you in. Please open your link again.";
      }
    } catch {
      // Network failure: the link was never checked, so it is not "invalid".
      error.value = "Could not sign you in. Please open your link again.";
    }
    router.replace({ query: {} });
  }
  if (!jwt.value) jwt.value = readStoredJwt();
  if (!jwt.value) { loading.value = false; return; } // not signed in → login card
  await loadPortal();
}

async function loadPortal() {
  try {
    await fetchAll();
    error.value = null;
  } catch (e) {
    if (e?.auth) { clearStoredJwt(); error.value = "Your portal session has expired."; }
    else error.value = "Failed to load portal data. Please try again later.";
  } finally {
    loading.value = false;
  }
}

async function requestNewLink() {
  const em = email.value.trim();
  if (!em) { loginError.value = "Enter your email first."; return; }
  requestSending.value = true;
  requestSent.value = false;
  loginError.value = "";
  try {
    const res = await fetch("/portal/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: em }),
    });
    if (res.status === 429) { loginError.value = TOO_MANY_ATTEMPTS; return; }
    if (!res.ok) { loginError.value = "Could not send a sign-in link. Please try again."; return; }
    requestSent.value = true;
  } catch {
    toast.add({ severity: "error", summary: "Error", detail: "Could not send link. Try again.", life: 4000 });
  } finally {
    requestSending.value = false;
  }
}

async function passwordLogin() {
  const em = email.value.trim();
  if (!em || !password.value) { loginError.value = "Enter your email and password."; return; }
  signingIn.value = true;
  loginError.value = "";
  try {
    const res = await fetch("/portal/login/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: em, password: password.value, remember: remember.value }),
    });
    if (res.status === 429) { loginError.value = TOO_MANY_ATTEMPTS; return; }
    if (res.status === 401) { loginError.value = "Invalid email or password."; return; }
    if (!res.ok) { loginError.value = "Could not sign in. Please try again."; return; }
    const body = await res.json();
    storeJwt(body.access_token || "", remember.value);
    password.value = "";
    error.value = null;
    loading.value = true;
    await loadPortal();
  } catch {
    loginError.value = "Could not sign in. Please try again.";
  } finally {
    signingIn.value = false;
  }
}

function openSetPassword() {
  newPassword.value = "";
  setPwVisible.value = true;
}

async function setPassword() {
  if (newPassword.value.length < 8) return;
  settingPw.value = true;
  try {
    await authedFetch("/portal/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ new_password: newPassword.value }),
    });
    setPwVisible.value = false;
    showSetPwPrompt.value = false;
    newPassword.value = "";
    toast.add({ severity: "success", summary: "Password saved", detail: "You can now sign in with your email and password.", life: 4000 });
  } catch (e) {
    if (e?.auth) { error.value = "Your portal session has expired."; return; }
    toast.add({ severity: "error", summary: "Error", detail: "Could not save password.", life: 4000 });
  } finally {
    settingPw.value = false;
  }
}

function signOut() {
  clearStoredJwt();
  error.value = null;
  showSetPwPrompt.value = false;
  estimates.value = [];
  invoices.value = [];
  jobs.value = [];
}

// Deposit prompt (2026-08-18 rework): the accept response carries either a
// live deposit invoice (legacy rows — invoice_number + pay_url) or a
// `deposit_ask` (amount only, NO invoice yet). Accepting flows straight
// into the payment prompt; "Pay later" is always available — acceptance is
// never blocked by payment, and the Pay button re-surfaces on the estimate
// card and the detail dialog. The invoice is minted only when the customer
// actually clicks to pay online (idempotent server-side); check/cash is
// recorded by the office when the money arrives.
const depositPrompt = ref(null);
const depositPromptOpen = computed({
  get: () => !!depositPrompt.value,
  set: (v) => { if (!v) depositPrompt.value = null; },
});

function openPayUrl(url) {
  if (url) window.open(url, "_blank", "noopener");
}

// The PDF needs the portal's bearer token, so a plain link can't fetch it:
// load it as a blob and hand the browser a download of that.
const pdfBusy = ref(false);
async function downloadPdf(kind, id, name) {
  pdfBusy.value = true;
  try {
    const res = await fetch(`/portal/${kind}/${id}/pdf`, { headers: { Authorization: `Bearer ${jwt.value}` } });
    if (res.status === 401) {
      clearStoredJwt();
      error.value = "Your portal session has expired.";
      return;
    }
    if (!res.ok) throw new Error(`pdf ${res.status}`);
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = `${name}.pdf`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch {
    toast.add({ severity: "error", summary: "Download failed", detail: "Could not load the PDF. Try again.", life: 4000 });
  } finally {
    pdfBusy.value = false;
  }
}

async function startDepositPay(estimateId) {
  actionBusy[estimateId] = true;
  // Open the tab SYNCHRONOUSLY, inside the click gesture — window.open
  // after an await is outside the user-gesture stack and Safari (often
  // Chrome too) silently blocks it: the invoice would mint and the payment
  // page would never appear. Point the pre-opened tab at the pay URL once
  // the mint returns; close it on any failure.
  // (no "noopener" feature here — that makes window.open return null; the
  // opener link is severed manually below instead.)
  const payTab = typeof window.open === "function" ? window.open("", "_blank") : null;
  if (payTab) payTab.opener = null;
  try {
    const resp = await authedFetch(`/portal/estimates/${estimateId}/deposit/pay`, { method: "POST" });
    const payUrl = resp?.deposit?.pay_url;
    if (payUrl && payTab) payTab.location = payUrl;
    else if (payTab) payTab.close();
    // The freshly minted invoice replaces the ask on the card and lands on
    // the Invoices tab.
    estimates.value = await authedFetch("/portal/estimates");
    try { invoices.value = await authedFetch("/portal/invoices"); } catch { /* tab refresh is best-effort */ }
  } catch (e) {
    if (payTab) payTab.close();
    if (e?.auth) { error.value = "Your portal session has expired."; return; }
    toast.add({ severity: "error", summary: "Error", detail: "Could not start the deposit payment", life: 4000 });
  } finally {
    actionBusy[estimateId] = false;
  }
}

async function startDepositPayFromDetail() {
  if (!detail.value) return;
  const id = detail.value.id;
  await startDepositPay(id);
  try { detail.value = await authedFetch(`/portal/estimates/${id}`); } catch { /* dialog keeps stale state */ }
}

async function payDepositNow() {
  const p = depositPrompt.value;
  depositPrompt.value = null;
  if (!p) return;
  if (p.pay_url) { openPayUrl(p.pay_url); return; }
  if (p.estimate_id) await startDepositPay(p.estimate_id);
}

async function estimateAction(id, action, successMsg) {
  actionBusy[id] = true;
  try {
    const resp = await authedFetch(`/portal/estimates/${id}/${action}`, { method: "POST" });
    toast.add({ severity: action === "accept" ? "success" : "warn", summary: successMsg, life: 3000 });
    if (action === "accept") {
      if (resp?.deposit?.pay_url) depositPrompt.value = resp.deposit;
      else if (resp?.deposit_ask) {
        depositPrompt.value = {
          ...resp.deposit_ask,
          estimate_id: id,
          estimate_number: resp.estimate_number,
        };
      }
    }
    estimates.value = await authedFetch("/portal/estimates");
    try { invoices.value = await authedFetch("/portal/invoices"); } catch { /* tab refresh is best-effort */ }
  } catch (e) {
    if (e?.auth) { error.value = "Your portal session has expired."; return; }
    const msg = e?.message && !e.message.startsWith("request failed")
      ? e.message
      : `Could not ${action} estimate`;
    toast.add({ severity: "error", summary: "Error", detail: msg, life: 4000 });
  } finally {
    actionBusy[id] = false;
  }
}

const acceptEstimate = (id) => estimateAction(id, "accept", "Estimate accepted");
const declineEstimate = (id) => estimateAction(id, "decline", "Estimate declined");

function clearDetailImages() {
  detailImages.value.forEach((img) => URL.revokeObjectURL(img.src));
  detailImages.value = [];
}

// ─── Job photos (2026-08-12) ─────────────────────────────────────────
// The tech photographs every job; the customer had never been shown a single
// one. These are pictures of their own door — "here is what we found, here is
// what we fixed" — and they load through the portal's own token-scoped route,
// never the staff document download.
const jobPhotosVisible = ref(false);
const jobPhotosLoading = ref(false);
const jobPhotosTitle = ref("Job photos");
const jobPhotoImages = ref([]);

function clearJobPhotoImages() {
  jobPhotoImages.value.forEach((img) => URL.revokeObjectURL(img.src));
  jobPhotoImages.value = [];
}

async function openJobPhotos(job) {
  jobPhotosVisible.value = true;
  jobPhotosLoading.value = true;
  jobPhotosTitle.value = job?.title ? `Photos — ${job.title}` : "Job photos";
  clearJobPhotoImages();
  try {
    const rows = await authedFetch(`/portal/jobs/${job.id}/photos`);
    const loaded = await Promise.all(
      (rows || []).map(async (p) => {
        try {
          const res = await fetch(p.url, { headers: { Authorization: `Bearer ${jwt.value}` } });
          if (!res.ok) return null;
          return {
            id: p.id,
            label: (p.caption || "").trim() || (p.kind || "").trim(),
            src: URL.createObjectURL(await res.blob()),
          };
        } catch { return null; }
      })
    );
    jobPhotoImages.value = loaded.filter(Boolean);
  } catch {
    jobPhotoImages.value = [];
  } finally {
    jobPhotosLoading.value = false;
  }
}

// Free the blobs when the dialog closes — same discipline as the estimate one.
watch(jobPhotosVisible, (open) => { if (!open) clearJobPhotoImages(); });

async function loadDetailImages(images) {
  // <img src> can't carry the Bearer header — pull each image as an
  // authenticated blob and hand the dialog object URLs instead.
  const loaded = await Promise.all(
    (images || []).map(async (img) => {
      try {
        const res = await fetch(img.url, { headers: { Authorization: `Bearer ${jwt.value}` } });
        if (!res.ok) return null;
        return { id: img.id, name: img.original_name, src: URL.createObjectURL(await res.blob()) };
      } catch { return null; }
    })
  );
  detailImages.value = loaded.filter(Boolean);
}

// Free the blob object URLs whenever the dialog closes, not just on reopen.
watch(detailVisible, (open) => { if (!open) clearDetailImages(); });

async function openEstimate(id) {
  detailVisible.value = true;
  detailLoading.value = true;
  clearDetailImages();
  try {
    detail.value = await authedFetch(`/portal/estimates/${id}`);
    // Not awaited: the dialog renders immediately and photos pop in as
    // their blobs arrive, instead of holding the spinner on big images.
    loadDetailImages(detail.value.images);
  } catch (e) {
    detailVisible.value = false;
    if (e?.auth) { error.value = "Your portal session has expired."; return; }
    toast.add({ severity: "error", summary: "Error", detail: "Could not load estimate", life: 4000 });
  } finally {
    detailLoading.value = false;
  }
}

async function openInvoice(id) {
  invoiceVisible.value = true;
  invoiceLoading.value = true;
  try {
    invoiceDetail.value = await authedFetch(`/portal/invoices/${id}`);
  } catch (e) {
    invoiceVisible.value = false;
    if (e?.auth) { error.value = "Your portal session has expired."; return; }
    toast.add({ severity: "error", summary: "Error", detail: "Could not load invoice", life: 4000 });
  } finally {
    invoiceLoading.value = false;
  }
}

async function actFromDetail(action, successMsg) {
  if (!detail.value) return;
  const id = detail.value.id;
  await estimateAction(id, action, successMsg);
  // Refresh the open dialog so the status/actions reflect the change.
  try { detail.value = await authedFetch(`/portal/estimates/${id}`); } catch { detailVisible.value = false; }
}

const acceptFromDetail = () => actFromDetail("accept", "Estimate accepted");
const declineFromDetail = () => actFromDetail("decline", "Estimate declined");

onMounted(init);
</script>

<style scoped>
/* PrimeVue v4 --p-* tokens flip with data-theme; the --surface-* names do not exist here. */
.portal-wrapper { min-height: 100vh; background: color-mix(in srgb, var(--p-content-background, #f3f4f6) 96%, var(--p-text-color, #000)); color: var(--p-text-color, #1e293b); }
/* Three columns: the name stays centred in the middle one and the actions get
   their own column, so a long name truncates instead of running under the
   buttons (it used to: the actions were absolutely positioned over it). */
.portal-header { background: var(--p-content-background, #fff); padding: 1rem 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); display: grid; grid-template-columns: 1fr minmax(0, auto) 1fr; align-items: center; gap: 0.5rem; border-bottom: 1px solid var(--p-content-border-color, transparent); }
.logo-container { grid-column: 2; display: flex; align-items: center; gap: 0.75rem; min-width: 0; }
.company-name { font-size: 1.25rem; font-weight: 700; color: var(--p-text-color, #1e293b); min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.portal-content { max-width: 900px; margin: 0 auto; padding: 1rem; }
.loading-wrap { display: flex; justify-content: center; padding: 3rem; }
.empty-msg { text-align: center; padding: 2rem; color: var(--p-text-muted-color, #6b7280); }
.card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; }
.portal-card { transition: transform 0.15s; }
.portal-card.clickable { cursor: pointer; }
.portal-card:hover { transform: translateY(-2px); }
.view-hint { display: flex; align-items: center; gap: 0.4rem; margin-top: 0.5rem; }
.detail-body { display: flex; flex-direction: column; gap: 0.75rem; }
.detail-images { display: flex; gap: 0.5rem; flex-wrap: wrap; }
/* Job-photo tiles carry a caption under the thumbnail (the tech's slot or
   note), so each one is a figure rather than a bare image. */
.job-photo-figure { margin: 0; display: flex; flex-direction: column; gap: 0.2rem; max-width: 200px; }
/* The photos link sits under the job name so it stays on screen on a phone,
   where this table scrolls sideways rather than stacking. */
.job-cell { display: flex; flex-direction: column; align-items: flex-start; gap: 0.15rem; }
.job-photos-btn { padding: 0.15rem 0; min-height: 32px; }
.job-photo-figure figcaption { text-transform: capitalize; }
.detail-status-row { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
.totals-block { margin-left: auto; min-width: 240px; display: flex; flex-direction: column; gap: 0.35rem; }
.totals-row { display: flex; justify-content: space-between; font-size: 0.95rem; }
.totals-row.grand { font-weight: 700; font-size: 1.1rem; border-top: 1px solid var(--p-content-border-color, #e5e7eb); padding-top: 0.35rem; color: var(--p-primary-color); }
.detail-actions { margin-top: 0.25rem; }
.invoice-notes { white-space: pre-line; }
.card-title-row { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; }
.amount { font-size: 1.5rem; font-weight: 700; color: var(--p-primary-color); margin: 0.5rem 0; }
.meta { font-size: 0.85rem; color: var(--p-text-muted-color, #6b7280); }
.action-row { display: flex; gap: 0.5rem; }
/* No utility CSS is loaded on this page, so the class the action buttons
   have always carried is defined here: they share the row's full width. */
.action-row > .flex-1 { flex: 1 1 0; }
.contact-list { display: flex; flex-direction: column; gap: 1rem; }
.contact-list div { display: flex; align-items: center; gap: 0.75rem; }
.contact-list i { color: var(--p-primary-color); }
.auth-error { max-width: 480px; margin: 2rem auto; }
.request-link-card { margin-top: 1rem; }
.request-link-row { display: flex; gap: 0.5rem; margin-top: 0.75rem; }
.sent-note { margin-top: 0.75rem; color: var(--p-primary-color); }
.header-actions { grid-column: 3; justify-self: end; display: flex; gap: 0.25rem; align-items: center; }
.login-wrap { max-width: 460px; margin: 2rem auto; }
.login-form { display: flex; flex-direction: column; gap: 0.9rem; }
.field { display: flex; flex-direction: column; gap: 0.35rem; }
.field > span { font-size: 0.85rem; font-weight: 600; color: var(--p-text-color, #374151); }
.field :deep(.p-inputtext) { width: 100%; }
.field :deep(.p-password), .field :deep(.p-password input) { width: 100%; }
.remember-row { display: flex; align-items: center; gap: 0.5rem; font-size: 0.9rem; cursor: pointer; }
.magic-fallback { display: flex; flex-direction: column; align-items: flex-start; gap: 0.4rem; }
@media (max-width: 640px) {
  .card-grid { grid-template-columns: 1fr; }
  .company-name { font-size: 1rem; }
  .portal-header { padding: 0.75rem 1rem; }
  /* Icon-only on a phone; the aria-label keeps the name for screen readers. */
  .header-actions :deep(.p-button-label) { display: none; }
}
.line-cat-heading { font-weight: 700; }
/* No heading for the uncategorized group — the PDF gives it none. */
.detail-lines :deep(.p-datatable-row-group-header:has(.line-cat-heading-empty)) { display: none; }
.line-cat-inline { display: none; }
/* 'column' mode on a phone: the category moves into the item cell. */
@media (max-width: 767px) {
  .detail-lines :deep(.line-cat-col) { display: none; }
  .line-cat-inline { display: block; font-size: 0.75rem; font-weight: 600; color: var(--p-text-muted-color, #6b7280); }
}
/* A phone has room for Item, Qty and Total; the unit price is the one to drop. */
@media (max-width: 640px) { .detail-lines :deep(.line-price-col) { display: none; } }
</style>
