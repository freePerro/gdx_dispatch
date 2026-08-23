<template>
    <section class="invoice-detail view-card">
      <div v-if="loading" class="loading-spinner"><p>Loading invoice...</p></div>
      <template v-else>
        <!-- Parts the tech recorded that this invoice does not bill
             (2026-08-19). The §8 policy decided in 2026-07 was: build from
             everything priced, leave the rest on the checklist, and MARK THE
             INVOICE. Only the mobile lane got the mark, so the office
             verified labor-only drafts with nothing saying two attested
             parts had been dropped. Advisory -- Edit is one click away, and
             plenty of parts legitimately go unbilled (warranty, goodwill,
             covered by a flat price). -->
        <div
          v-if="unbilledPartsError === 'forbidden'"
          class="unbilled-parts-banner"
          data-testid="unbilled-parts-forbidden"
        >
          <div>
            <strong>Can't check this job's recorded parts.</strong>
            <span class="unbilled-parts-list">
              Your role can't read inventory, so this invoice may be missing
              parts the tech recorded. Ask someone with inventory access
              before verifying.
            </span>
          </div>
        </div>
        <div
          v-else-if="unbilledJobParts.length"
          class="unbilled-parts-banner"
          data-testid="unbilled-parts-banner"
        >
          <div>
            <strong>
              {{ unbilledJobParts.length }}
              recorded part{{ unbilledJobParts.length === 1 ? '' : 's' }}
              from this job {{ unbilledJobParts.length === 1 ? 'is' : 'are' }}
              not on this invoice.
            </strong>
            <span class="unbilled-parts-list">
              {{ unbilledJobParts.map((p) => p.part_name).filter(Boolean).join(', ') }}
            </span>
          </div>
          <Button
            label="Edit to add them"
            size="small"
            outlined
            data-testid="unbilled-parts-edit"
            @click="enterEditMode"
          />
        </div>

        <!-- Header -->
        <header class="detail-header">
          <div>
            <Button
              icon="pi pi-arrow-left"
              label="Back to Billing"
              text
              size="small"
              @click="$router.push('/billing')"
              data-testid="back-to-billing"
            />
            <h2 data-testid="invoice-number">{{ invoice.invoice_number }}</h2>
            <p data-testid="invoice-customer" class="customer-name">
              <router-link v-if="invoice.customer_id" :to="`/customers/${invoice.customer_id}`" class="link">
                {{ invoice.customer_name }}
              </router-link>
              <span v-else>{{ invoice.customer_name }}</span>
              <router-link v-if="invoice.job_id" :to="`/jobs/${invoice.job_id}`" class="link" style="margin-left:1rem; font-size:0.85rem;">
                <i class="pi pi-briefcase" /> View Job
              </router-link>
              <router-link
                v-if="invoice.estimate_id || invoice.source_estimate_id"
                :to="`/estimates/${invoice.estimate_id || invoice.source_estimate_id}`"
                class="link"
                style="margin-left:1rem; font-size:0.85rem;"
                data-testid="invoice-view-estimate-link"
              >
                <i class="pi pi-file" /> View Estimate
              </router-link>
            </p>
          </div>
          <div class="header-meta">
            <Tag
              :value="invoice.status"
              :severity="statusSeverity(invoice.status)"
              data-testid="invoice-status"
            />
            <Tag
              v-if="invoice.billing_type === 'deposit'"
              value="deposit"
              severity="info"
              style="margin-left: 0.35rem"
              data-testid="invoice-deposit-tag"
            />
            <!-- Machine provenance (2026-08-08): the office must KNOW it is
                 reviewing machine-priced numbers, not a colleague's. -->
            <Tag
              v-if="invoice.origin === 'closeout_autodraft'"
              value="auto-drafted from closeout"
              severity="warn"
              style="margin-left: 0.35rem"
              v-tooltip.bottom="'Priced by the system from the tech\'s closeout (hours + parts). Review every line before verifying.'"
              data-testid="invoice-autodraft-tag"
            />
            <p>Due: <strong>{{ formatDate(invoice.due_date) }}</strong></p>
            <p>Created: {{ formatDate(invoice.created_at) }}</p>
            <!-- formatStampDateTime: QB-backfilled stamps are UTC midnight
                 ("day known, minute not") and render date-only on the
                 correct calendar day; real sends show time-of-day. -->
            <p v-if="invoice.sent_at" data-testid="invoice-last-sent">
              Last sent: {{ formatStampDateTime(invoice.sent_at) }}
              <!-- Paper invoices (migration 057): without the channel, a
                   mailed invoice's stamp reads like an email nobody can find. -->
              <span v-if="invoice.sent_via === 'mail'">(by mail)</span>
            </p>
            <p v-if="qbEnabled" data-testid="invoice-qb-sync">
              QuickBooks:
              <Tag
                :value="qbSync.label"
                :severity="qbSync.severity"
                data-testid="invoice-qb-sync-tag"
              />
            </p>
          </div>
        </header>

        <!-- Bill To panel — surfaces customer contact on the invoice so the
             office can email/call without bouncing through /customers/<id>.
             "Edit" opens the shared CustomerFormDialog. -->
        <section class="bill-to-card" data-testid="invoice-bill-to">
          <div class="bill-to-header">
            <h3>Bill To</h3>
            <Button
              v-if="invoice.customer_id"
              label="Edit Customer"
              icon="pi pi-pencil"
              size="small"
              text
              data-testid="invoice-edit-customer-btn"
              @click="openCustomerEdit"
            />
          </div>
          <div class="bill-to-grid">
            <div class="bill-to-row" data-testid="bill-to-name">
              <i class="pi pi-user" />
              <span v-if="invoice.customer_name">{{ invoice.customer_name }}</span>
              <span v-else class="muted">Unknown customer</span>
            </div>
            <div class="bill-to-row" data-testid="bill-to-email">
              <i class="pi pi-envelope" />
              <a v-if="invoice.customer_email" :href="`mailto:${invoice.customer_email}`">{{ invoice.customer_email }}</a>
              <a v-else-if="invoice.customer_id" href="#" class="muted add-link" data-testid="bill-to-add-email" @click.prevent="openCustomerEdit">+ Add email</a>
              <span v-else class="muted">—</span>
            </div>
            <div class="bill-to-row" data-testid="bill-to-phone">
              <i class="pi pi-phone" />
              <a v-if="invoice.customer_phone" :href="`tel:${invoice.customer_phone}`">{{ formatPhone(invoice.customer_phone) }}</a>
              <a v-else-if="invoice.customer_id" href="#" class="muted add-link" data-testid="bill-to-add-phone" @click.prevent="openCustomerEdit">+ Add phone</a>
              <span v-else class="muted">—</span>
            </div>
            <div class="bill-to-row" data-testid="bill-to-address">
              <i class="pi pi-map-marker" />
              <a v-if="invoice.customer_address" :href="`https://maps.google.com/?q=${encodeURIComponent(invoice.customer_address)}`" target="_blank" rel="noopener">{{ invoice.customer_address }}</a>
              <a v-else-if="invoice.customer_id" href="#" class="muted add-link" data-testid="bill-to-add-address" @click.prevent="openCustomerEdit">+ Add address</a>
              <span v-else class="muted">—</span>
            </div>
          </div>
        </section>

        <Divider />

        <!-- Line Items -->
        <div class="lines-header">
          <h3>Line Items</h3>
          <div v-if="canEdit" class="lines-header-actions">
            <Button
              v-if="!editing"
              label="Edit"
              icon="pi pi-pencil"
              size="small"
              outlined
              data-testid="invoice-edit-btn"
              @click="enterEditMode"
            />
            <template v-else>
              <!-- Add Line lives inside LineItemEditor — the previous
                   duplicate "Add Line" here was removed when this view
                   was switched to the shared editor (2026-05-12). -->
              <Button
                label="Cancel"
                size="small"
                severity="secondary"
                outlined
                data-testid="invoice-edit-cancel"
                @click="cancelEdit"
              />
              <Button
                label="Save Changes"
                icon="pi pi-check"
                size="small"
                :loading="savingEdit"
                data-testid="invoice-edit-save"
                @click="saveEdit"
              />
            </template>
          </div>
        </div>
        <!-- Read-only line table (default) -->
        <DataTable
      responsiveLayout="scroll"
          v-if="!editing"
          :value="invoice.line_items"
          dataKey="id"
          data-testid="invoice-line-items"
          class="mb-1"
        >
          <template #empty>No line items.</template>
          <!-- D-S122b-detail-view-columns: render category/cost/margin fields
               so detail page round-trips what /billing/new captures.
               Column order matches LineItemEditor (Category / Description /
               Qty / Cost / Unit Price / Taxable / Margin / Total). -->
          <Column field="category" header="Category" style="width: 110px">
            <template #body="{ data }">
              <span v-if="data.category">{{ data.category }}</span>
              <span v-else style="opacity: 0.4">—</span>
            </template>
          </Column>
          <Column field="description" header="Description" />
          <Column field="quantity" header="Qty" style="width: 80px" />
          <Column header="Cost" style="width: 100px">
            <template #body="{ data }">
              <span v-if="data.cost_snapshot != null">{{ currency(data.cost_snapshot) }}</span>
              <span v-else style="opacity: 0.4">—</span>
            </template>
          </Column>
          <Column field="unit_price" header="Unit Price" style="width: 120px">
            <template #body="{ data }">{{ currency(data.unit_price) }}</template>
          </Column>
          <Column header="Taxable" style="width: 90px; text-align: center">
            <template #body="{ data }">
              <i v-if="data.taxable" class="pi pi-check" style="color: var(--p-success-500)" />
              <span v-else style="opacity: 0.5">—</span>
            </template>
          </Column>
          <Column header="Margin" style="width: 90px">
            <template #body="{ data }">
              <span v-if="data.margin_pct_override != null">{{ formatPercent(data.margin_pct_override) }}</span>
              <span v-else-if="data.margin_pct_snapshot != null">{{ formatPercent(data.margin_pct_snapshot) }}<small class="muted"> tier</small></span>
              <span v-else style="opacity: 0.4">—</span>
            </template>
          </Column>
          <Column header="Total" style="width: 120px; text-align: right">
            <template #body="{ data }">{{ currency(lineTotal(data)) }}</template>
          </Column>
        </DataTable>
        <!-- Editable line items — shared LineItemEditor component (parity
             with /billing/new and /estimates/new). 2026-05-12: replaced the
             inline DataTable because it lacked the tier-aware recompute that
             EstimateView/InvoiceCreateView/LineItemEditor all share — typing
             a cost in edit mode never auto-filled unit_price. The shared
             editor also brings the Add-from-Catalog + parts-from-job panels
             into edit mode so a dispatcher can pull more parts onto a
             still-draft invoice without leaving the page. -->
        <LineItemEditor
          v-else
          v-model:lines="editLines"
          :categories="lineCategoryOptions"
          :job-id="invoice.job_id || null"
          :locked-predicate="isDepositNettingLine"
          locked-tooltip="Deposit netting line — it mirrors the deposit actually paid and can't be edited or deleted. Void the invoice and re-create it if the netting is wrong."
          :closeout="closeoutSuggestion"
          show-taxable
          show-cost
          show-margin
          show-labor
          data-testid="invoice-edit-line-items"
        />
        <!-- Editable tax rate + dates + notes when in edit mode -->
        <div v-if="editing" class="edit-meta-grid">
          <div class="edit-field">
            <label>Tax Rate (%)</label>
            <InputNumber
              v-model="editTaxRatePct"
              :min="0"
              :max="100"
              :minFractionDigits="2"
              :maxFractionDigits="4"
              suffix=" %"
              data-testid="invoice-edit-tax-rate"
            />
            <small class="hint">
              Tenant default: {{ formatPercent(tenantDefaultRatePct, { digits: 2, whole: true }) }}.
              Leave 0 if every line is non-taxable.
            </small>
          </div>
          <div class="edit-field">
            <label>Invoice Date</label>
            <InputText v-model="editInvoiceDate" type="date" />
          </div>
          <div class="edit-field">
            <label>Due Date</label>
            <InputText v-model="editDueDate" type="date" />
          </div>
          <div class="edit-field" style="grid-column: 1 / -1">
            <label>Notes</label>
            <InputText v-model="editNotes" class="w-full" />
          </div>
          <div class="edit-field" style="grid-column: 1 / -1; display:flex; align-items:center; gap:0.6rem;">
            <ToggleSwitch v-model="editHideLinePrices" inputId="inv-hide-line-prices" data-testid="invoice-hide-line-prices" />
            <label for="inv-hide-line-prices" style="margin:0">Hide line-item prices on PDF</label>
          </div>
        </div>

        <!-- Totals. In edit mode the breakdown is computed live from the
             editable lines + rate so the dispatcher can see what the next
             save will produce. Read mode uses the server-stored numbers. -->
        <div class="totals-section" data-testid="invoice-totals">
          <template v-if="editing">
            <div class="total-row">
              <span>Subtotal</span>
              <strong>{{ currency(editSubtotal) }}</strong>
            </div>
            <div class="total-row">
              <span>Taxable Subtotal</span>
              <strong>{{ currency(editTaxableSubtotal) }}</strong>
            </div>
            <div class="total-row">
              <span>Tax ({{ formatPercent(editTaxRatePct, { digits: 2, whole: true }) }})</span>
              <strong>{{ currency(editTax) }}</strong>
            </div>
            <div class="total-row grand">
              <span>Preview Total</span>
              <strong>{{ currency(editTotal) }}</strong>
            </div>
          </template>
          <template v-else>
            <template v-if="invoice.line_items && invoice.line_items.length">
              <div class="total-row">
                <span>Subtotal</span>
                <strong>{{ currency(invoice.subtotal || subtotal) }}</strong>
              </div>
              <div v-if="invoice.taxable_subtotal !== undefined && invoice.taxable_subtotal !== invoice.subtotal" class="total-row">
                <span>Taxable Subtotal</span>
                <strong>{{ currency(invoice.taxable_subtotal) }}</strong>
              </div>
              <div class="total-row">
                <span>Tax<template v-if="invoice.tax_rate != null"> ({{ formatPercent(invoice.tax_rate, { digits: 2 }) }})</template></span>
                <strong>{{ currency(invoice.tax_amount) }}</strong>
              </div>
            </template>
            <div class="total-row grand">
              <span>Total</span>
              <strong>{{ currency(invoice.total) }}</strong>
            </div>
            <!-- Paid + Balance Due reflect the SAVED invoice — hide them
                 in edit mode so they don't argue with Preview Total above
                 (which is the in-progress projection). 2026-05-12 audit. -->
            <div class="total-row paid" v-if="totalPaid > 0">
              <span>Paid</span>
              <strong>{{ currency(totalPaid) }}</strong>
            </div>
            <div class="total-row balance">
              <span>{{ balanceDue < 0 ? 'Overpaid by' : 'Balance Due' }}</span>
              <strong :class="{ 'overpaid': balanceDue < 0 }">{{ currency(Math.abs(balanceDue)) }}</strong>
            </div>
          </template>
        </div>

        <Divider />

        <!-- Action Buttons — hidden in edit mode (2026-05-12 audit). The
             Send / Record Payment / Push QB / Delete actions all operate
             on the SAVED invoice, so surfacing them mid-edit lets a user
             "Send" an invoice whose draft edits haven't been committed yet
             — confusing at best, error-prone at worst. -->
        <div v-if="!editing" class="actions" data-testid="invoice-actions">
          <!-- Re-send is allowed on sent/overdue (2026-07-20): the composer
               already gates on an explicit click, and the concrete need is
               re-sending an invoice whose first email went out without the
               PDF. Paid unlocked 2026-08-17 as "Send Receipt" — compose
               returns thank-you wording and the PDF carries the PAID badge.
               Only void stays locked. -->
          <Button
            :label="String(invoice.status || '').toLowerCase() === 'paid' ? 'Send Receipt'
              : (['sent','overdue'].includes(String(invoice.status || '').toLowerCase()) ? 'Re-send Invoice' : 'Send Invoice')"
            icon="pi pi-send"
            data-testid="send-invoice-btn"
            :disabled="String(invoice.status || '').toLowerCase() === 'void'"
            @click="sendInvoice"
          />
          <!-- Paper invoices: printed + posted, no email involved. Stamps the
               delivery fact with channel 'mail' so the row leaves the Billing
               "Unsent" tab honestly. Hidden once mailed (re-mailing the same
               invoice is rare enough that Send/Re-send covers the rest). -->
          <Button
            v-if="!['paid','void'].includes(String(invoice.status || '').toLowerCase()) && invoice.sent_via !== 'mail'"
            label="Mark as Mailed"
            icon="pi pi-envelope"
            severity="secondary"
            outlined
            :loading="markingMailed"
            data-testid="mark-mailed-btn"
            @click="markAsMailed"
          />
          <Button
            label="Record Payment"
            icon="pi pi-dollar"
            severity="success"
            data-testid="record-payment-btn"
            :disabled="String(invoice.status || '').toLowerCase() === 'paid'"
            @click="openPaymentDialog"
          />
          <Button
            label="Download PDF"
            icon="pi pi-file-pdf"
            severity="secondary"
            data-testid="download-pdf-btn"
            @click="downloadPdf"
          />
          <Button
            v-if="qbConnected"
            label="Push to QuickBooks"
            icon="pi pi-cloud-upload"
            severity="info"
            outlined
            :loading="pushingToQb"
            data-testid="push-qb-btn"
            @click="pushToQuickbooks"
          />
          <!-- PR6-billing-capture: per-invoice dunning mute for payment
               arrangements — manual reminder logs never pause the robot. -->
          <Button
            v-if="['sent','overdue'].includes(String(invoice.status || '').toLowerCase())"
            :label="invoice.dunning_paused ? 'Resume reminders' : 'Pause reminders'"
            :icon="invoice.dunning_paused ? 'pi pi-play' : 'pi pi-pause'"
            severity="warn"
            outlined
            data-testid="dunning-pause-btn"
            @click="toggleDunningPause"
          />
          <!-- Tier-2 UI doors (contract-gap sweep 2026-07-24): the credit
               lifecycle was fully built server-side with zero entry points —
               the office could SEE a credit memo but never issue one. -->
          <!-- Plan §11: the office's explicit approval. Until verified the
               tech's mobile Send is refused (409) — on the hourly lane the
               closeout hours ARE the price, typed from memory, so a second
               pair of eyes stands between the truck and the customer's
               inbox. (Audit round 2: the first version of this change added
               the handler and NO button — every tech invoice would have
               409'd forever with no way to unblock it.) -->
          <Button
            v-if="!invoice.verified_at"
            label="Verify invoice"
            icon="pi pi-check-square"
            severity="success"
            data-testid="verify-invoice-btn"
            :loading="verifying"
            @click="verifyInvoice"
          />
          <Tag
            v-else
            value="Verified"
            severity="success"
            data-testid="invoice-verified-tag"
            v-tooltip="'Office-verified — the tech can send it from the field'"
          />
          <Button
            v-if="['sent','overdue'].includes(String(invoice.status || '').toLowerCase()) && balanceDue > 0"
            label="Credit Memo"
            icon="pi pi-percentage"
            severity="warn"
            outlined
            data-testid="credit-memo-btn"
            @click="showCreditMemoDialog = true"
          />
          <Button
            v-if="glPostingEnabled && ['sent','overdue'].includes(String(invoice.status || '').toLowerCase()) && balanceDue > 0"
            label="Apply Credit"
            icon="pi pi-wallet"
            severity="info"
            outlined
            data-testid="apply-credit-btn"
            @click="showApplyCreditDialog = true"
          />
          <Button
            v-if="!invoice.locked && String(invoice.status || '').toLowerCase() !== 'void'"
            label="Finalize"
            icon="pi pi-lock"
            severity="secondary"
            outlined
            :loading="finalizing"
            data-testid="finalize-invoice-btn"
            @click="finalizeInvoice"
          />
          <Tag
            v-if="invoice.locked"
            value="Locked"
            severity="contrast"
            icon="pi pi-lock"
            data-testid="invoice-locked-tag"
          />
          <!-- Void. The endpoint has existed, complete and audited, since GL
               S5 and had ZERO callers until 2026-08-23 — no .vue file called
               it, and prod had never written a single `invoice_voided` audit
               row. The office simply could not void an invoice.

               Not the same verb as Delete: delete is soft and draft-only,
               void is TERMINAL (there is no un-void endpoint anywhere) and
               keeps the invoice on the record with its number intact, which
               is what a compliance trail needs. So it gets a typed
               confirmation rather than a one-click confirm — same pattern
               DatabaseAdminView uses for a migration.

               Hidden on an already-void invoice. NOT hidden on a paid one:
               the button stays, and the dialog explains that the payments
               have to go first. Hiding it there would be a different dead end
               — the operator wanting to void a mispaid invoice would find no
               control and no reason. (An earlier draft of this comment
               claimed a paid-invoice guard that the v-if did not have;
               adversarial review caught it.)

               `canWriteInvoices` is UX only. On THIS tenant it does not
               exclude technicians: their TenantRole snapshot has drifted to
               include invoices.read_all + invoices.write, and the resolver
               trusts a non-admin snapshot verbatim. Filed, not papered over. -->
          <Button
            v-if="canWriteInvoices && String(invoice.status || '').toLowerCase() !== 'void'"
            label="Void"
            icon="pi pi-ban" aria-label="Void invoice"
            severity="danger"
            outlined
            data-testid="void-invoice-btn"
            @click="openVoidDialog"
          />
          <Tag
            v-if="String(invoice.status || '').toLowerCase() === 'void'"
            value="Void"
            severity="danger"
            icon="pi pi-ban"
            data-testid="invoice-void-tag"
          />
          <Button
            label="Delete"
            icon="pi pi-trash" aria-label="Delete"
            severity="danger"
            outlined
            data-testid="delete-invoice-btn"
            @click="confirmDelete"
          />
        </div>

        <Divider v-if="!editing" />

        <!-- Superseded-deposit banner — without it a partially-paid deposit
             closed out at final-create reads "paid" while its payments don't
             sum to the total, and the operator only learns the truth from
             the record-payment 409. -->
        <div
          v-if="invoice.is_superseded_deposit"
          class="superseded-banner"
          data-testid="superseded-deposit-banner"
        >
          <i class="pi pi-info-circle" />
          This deposit was closed out when the final invoice was created —
          the unpaid remainder was credit-memo'd (see Adjustments below).
          Record any further payment on the final invoice, not here.
        </div>

        <!-- Overpayment banner (M11 money-audit detector, surfaced
             2026-08-08): balance_due floors at 0, so money collected ABOVE
             the total was invisible on every screen — the usual cause is a
             duplicate payment. -->
        <div
          v-if="invoice.amount_overpaid > 0"
          class="superseded-banner overpaid-banner"
          data-testid="overpaid-banner"
        >
          <i class="pi pi-exclamation-triangle" />
          Collected {{ currency(invoice.amount_overpaid) }} <strong>above the invoice total</strong> —
          usually a duplicate payment. Review Payment History below; refund or
          credit the excess.
        </div>

        <!-- Payment History -->
        <h3>Payment History</h3>
        <DataTable
      responsiveLayout="scroll" :value="invoice.payments" dataKey="id" data-testid="payment-history-table">
          <template #empty>No payments recorded.</template>
          <Column field="date" header="Date">
            <template #body="{ data }">{{ formatDate(data.date) }}</template>
          </Column>
          <Column field="method" header="Method" />
          <Column field="reference" header="Reference" />
          <Column field="amount" header="Amount" style="text-align: right">
            <template #body="{ data }">{{ currency(data.amount) }}</template>
          </Column>
        </DataTable>

        <!-- Adjustments (credit memos / refunds / applied credits) — explain
             why balance_due ≠ total − payments. Hidden when there are none. -->
        <template v-if="invoice.adjustments && invoice.adjustments.length">
          <h3>Adjustments</h3>
          <DataTable
            responsiveLayout="scroll"
            :value="invoice.adjustments"
            dataKey="id"
            data-testid="adjustments-table"
          >
            <Column field="created_at" header="Date">
              <template #body="{ data }">{{ formatDate(data.created_at) }}</template>
            </Column>
            <Column field="kind" header="Type">
              <template #body="{ data }">{{ adjustmentKindLabel(data.kind) }}</template>
            </Column>
            <Column field="reason" header="Reason" />
            <Column field="amount" header="Amount" style="text-align: right">
              <template #body="{ data }">{{ currency(data.amount) }}</template>
            </Column>
          </DataTable>
        </template>

        <!-- Notes -->
        <div v-if="invoice.notes" class="notes-section">
          <h3>Notes</h3>
          <p data-testid="invoice-notes">{{ invoice.notes }}</p>
        </div>

        <!-- Job photos on the invoice PDF (2026-08-07): before/after shots
             justify the bill. Checked photos print as a "Job Photos" grid
             on the PDF, so they ride every delivery channel (email, print,
             postal). Editable while draft; read-only after. -->
        <!-- Rendered whenever the invoice has a job (2026-08-12), not only
             when photos happen to load. It used to vanish on `!jobPhotos.length`
             — and jobPhotos is [] both when the job has no photos AND when the
             read failed or was denied, so "there is no way to do this" was the
             only reading available to the user. Say which it is. -->
        <div v-if="invoice.job_id" class="notes-section" data-testid="invoice-job-photos">
          <h3>Job photos on invoice</h3>
          <p class="photo-pick-hint">
            Checked photos print on the invoice PDF.
            <template v-if="invoice.status !== 'draft'"> (locked — invoice is no longer a draft)</template>
          </p>
          <p v-if="jobPhotosError" class="photo-pick-hint" data-testid="invoice-photos-error">
            {{ jobPhotosError }}
          </p>
          <p v-else-if="!jobPhotos.length" class="photo-pick-hint" data-testid="invoice-photos-empty">
            This job has no photos yet.
          </p>
          <div v-else class="photo-pick-grid">
            <label
              v-for="p in jobPhotos"
              :key="p.id"
              class="photo-pick"
              :class="{ selected: isPhotoAttached(p.id), locked: invoice.status !== 'draft' }"
              :data-testid="`invoice-photo-${p.id}`"
            >
              <input
                type="checkbox"
                :checked="isPhotoAttached(p.id)"
                :disabled="invoice.status !== 'draft' || photoSaving"
                @change="togglePhoto(p.id)"
              />
              <AuthedImage :src="p.url" :alt="p.caption || p.kind" class="photo-pick-thumb" />
              <span class="photo-pick-meta">
                {{ p.kind }}<template v-if="p.caption"> — {{ p.caption }}</template>
              </span>
            </label>
          </div>
        </div>
      </template>

      <!-- Email composer (Outlook-backed; mailto fallback when not connected) -->
      <Dialog v-model:visible="showComposer" header="Email invoice" modal
        :style="{ width: '720px' }" data-testid="invoice-composer">
        <div v-if="composerLoading" class="composer-loading">Building email…</div>
        <div v-else class="composer-form">
          <div class="form-field">
            <label>To</label>
            <Select v-if="composer.recipients.length" v-model="composer.contact_id"
              :options="composer.recipients" option-value="contact_id" option-label="email"
              class="w-full" data-testid="composer-recipient"
              @change="onRecipientChange">
              <template #option="{ option }">
                <span>{{ option.name }} &lt;{{ option.email }}&gt;</span>
                <Tag v-if="option.is_primary" value="primary" severity="info" class="ml-2" />
                <small class="muted ml-2">{{ option.label }}</small>
              </template>
              <template #value="{ value }">
                <span>{{ recipientDisplay(value) }}</span>
              </template>
            </Select>
            <InputText v-else v-model="composer.to" placeholder="customer@example.com"
              class="w-full" data-testid="composer-to" />
            <small v-if="canMakeDefaultRecipient" class="muted">
              <a href="#" data-testid="composer-make-default"
                @click.prevent="makeDefaultRecipient">Always send to this person</a>
            </small>
          </div>
          <div class="form-field">
            <label>Subject</label>
            <InputText v-model="composer.subject" class="w-full" data-testid="composer-subject" />
          </div>
          <div class="form-field">
            <label>Message</label>
            <Textarea v-model="composer.body_text" rows="8" class="w-full" data-testid="composer-body" />
            <small class="muted">Your message — line items, totals and the pay button are added
              automatically around it. Use Preview to see the final email.</small>
          </div>
          <div class="form-field">
            <label>Attachments</label>
            <div class="composer-attachments">
              <label class="composer-att-row">
                <input type="checkbox" :checked="true" disabled />
                <i class="pi pi-file-pdf" />
                <span>{{ composer.pdf?.name }}</span>
                <small class="muted">{{ formatBytes(composer.pdf?.size_bytes) }} · auto-attached</small>
              </label>
              <label v-for="att in composer.extras" :key="att.id" class="composer-att-row">
                <input type="checkbox" v-model="att._include" />
                <i :class="att.content_type?.startsWith('image/') ? 'pi pi-image' : 'pi pi-file'" />
                <span>{{ att.name }}</span>
                <small class="muted">{{ formatBytes(att.file_size) }}</small>
              </label>
            </div>
            <ComposerPdfPreview :pdf="composer.pdf" />
          </div>
          <div v-if="composer.previewHtml" class="form-field">
            <label>Email preview</label>
            <iframe class="composer-preview" sandbox="" :srcdoc="composer.previewHtml"
              data-testid="composer-preview" title="Email preview" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" text @click="showComposer = false" data-testid="composer-cancel" />
          <Button label="Preview" icon="pi pi-eye" severity="secondary" outlined
            :loading="composerPreviewing" :disabled="composerLoading"
            data-testid="composer-preview-btn" @click="previewComposer" />
          <Button label="Send" icon="pi pi-send" severity="primary"
            :loading="composerSending" :disabled="composerLoading || !composerHasRecipient"
            data-testid="composer-send" @click="sendComposer" />
        </template>
      </Dialog>

      <!-- Record Payment Dialog -->
      <Dialog
        v-model:visible="showPaymentDialog"
        header="Record Payment"
        modal
        :style="{ width: '480px' }"
        data-testid="record-payment-dialog"
      >
        <div class="form-grid-single">
          <div class="form-field">
            <label for="pay-amount">Amount *</label>
            <InputNumber
              id="pay-amount"
              v-model="newPayment.amount"
              mode="currency"
              currency="USD"
              locale="en-US"
              :min="0.01"
              :max="balanceDue > 0 ? balanceDue : undefined"
              data-testid="payment-amount"
            />
            <small v-if="balanceDue > 0" class="form-hint">Balance due: {{ currency(balanceDue) }}</small>
          </div>
          <div class="form-field">
            <label for="pay-method">Payment Method *</label>
            <Select
              id="pay-method"
              v-model="newPayment.method"
              :options="paymentMethods"
              data-testid="payment-method"
            />
          </div>
          <div class="form-field">
            <label for="pay-date">Payment Date *</label>
            <InputText
              id="pay-date"
              v-model="newPayment.date"
              type="date"
              :max="todayKey()"
              data-testid="payment-date"
            />
            <small class="form-hint">Bank deposit / receipt date — backdate for corrections</small>
          </div>
          <div class="form-field">
            <label for="pay-ref">Reference #</label>
            <InputText
              id="pay-ref"
              v-model="newPayment.reference"
              placeholder="Check #, confirmation..."
              data-testid="payment-reference"
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showPaymentDialog = false" />
          <Button
            label="Save Payment"
            data-testid="save-payment"
            :disabled="!newPayment.amount || !newPayment.method || !newPayment.date"
            :loading="savingPayment"
            @click="recordPayment"
          />
        </template>
      </Dialog>

      <!-- Credit Memo Dialog (Tier-2 UI door) -->
      <Dialog
        v-model:visible="showCreditMemoDialog"
        header="Issue Credit Memo"
        modal
        :style="{ width: '480px' }"
        data-testid="credit-memo-dialog"
      >
        <div class="form-grid-single">
          <div class="form-field">
            <label for="cm-amount">Amount to credit *</label>
            <InputNumber
              id="cm-amount"
              v-model="creditMemo.amount"
              mode="currency"
              currency="USD"
              locale="en-US"
              :min="0.01"
              :max="balanceDue > 0 ? balanceDue : undefined"
              data-testid="credit-memo-amount"
            />
            <small class="form-hint">Forgives part of the remaining balance ({{ currency(balanceDue) }}). This is permanent and audited.</small>
          </div>
          <div class="form-field">
            <label for="cm-reason">Reason *</label>
            <InputText
              id="cm-reason"
              v-model="creditMemo.reason"
              placeholder="e.g. goodwill adjustment, billing error"
              data-testid="credit-memo-reason"
            />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showCreditMemoDialog = false" />
          <Button
            label="Issue Credit"
            severity="warn"
            data-testid="save-credit-memo"
            :disabled="!creditMemo.amount || !creditMemo.reason.trim()"
            :loading="savingCreditMemo"
            @click="issueCreditMemo"
          />
        </template>
      </Dialog>

      <!-- Apply Customer Credit Dialog (Tier-2 UI door) -->
      <Dialog
        v-model:visible="showApplyCreditDialog"
        header="Apply Customer Credit"
        modal
        :style="{ width: '480px' }"
        data-testid="apply-credit-dialog"
      >
        <div class="form-grid-single">
          <div class="form-field">
            <label for="ac-amount">Amount to apply *</label>
            <InputNumber
              id="ac-amount"
              v-model="applyCredit.amount"
              mode="currency"
              currency="USD"
              locale="en-US"
              :min="0.01"
              :max="balanceDue > 0 ? balanceDue : undefined"
              data-testid="apply-credit-amount"
            />
            <small class="form-hint">
              Consumes this customer's credit balance against the invoice.
              Requires ledger posting — the server enforces both the credit
              balance and the remaining balance ({{ currency(balanceDue) }}).
            </small>
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showApplyCreditDialog = false" />
          <Button
            label="Apply Credit"
            data-testid="save-apply-credit"
            :disabled="!applyCredit.amount"
            :loading="savingApplyCredit"
            @click="applyCustomerCredit"
          />
        </template>
      </Dialog>

      <!-- Void Dialog. Typed confirmation, not a one-click confirm: a void
           cannot be undone and no un-void endpoint exists. The body says what
           actually happens, because "are you sure?" is not information. -->
      <Dialog
        v-model:visible="showVoidDialog"
        header="Void this invoice?"
        modal
        :style="{ width: '32rem', maxWidth: '95vw' }"
        data-testid="void-invoice-dialog"
      >
        <div class="void-dialog-body">
          <div
            v-if="voidBlockedReason"
            class="void-blocked"
            data-testid="void-blocked-reason"
          >
            {{ voidBlockedReason }}
          </div>
          <p class="void-lead">
            <strong>{{ invoice.invoice_number }}</strong> for
            <strong>{{ currency(invoice.total) }}</strong> will be voided.
            <strong>This is permanent</strong> — there is no way to un-void an
            invoice.
          </p>
          <ul class="void-consequences" data-testid="void-consequences">
            <li>The invoice stays on the record with its number, marked void.</li>
            <li>
              Any parts and change orders it claimed go back on the unbilled
              checklist, so they can be billed on a new invoice.
            </li>
            <li v-if="glPostingEnabled">
              Its ledger entry is reversed and the receivable cleared.
            </li>
            <li>To bill this work you will need to create a new invoice.</li>
          </ul>
          <label v-if="!voidBlockedReason" class="void-type-label" for="void-confirm-input">
            Type <code>{{ invoice.invoice_number }}</code> to confirm
          </label>
          <InputText
            v-if="!voidBlockedReason"
            id="void-confirm-input"
            v-model="voidConfirmText"
            class="void-type-input"
            autocomplete="off"
            data-testid="void-confirm-input"
            :placeholder="invoice.invoice_number"
          />
        </div>
        <template #footer>
          <Button
            label="Cancel"
            severity="secondary"
            data-testid="void-cancel-btn"
            @click="showVoidDialog = false"
          />
          <Button
            label="Void invoice"
            icon="pi pi-ban"
            severity="danger"
            data-testid="void-confirm-btn"
            :disabled="!voidConfirmMatches"
            :loading="voiding"
            @click="voidInvoice"
          />
        </template>
      </Dialog>

      <!-- ConfirmDialog removed 2026-05-12 — AppLayout.vue:49 already mounts
           one globally, and PrimeVue's useConfirm() broadcasts to every
           mounted instance, causing duplicate dialog renders. -->

      <CustomerFormDialog
        v-model:visible="showCustomerEditDialog"
        mode="edit"
        :customer="customerForEdit"
        @saved="onCustomerSaved"
      />

      <Toast data-testid="invoice-detail-toast" />
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useToast } from "primevue/usetoast";
import { useApiWithToast as useApi } from "../composables/useApiWithToast";
import { qbSyncLabel } from "../composables/qbSyncLabel";
import { formatDate, formatMoney, formatPercent, formatPhone, formatStampDateTime } from "../composables/useFormatters";
import { useDestructiveConfirm } from "../composables/useDestructiveConfirm";
import { usePermission } from "../composables/usePermission";
import { invoiceStatusSeverity as statusSeverity } from "../utils/statusSeverity";
import { useTenantModules } from "../composables/useTenantModules";
import { openAuthedFile } from "../composables/useAuthedFile";
import { useTenantTimezone } from "../composables/useTenantTimezone";
import Button from "primevue/button";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import Divider from "primevue/divider";
import Select from "primevue/select";
import ToggleSwitch from "primevue/toggleswitch";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
import Toast from "primevue/toast";
import LineItemEditor from "../components/LineItemEditor.vue";
import CustomerFormDialog from "../components/CustomerFormDialog.vue";
import ComposerPdfPreview from "../components/ComposerPdfPreview.vue";
import AuthedImage from "../components/AuthedImage.vue";
import { LINE_CATEGORY_OPTIONS } from "../composables/useLineCategories";

const api = useApi();
const route = useRoute();
const router = useRouter();
const { confirmDestructive, confirmAsync } = useDestructiveConfirm();
const toast = useToast();

const loading = ref(true);
const savingPayment = ref(false);
const showPaymentDialog = ref(false);
const qbConnected = ref(false);
const pushingToQb = ref(false);


// Job photos on the invoice PDF (2026-08-07). jobPhotos = the job's photo
// roll; invoice.attached_photo_ids = the current pick. Toggling PATCHes the
// whole list (draft only) — the server validates every id against the job.
const jobPhotos = ref([]);
const jobPhotosError = ref("");
const photoSaving = ref(false);

function isPhotoAttached(id) {
  return (invoice.value.attached_photo_ids || []).includes(id);
}

async function fetchJobPhotos() {
  jobPhotos.value = [];
  jobPhotosError.value = "";
  const jobId = invoice.value.job_id;
  if (!jobId) return;
  try {
    const rows = await api.get(`/api/jobs/${jobId}/photos`, { suppressErrorToast: true });
    jobPhotos.value = Array.isArray(rows) ? rows : [];
  } catch (err) {
    // Still never blocks the invoice view — but the failure is now VISIBLE.
    // Swallowing it into [] made a denied read look identical to a job with no
    // photos, which is how "there's no way to add photos" became the truth on
    // screen for anyone the job-access gate turned away.
    jobPhotos.value = [];
    jobPhotosError.value = (err?.status === 403 || err?.status === 404)
      ? "You don't have access to this job's photos."
      : "Couldn't load this job's photos.";
  }
}

async function togglePhoto(id) {
  if (invoice.value.status !== "draft" || photoSaving.value) return;
  const current = invoice.value.attached_photo_ids || [];
  const next = current.includes(id) ? current.filter((p) => p !== id) : [...current, id];
  photoSaving.value = true;
  const prev = current;
  invoice.value.attached_photo_ids = next; // optimistic — checkbox answers instantly
  try {
    await api.patch(`/api/invoices/${invoice.value.id}`, { attached_photo_ids: next });
  } catch (e) {
    invoice.value.attached_photo_ids = prev;
    toast.add({ severity: "error", summary: "Photo not saved", detail: e.message || "Try again.", life: 4000 });
  } finally {
    photoSaving.value = false;
  }
}

// Email composer (mirrors EstimateView). Server preps {to,subject,body_text,
// pdf{base64}} via /api/invoices/{id}/email-compose. User reviews + edits.
// Send routes through /api/outlook/send (PDF auto-attached). 409 falls back
// to mailto + downloading the PDF locally so the user can drag-attach.
const showComposer = ref(false);
const composerLoading = ref(false);
const composerSending = ref(false);
const composerPreviewing = ref(false);
const composerHasRecipient = computed(() =>
  composer.value.recipients?.length ? true : Boolean(composer.value.to)
);
function recipientDisplay(contactId) {
  const opt = (composer.value.recipients || []).find((r) => r.contact_id === (contactId || ""));
  return opt ? `${opt.name} <${opt.email}>` : composer.value.to || "Choose a recipient";
}
const composer = ref({ to: "", subject: "", body_text: "", pdf: null, extras: [] });
const paymentMethods = ["Cash", "Check", "Card", "Zelle", "Venmo", "ACH", "Other"];
const newPayment = ref({ amount: 0, method: "Cash", reference: "", date: "" });
// Tenant-zone calendar day — a UTC slice dates evening payments tomorrow.
const { zonedDateKey } = useTenantTimezone();
const todayKey = () => zonedDateKey(new Date());

function openPaymentDialog() {
  // Prefill the balance instead of 0. The app knows the number and used to make
  // the operator retype it from memory — and a mistyped LOW amount is silent
  // damage: a partly-paid deposit is credit-memo'd as "superseded" at
  // final-invoice time rather than voided, so the shortfall is written off and
  // the customer is over-billed while every screen reads "paid".
  const balance = Number(
    invoice.value?.balance_due ?? invoice.value?.total ?? 0,
  );
  newPayment.value = {
    amount: balance > 0 ? balance : 0,
    method: "Cash",
    reference: "",
    date: todayKey(),
  };
  showPaymentDialog.value = true;
}
// Tier-2 UI doors — credit lifecycle + finalize
const showCreditMemoDialog = ref(false);
const creditMemo = ref({ amount: 0, reason: "" });
const savingCreditMemo = ref(false);
const showApplyCreditDialog = ref(false);
const applyCredit = ref({ amount: 0 });
const savingApplyCredit = ref(false);
const finalizing = ref(false);
// Customer credits live on the GL — with ledger posting off (its prod state
// until the CPA sign-off) the apply-credit endpoint 409s on every call, so
// the button only renders when posting is actually on.
const glPostingEnabled = ref(false);

async function loadGlPosting() {
  try {
    const data = await api.get("/api/accounting/settings", { suppressErrorToast: true });
    glPostingEnabled.value = Boolean(data?.settings?.ledger_posting_enabled);
  } catch {
    glPostingEnabled.value = false;
  }
}
// D-S122b-detail-view-columns — same category set as InvoiceCreateView.
// Shared with /billing/new via composables/useLineCategories.js — this was the
// second of three hardcoded copies. EstimateView still holds the third (a plain
// string array, different shape); p5 replaces all of them with
// GET /api/catalogs/pricing-categories.
const lineCategoryOptions = LINE_CATEGORY_OPTIONS;
// Tenant-configured default rate (decimal fraction, e.g. 0.0738 == 7.38%).
// Loaded once from /api/tax/config in fetchInvoice; used as the seed value
// when entering edit mode on a legacy invoice that has no rate of its own.
const taxRate = ref(0.0);

// --- Edit-mode state. Only meaningful while editing=true. ---
const editing = ref(false);
const savingEdit = ref(false);
const editLines = ref([]);          // {_key, id?, description, quantity, unit_price, taxable}
// Closeout billing suggestion for this invoice's job — feeds the Add Labor
// picker's attested lane. Same endpoint /billing/new uses, fetched lazily on
// entering edit mode so a read-only view costs nothing.
const closeoutSuggestion = ref(null);

async function loadCloseoutSuggestion() {
  closeoutSuggestion.value = null;
  const jobId = invoice.value?.job_id;
  if (!jobId) return;
  try {
    closeoutSuggestion.value = await api.get(
      `/api/jobs/${jobId}/closeout-billing-suggestion`,
      { suppressErrorToast: true },
    );
  } catch {
    // Best-effort: the matrix lane still works, lane 2 just stays hidden.
  }
}
const editTaxRatePct = ref(0);      // displayed as percent (e.g., 7.38), not decimal
const editInvoiceDate = ref("");    // ISO yyyy-mm-dd
const editDueDate = ref("");
const editNotes = ref("");
const editHideLinePrices = ref(false);
const tenantDefaultRatePct = computed(() => taxRate.value * 100);

const verifying = ref(false);
async function verifyInvoice() {
  // Deliberately NOT gated behind a confirm dialog. The §11 gate asks "has a
  // human signed off on these numbers" and never "is anything missing",
  // which is what let a labor-only draft sail through -- but
  // useDestructiveConfirm auto-accepts silently (issue #215), so a confirm
  // here would LOOK like a second gate while stopping nothing. A control
  // that no-ops is the defect class this whole stack exists to remove. The
  // banner above the fold is the real surfacing, and it renders before the
  // Verify button is ever reached. Revisit once #215 is fixed.
  verifying.value = true;
  try {
    const r = await api.post(`/api/invoices/${route.params.id}/verify`, {});
    invoice.value.verified_at = r?.verified_at || new Date().toISOString();
    toast.add({ severity: "success", summary: "Invoice verified", detail: "The tech can now send it from the field.", life: 3000 });
  } catch (e) {
    toast.add({ severity: "error", summary: "Verify failed", detail: e?.message || "Try again.", life: 4000 });
  } finally {
    verifying.value = false;
  }
}

// Paper invoices — printed and posted, no email involved. mark-sent with
// channel 'mail' stamps sent_at (the delivery fact) + sent_via, flipping
// Draft → Sent and clearing the row from Billing's Unsent tab.
const markingMailed = ref(false);
// §11 rail (2026-08-08): delivery endpoints refuse unverified drafts. The
// office clicking Send/Mail IS the review moment, so offer verify-and-
// continue in one motion instead of bouncing them to a separate button.
async function ensureVerifiedForDelivery() {
  if (String(invoice.value.status || "").toLowerCase() !== "draft" || invoice.value.verified_at) {
    return true;
  }
  const ok = await confirmAsync({
    message:
      "This draft hasn't been verified. Verify it now (recording you as the reviewer) and continue?",
    header: "Verify and continue",
    acceptLabel: "Verify and continue",
    acceptClass: "p-button-success",
  });
  if (!ok) return false;
  try {
    await api.post(`/api/invoices/${invoice.value.id}/verify`, {});
    await fetchInvoice();
    return true;
  } catch (e) {
    toast.add({ severity: "error", summary: "Verify failed", detail: e?.message || "Try again.", life: 4000 });
    return false;
  }
}

async function markAsMailed() {
  if (!(await ensureVerifiedForDelivery())) return;
  markingMailed.value = true;
  try {
    await api.post(`/api/invoices/${route.params.id}/mark-sent`, { channel: "mail" });
    await fetchInvoice();
    toast.add({
      severity: "success",
      summary: "Marked as mailed",
      detail: "Recorded as delivered by mail — it no longer counts as unsent.",
      life: 4000,
    });
  } catch (e) {
    toast.add({ severity: "error", summary: "Mark as mailed failed", detail: e?.message || "Try again.", life: 4000 });
  } finally {
    markingMailed.value = false;
  }
}

const invoice = ref({
  id: null,
  invoice_number: "",
  customer_id: null,
  customer_name: "",
  customer_email: "",
  customer_phone: "",
  customer_address: "",
  status: "Draft",
  total: 0,
  due_date: "",
  created_at: "",
  notes: "",
  line_items: [],
  payments: [],
});

// Void. `hasPermission` is UX glue only — the server gates the route on
// invoices.write; this just stops offering a button that would 403.
const { hasPermission } = usePermission();
const canWriteInvoices = computed(() => hasPermission("invoices.write"));
const showVoidDialog = ref(false);
const voidConfirmText = ref("");
const voiding = ref(false);
// Trim, and compare case-insensitively: the operator is retyping a number
// they can see, not proving they can match whitespace.
// The server refuses a void while any NON-voided payment exists: "voiding a
// bill while keeping its money would silently orphan the cash." Rather than
// let the operator read a scary permanent-deletion warning, retype the whole
// invoice number and THEN eat a 409, say it up front. Prefer the server's
// amount_paid; fall back to the payments sum (which includes voided rows and
// so only ever over-blocks) when an older payload omits it.
const livePaidAmount = computed(() => {
  const server = invoice.value?.amount_paid;
  return server === undefined || server === null ? totalPaid.value : toNum(server);
});
const voidBlockedReason = computed(() => {
  if (livePaidAmount.value > 0) {
    return "This invoice has recorded payments. Void or remove those payments first — voiding a bill while keeping its money would leave the cash with nothing to belong to.";
  }
  return "";
});
const voidConfirmMatches = computed(() => {
  if (voidBlockedReason.value) return false;
  const target = String(invoice.value?.invoice_number || "").trim().toLowerCase();
  return target.length > 0 && voidConfirmText.value.trim().toLowerCase() === target;
});

// Customer-edit dialog state. customerForEdit holds the full customer record
// (loaded just-in-time when the user clicks Edit) so the dialog can preserve
// fields the invoice payload doesn't carry (notes, access_notes, etc.).
const showCustomerEditDialog = ref(false);
const customerForEdit = ref(null);

// Tier 10 — per-record QuickBooks push state. Only shown when the tenant has
// the QuickBooks module enabled (otherwise every row reads "not synced", which
// is technically true but pure noise for a tenant that doesn't use QB).
const { isEnabled } = useTenantModules();
const qbEnabled = computed(() => isEnabled("quickbooks"));
const qbSync = computed(() => qbSyncLabel(invoice.value, formatStampDateTime));

// --- Computed ---
const subtotal = computed(() =>
  invoice.value.line_items.reduce((sum, item) => sum + lineTotal(item), 0)
);
const tax = computed(() => subtotal.value * taxRate.value);
const totalPaid = computed(() =>
  invoice.value.payments.reduce((sum, p) => sum + toNum(p.amount), 0)
);
// Mirror the server's balance formula (_recalculate_invoice): credit memos
// and applied credits reduce the balance; refunds don't. Without this a
// credit-memo'd invoice (e.g. a superseded deposit) showed a red fake
// balance next to its "paid" tag.
const totalAdjustmentCredits = computed(() =>
  (invoice.value.adjustments || [])
    .filter((a) => a.kind === "credit_memo" || a.kind === "credit_applied")
    .reduce((sum, a) => sum + toNum(a.amount), 0)
);
const balanceDue = computed(() =>
  toNum(invoice.value.total) - totalPaid.value - totalAdjustmentCredits.value
);

// Edit mode is gated on draft status — once an invoice is sent or paid,
// the source-of-truth is whatever the customer received.
const canEdit = computed(() => {
  const s = String(invoice.value.status || "").toLowerCase();
  return s === "draft";
});

const editSubtotal = computed(() =>
  editLines.value.reduce((sum, ln) => sum + lineTotal(ln), 0),
);
const editTaxableSubtotal = computed(() =>
  editLines.value.reduce((sum, ln) => sum + (ln.taxable ? lineTotal(ln) : 0), 0),
);
const editTax = computed(() => editTaxableSubtotal.value * (toNum(editTaxRatePct.value) / 100));
const editTotal = computed(() => editSubtotal.value + editTax.value);

// --- Helpers ---
function toNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function lineTotal(item) {
  return toNum(item.quantity) * toNum(item.unit_price);
}

// The deposit-netting line the server adds to a final invoice: category
// "Deposit" (DEPOSIT_CATEGORY, modules/deposits/service.py) with a negative
// total. The deposit invoice's own positive line shares the category, so the
// sign check matters. Server-owned — the API 409s any edit/delete of it.
function isDepositNettingLine(item) {
  return String(item?.category || "") === "Deposit" && lineTotal(item) < 0;
}

function currency(value) {
  return formatMoney(toNum(value));
}

function adjustmentKindLabel(kind) {
  const map = {
    credit_memo: "Credit memo",
    refund: "Refund",
    credit_applied: "Credit applied",
  };
  return map[kind] || kind || "—";
}

function normalizeInvoice(payload) {
  // Backend `_serialize_invoice` puts the line array under `lines`. Pre-fix
  // this only checked `line_items`/`lineItems`/`items` so the InvoiceDetail
  // page rendered "No line items" for every QB-imported invoice. Same shape
  // bug as EstimateDetailView (Apr 30 2026 walk-through).
  const lineItems = (payload.lines || payload.line_items || payload.lineItems || payload.items || []).map((item, i) => ({
    id: item.id ?? `line-${i}`,
    description: item.description || "",
    quantity: toNum(item.quantity ?? 1),
    unit_price: toNum(item.unit_price ?? item.unitPrice ?? item.amount ?? 0),
    // Default true when the server didn't tell us — matches the column's
    // server_default. Only legacy QB-imported lines might come back without
    // an explicit value and historically those were treated as taxable.
    taxable: item.taxable === undefined ? true : Boolean(item.taxable),
    // S122-b detail-view parity — the DataTable columns at lines 91-127 read
    // these fields. Pre-fix the normalizer dropped them, so the columns fell
    // through to "—" even though the DB had real values. Forward as-is —
    // currency/percent formatting happens in the template.
    category: item.category ?? null,
    // Same class of bug S122-b fixed for category/cost/margin four lines up:
    // a field the normalizer drops is written to the DB and then invisible
    // forever, so the office re-ticks a box that is already true.
    includes_labor: Boolean(item.includes_labor),
    cost_snapshot: item.cost_snapshot ?? null,
    margin_pct_snapshot: item.margin_pct_snapshot ?? null,
    margin_pct_override: item.margin_pct_override ?? null,
    part_id: item.part_id ?? null,
    // Labor provenance (071). THE SAME BUG THIS COMMENT BLOCK ALREADY NAMES
    // TWICE: a field the normalizer drops is written to the DB and then
    // invisible forever. Dropping these meant edit mode had no provenance, so
    // repricing a $650 matrix-quoted labor line to $900 left the row still
    // claiming the matrix quoted $900 — the exact falsehood migration 071 and
    // the contract validator exist to prevent.
    labor_source: item.labor_source ?? null,
    labor_price_item_id: item.labor_price_item_id ?? null,
    estimated_man_hours: item.estimated_man_hours ?? null,
  }));

  const payments = (payload.payments || payload.payment_history || []).map((p, i) => ({
    id: p.id ?? `pay-${i}`,
    amount: toNum(p.amount),
    method: p.method || "Cash",
    reference: p.reference || p.notes || "",
    date: p.date || p.paid_at || p.created_at || "",
  }));

  const computedTotal = lineItems.reduce((s, li) => s + toNum(li.quantity) * toNum(li.unit_price), 0);
  // Trust the server for the rate. Don't default to a hardcoded 8.25% —
  // that's been silently distorting QB-imported invoices' totals (Doug
  // 2026-05-06 / S110). Only overwrite the tenant-default rate (loaded
  // separately by loadTaxRate) when the invoice itself carries one.
  const serverRate = payload.tax_rate ?? payload.taxRate;
  if (serverRate != null) {
    const rate = toNum(serverRate);
    // Already a decimal fraction on the server; tolerate older payloads
    // that pass percentages by detecting >1.
    taxRate.value = rate > 1 ? rate / 100 : rate;
  }

  invoice.value = {
    id: payload.id,
    invoice_number: payload.invoice_number || payload.invoiceNumber || `INV-${String(payload.id).substring(0, 8)}`,
    // The server's LIVE paid figure (core/invoice_paid.py — sum of payments
    // that are not voided). The `payments` array below carries voided rows
    // too, so summing it over-counts; this is the number the void guard has
    // to key on, because the server refuses a void only while a NON-voided
    // payment exists. Another field the normalizer used to drop.
    amount_paid: toNum(payload.amount_paid ?? 0),
    customer_id: payload.customer_id || null,
    // 2026-08-12 browser walk: THIS is why the job-photo picker never worked.
    // This normalizer copies fields explicitly, job_id was never listed, so
    // `invoice.job_id` was permanently undefined — the picker's v-if could not
    // be true and fetchJobPhotos() returned early without ever asking the
    // server. The card shipped in v1.44.0 and had rendered for nobody since;
    // production has zero invoices carrying a photo, which is the same fact
    // seen from the database end.
    job_id: payload.job_id || null,
    customer_name: payload.customer_name || payload.customer || (typeof payload.customer === "object" ? payload.customer?.name : "") || "Unknown",
    customer_email: payload.customer_email || "",
    customer_phone: payload.customer_phone || "",
    customer_address: payload.customer_address || "",
    status: payload.effective_status || payload.status || "Draft",
    subtotal: toNum(payload.subtotal),
    taxable_subtotal: payload.taxable_subtotal === undefined ? undefined : toNum(payload.taxable_subtotal),
    tax_rate: payload.tax_rate == null ? null : toNum(payload.tax_rate),
    tax_amount: toNum(payload.tax_amount),
    total: toNum(payload.total ?? payload.amount ?? computedTotal),
    invoice_date: payload.invoice_date || payload.invoiceDate || "",
    due_date: payload.due_date || payload.dueDate || "",
    created_at: payload.created_at || payload.createdAt || "",
    verified_at: payload.verified_at || null,
    sent_at: payload.sent_at || "",
    sent_via: payload.sent_via || "",
    notes: payload.notes || "",
    // Deposit lifecycle (2026-07-24): the tag, the "View Estimate" link and
    // the adjustments panel all need these — the normalizer used to drop
    // them, which is why a deposit invoice was indistinguishable from a
    // final on this page.
    billing_type: payload.billing_type || "standard",
    estimate_id: payload.estimate_id || null,
    // Migration 072 — the chip renders for either link, so the normalizer has
    // to carry both. Dropping it here is the same "written to the DB and then
    // invisible forever" bug this file has now hit three times.
    source_estimate_id: payload.source_estimate_id || null,
    adjustments: payload.adjustments || [],
    is_superseded_deposit: Boolean(payload.is_superseded_deposit),
    // PR6 — drives the Pause/Resume reminders toggle.
    dunning_paused: Boolean(payload.dunning_paused),
    // Tier-2 finalize door — without this the Finalize button never
    // disappears after locking (the audit's "survives its own success").
    locked: Boolean(payload.locked),
    // Drives the edit-mode "hide line-item prices on PDF" toggle.
    hide_line_prices: Boolean(payload.hide_line_prices),
    // Job photos picked for the PDF — drives the photo-picker checkboxes.
    attached_photo_ids: Array.isArray(payload.attached_photo_ids) ? payload.attached_photo_ids : [],
    // Machine provenance — drives the "auto-drafted from closeout" tag.
    origin: payload.origin || null,
    // M11 money-audit detector (2026-08-08: computed + serialized since
    // v1.41.1 but rendered NOWHERE — money collected above the total was
    // invisible on every surface). Drives the overpayment banner.
    amount_overpaid: Number(payload.amount_overpaid) || 0,
    // Tier 10 — per-record QuickBooks push state for the sync chip. This
    // normalizer copies fields explicitly, so these must be listed or the chip
    // reads undefined and always renders "Not in QuickBooks".
    qb_dirty: payload.qb_dirty,
    qb_synced_at: payload.qb_synced_at || null,
    qb_in_quickbooks: Boolean(payload.qb_in_quickbooks),
    line_items: lineItems,
    payments,
  };
}

// --- Actions ---
async function openCustomerEdit() {
  if (!invoice.value.customer_id) return;
  // Pull the full customer so the dialog edits a complete record (notes,
  // access_notes, customer_type, etc. aren't on the invoice payload).
  try {
    const result = await api.get(`/api/customers/${invoice.value.customer_id}`);
    customerForEdit.value = result?.data || result || {
      id: invoice.value.customer_id,
      name: invoice.value.customer_name,
      email: invoice.value.customer_email,
      phone: invoice.value.customer_phone,
      address: invoice.value.customer_address,
    };
  } catch {
    // Fall back to the slice we already have on the invoice payload so the
    // dialog still opens — the user can at least add the missing email.
    customerForEdit.value = {
      id: invoice.value.customer_id,
      name: invoice.value.customer_name,
      email: invoice.value.customer_email,
      phone: invoice.value.customer_phone,
      address: invoice.value.customer_address,
    };
  }
  showCustomerEditDialog.value = true;
}

async function onCustomerSaved() {
  // Re-fetch so the Bill-To card reflects the saved fields (server is the
  // source of truth — esp. for encrypted columns like address).
  await fetchInvoice();
}

// Parts the tech recorded on this job that are NOT on this invoice
// (2026-08-19). job-closeout-billing-visibility-plan §8 decided this in
// 2026-07: build the invoice from everything priced, leave the rest on the
// checklist, and MARK THE INVOICE so the office knows. Only the mobile lane
// ever got that mark; the desktop lane surfaced nothing, so
// require_deliverable asked "did a human sign off" and never "is anything
// missing" -- the rubber-stamp failure that plan predicted at its line 913.
//
// Draft-only and job-only: a sent invoice is history, and a counter sale has
// no job whose parts could be missing. Best-effort -- a failed read must
// never block the page.
const unbilledJobParts = ref([]);
// 403 is NOT "nothing missing". The accounting role carries invoices.write
// and billing.read but NOT inventory.read, so the very user who verifies
// drafts gets a permission error here -- and a silent empty banner reads to
// them as an all-clear on a money screen. LineItemEditor learned this once
// already (D-S122-parts-panel-silent-hide); the URL was copied from it, the
// lesson was not.
const unbilledPartsError = ref(null); // null | 'forbidden'
async function fetchUnbilledJobParts() {
  unbilledJobParts.value = [];
  unbilledPartsError.value = null;
  const jobId = invoice.value?.job_id;
  const isDraft = String(invoice.value?.status || "").toLowerCase() === "draft";
  if (!jobId || !isDraft) return;
  try {
    const r = await api.get(
      `/api/jobs/${encodeURIComponent(jobId)}/parts-needed?status=ordered,received,used&unbilled=true`,
      { suppressErrorToast: true },
    );
    const rows = Array.isArray(r) ? r : Array.isArray(r?.data) ? r.data : [];
    // "Unbilled" is job-wide; this banner claims something narrower — not on
    // THIS invoice. A line already billing the part (part_id linkage) must
    // not be reported as missing, or the office follows the button, adds a
    // second line for a part already charged, and the new claim silences the
    // banner: a false alarm laundering itself into a double charge.
    const linedPartIds = new Set(
      (invoice.value?.line_items || [])
        .map((l) => l.part_id)
        .filter(Boolean)
        .map(String),
    );
    unbilledJobParts.value = rows.filter((p) => !linedPartIds.has(String(p.id)));
  } catch (e) {
    unbilledJobParts.value = [];
    if (e?.status === 403) unbilledPartsError.value = "forbidden";
  }
}

async function fetchInvoice() {
  loading.value = true;
  try {
    const result = await api.get(`/api/invoices/${route.params.id}`);
    normalizeInvoice(result?.data || result || {});
    fetchJobPhotos(); // fire-and-forget — the picker card fills in when it lands
    fetchUnbilledJobParts(); // fire-and-forget — banner fills in when it lands
  } catch {
    toast.add({ severity: "warn", summary: "Offline", detail: "Using placeholder data", life: 3000 });
    normalizeInvoice({
      id: route.params.id,
      invoice_number: `INV-${route.params.id}`,
      customer: "Sample Customer",
      status: "Draft",
      due_date: "2026-04-20",
      line_items: [
        { id: 1, description: "Service call", quantity: 1, unit_price: 150 },
        { id: 2, description: "Parts", quantity: 2, unit_price: 75 },
      ],
      payments: [],
    });
  } finally {
    loading.value = false;
  }
}

async function sendInvoice() {
  // 2026-05-15 — replaced the direct POST /send (which fired a server-side
  // HTML email with no PDF attached) with the same composer flow estimates
  // use: open dialog with PDF preview → user reviews → send via Outlook (or
  // mailto fallback). 2026-05-12 accidental-send guardrail is now built into
  // the dialog itself: nothing leaves the browser until the user clicks Send.
  if (!(await ensureVerifiedForDelivery())) return;
  composerLoading.value = true;
  showComposer.value = true;
  composer.value = {
    to: "", subject: "", body_text: "", pdf: null, extras: [],
    recipients: [], contact_id: "", previewHtml: "", prefillBody: "",
  };
  try {
    const data = await api.get(`/api/invoices/${route.params.id}/email-compose`);
    const payload = data?.data || data;
    composer.value = {
      to: (payload.to && payload.to[0]) || "",
      subject: payload.subject || "",
      body_text: payload.body_text || "",
      prefillBody: payload.body_text || "",
      pdf: payload.pdf,
      extras: (payload.extra_attachments || []).map((a) => ({ ...a, _include: true })),
      recipients: (payload.recipients || []).map((r) => ({ ...r, contact_id: r.contact_id || "" })),
      contact_id: payload.selected_contact_id || "",
      customer_id: payload.customer_id || null,
      previewHtml: "",
    };
  } catch (err) {
    showComposer.value = false;
    toast.add({ severity: "error", summary: "Compose failed", detail: err?.message || "", life: 4000 });
  } finally {
    composerLoading.value = false;
  }
}

const canMakeDefaultRecipient = computed(() => {
  const opt = (composer.value.recipients || []).find(
    (r) => r.contact_id === (composer.value.contact_id || "")
  );
  return Boolean(opt && opt.contact_id && !opt.is_primary && composer.value.customer_id);
});

async function makeDefaultRecipient() {
  const contactId = composer.value.contact_id;
  const customerId = composer.value.customer_id;
  if (!contactId || !customerId) return;
  try {
    await api.post(
      `/api/customers/${customerId}/contacts/${contactId}/make-primary`, {},
      { successMessage: "Saved — automated emails for this account now go to this person." },
    );
    composer.value.recipients = composer.value.recipients.map((r) => ({
      ...r, is_primary: r.contact_id === contactId,
    }));
  } catch (err) {
    toast.add({ severity: "error", summary: "Could not save default", detail: err?.message || "", life: 4000 });
  }
}

async function onRecipientChange() {
  // Re-prefill the greeting for the newly chosen person — but never clobber
  // copy the operator already edited; then only the address changes.
  const contactId = composer.value.contact_id || "";
  const opt = (composer.value.recipients || []).find((r) => r.contact_id === contactId);
  if (opt) composer.value.to = opt.email;
  const edited = composer.value.body_text !== composer.value.prefillBody;
  if (edited) return;
  try {
    const q = contactId ? `?contact_id=${encodeURIComponent(contactId)}` : "";
    const data = await api.get(`/api/invoices/${route.params.id}/email-compose${q}`, { suppressErrorToast: true });
    const payload = data?.data || data;
    composer.value.subject = payload.subject || composer.value.subject;
    composer.value.body_text = payload.body_text || composer.value.body_text;
    composer.value.prefillBody = payload.body_text || "";
    composer.value.previewHtml = "";
  } catch {
    // Prefill refresh is cosmetic — the send path re-resolves server-side.
  }
}

async function previewComposer() {
  composerPreviewing.value = true;
  try {
    const data = await api.post(`/api/invoices/${route.params.id}/email-preview`, {
      body_text: composer.value.body_text,
      subject: composer.value.subject,
      contact_id: composer.value.contact_id || null,
      // Free-typed address (no stored recipients) must reach the server —
      // audit catch: it used to be collected and silently dropped.
      to_email: composer.value.recipients.length ? null : (composer.value.to || null),
    }, { suppressErrorToast: true });
    const payload = data?.data || data;
    composer.value.previewHtml = payload.html || "";
  } catch (err) {
    toast.add({ severity: "error", summary: "Preview failed", detail: err?.message || "", life: 4000 });
  } finally {
    composerPreviewing.value = false;
  }
}

async function sendComposer() {
  if (!composerHasRecipient.value) return;
  composerSending.value = true;
  try {
    // Server-side render + send: the backend wraps this copy in the branded
    // shell (line items, settlement rows, clickable pay button), resolves
    // the recipient, attaches the PDF, delivers via Outlook Graph or SMTP,
    // stamps status/sent_at/sent_via, and records the attempt in the
    // outbound email log. Replaces the browser-built pre-wrapped body,
    // the direct Outlook relay, and the separate mark-sent call.
    const result = await api.post(`/api/invoices/${route.params.id}/send`, {
      body_text: composer.value.body_text,
      subject: composer.value.subject,
      contact_id: composer.value.contact_id || null,
    }, { suppressErrorToast: true });
    const payload = result?.data || result;
    if (payload.email_sent) {
      await fetchInvoice();
      toast.add({
        severity: "success",
        summary: "Sent",
        detail: `Invoice emailed to ${composer.value.to || "the customer"}.`,
        life: 5000,
      });
      showComposer.value = false;
    } else if (
      ["no_email_provider_connected", "outlook_not_connected", "outlook_reconnect_required"]
        .includes(payload.email_skip_reason)
    ) {
      toast.add({
        severity: "info",
        summary: "Opening your mail client",
        detail: "No email provider is connected for this user — using your default mail client instead.",
        life: 5000,
      });
      await _emailViaMailtoFallback(composer.value, {
        name: composer.value.pdf.name,
        content_type: composer.value.pdf.content_type,
        content_base64: composer.value.pdf.content_base64,
      });
      showComposer.value = false;
    } else {
      toast.add({
        severity: "error",
        summary: "Send failed",
        detail: payload.email_skip_reason || "The email could not be delivered",
        life: 6000,
      });
    }
  } catch (err) {
    toast.add({ severity: "error", summary: "Send failed", detail: err?.message || "", life: 5000 });
  } finally {
    composerSending.value = false;
  }
}

async function _emailViaMailtoFallback(c, pdfAtt) {
  // Save PDF locally so the user can drag-attach into their default mail
  // client. mailto: itself doesn't carry attachments — we surface both.
  const bytes = Uint8Array.from(atob(pdfAtt.content_base64), (ch) => ch.charCodeAt(0));
  const blob = new Blob([bytes], { type: pdfAtt.content_type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = pdfAtt.name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
  const mailto = `mailto:${encodeURIComponent(c.to)}?subject=${encodeURIComponent(c.subject)}&body=${encodeURIComponent(c.body_text)}`;
  window.location.href = mailto;
  try {
    await api.post(`/api/invoices/${route.params.id}/mark-sent`, { channel: "email" }, { suppressErrorToast: true });
    await fetchInvoice();
  } catch (mse) {
    // Same as the Outlook path: surface a status-flip failure so the
    // operator knows the email left their hands but the row still says Draft.
    toast.add({
      severity: "warn",
      summary: "Email handed to your mail client",
      detail: "Status not auto-flipped — verify it sent, then mark this invoice as Sent manually.",
      life: 7000,
    });
  }
}

function formatBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / (1024 * 1024)).toFixed(2)} MB`;
}

async function toggleDunningPause() {
  // PR6 — explicit per-invoice mute (payment arrangement made).
  try {
    const next = !invoice.value.dunning_paused;
    await api.post(`/api/invoices/${id}/dunning-pause`, { paused: next });
    invoice.value.dunning_paused = next;
    toast.add({
      severity: 'info',
      summary: next ? 'Reminders paused' : 'Reminders resumed',
      detail: next
        ? 'Automated payment reminders are muted for this invoice.'
        : 'This invoice is back on the automated reminder schedule.',
      life: 3000,
    });
  } catch (e) {
    toast.add({ severity: 'error', summary: 'Error', detail: e.message || 'Failed to update reminders', life: 4000 });
  }
}

async function issueCreditMemo() {
  savingCreditMemo.value = true;
  try {
    // Server re-caps against the live balance and refuses drafts/voids —
    // the button visibility is UX, the server check is the contract.
    const result = await api.post(
      `/api/invoices/${route.params.id}/credit-memo`,
      { amount: creditMemo.value.amount, reason: creditMemo.value.reason.trim() },
      { suppressErrorToast: true },
    );
    showCreditMemoDialog.value = false;
    creditMemo.value = { amount: 0, reason: "" };
    toast.add({
      severity: "success",
      summary: "Credit issued",
      detail: `Balance due is now ${currency(result?.balance_due ?? 0)}`,
      life: 4000,
    });
    await fetchInvoice();
  } catch (err) {
    toast.add({ severity: "error", summary: "Credit memo failed", detail: err.message || "Could not issue credit", life: 5000 });
  } finally {
    savingCreditMemo.value = false;
  }
}

async function applyCustomerCredit() {
  savingApplyCredit.value = true;
  try {
    await api.post(
      `/api/invoices/${route.params.id}/apply-credit`,
      { amount: applyCredit.value.amount },
      { suppressErrorToast: true },
    );
    showApplyCreditDialog.value = false;
    applyCredit.value = { amount: 0 };
    toast.add({ severity: "success", summary: "Credit applied", life: 3000 });
    await fetchInvoice();
  } catch (err) {
    // Most common refusal: ledger posting is off (customer credits live on
    // the ledger). Surface the server's own words.
    toast.add({ severity: "error", summary: "Apply credit failed", detail: err.message || "Could not apply credit", life: 6000 });
  } finally {
    savingApplyCredit.value = false;
  }
}

async function finalizeInvoice() {
  finalizing.value = true;
  try {
    await api.post(`/api/invoices/${route.params.id}/finalize`, {}, { successMessage: "Invoice locked" });
    await fetchInvoice();
  } catch {
    // fireError already toasted
  } finally {
    finalizing.value = false;
  }
}

async function recordPayment() {
  savingPayment.value = true;
  try {
    const result = await api.post(`/api/invoices/${route.params.id}/payments`, {
      amount: newPayment.value.amount,
      method: newPayment.value.method,
      reference: newPayment.value.reference,
      date: newPayment.value.date || todayKey(),
    });
    const saved = result?.data || result || {};
    invoice.value.payments.push({
      id: saved.id ?? `pay-${Date.now()}`,
      amount: toNum(saved.amount || newPayment.value.amount),
      method: saved.method || newPayment.value.method,
      reference: saved.reference || newPayment.value.reference,
      date: saved.date || newPayment.value.date || todayKey(),
    });
    // Update status if fully paid
    if (balanceDue.value <= 0) {
      invoice.value.status = "Paid";
    }
    showPaymentDialog.value = false;
    newPayment.value = { amount: 0, method: "Cash", reference: "", date: "" };
    toast.add({ severity: "success", summary: "Recorded", detail: "Payment recorded", life: 3000 });
  } catch (err) {
    toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to record payment", life: 3000 });
  } finally {
    savingPayment.value = false;
  }
}

// --- Edit-mode actions ---
function enterEditMode() {
  // Snapshot the current invoice into editLines + edit fields. _key is
  // a Vue v-for key that survives re-orders; lines new since enterEdit
  // get a temporary key so we can identify them at save time.
  editLines.value = invoice.value.line_items.map((ln, i) => ({
    _key: `e-${ln.id ?? i}`,
    id: typeof ln.id === "string" && ln.id.length >= 32 ? ln.id : null,
    description: ln.description || "",
    quantity: toNum(ln.quantity) || 1,
    unit_price: toNum(ln.unit_price),
    taxable: ln.taxable !== false,
    // D-S122b-detail-view-columns — snapshot the new fields too.
    category: ln.category || null,
    includes_labor: Boolean(ln.includes_labor),
    part_id: ln.part_id || null,
    cost: ln.cost_snapshot != null ? toNum(ln.cost_snapshot) : null,
    // NB: invoice_lines has no pricing_category column, so a line loaded here
    // never carries one and `bucketForLine` always falls through to mapping the
    // display category. Create and edit therefore agree only while the display
    // string and the item's bucket agree — true for every live catalog row, and
    // pinned by the round-trip test in useLineCategories.spec.js.
    // Form shows percent (e.g. 35); backend stores decimal (0.35). Round-
    // trip via *100 on entry and /100 on save.
    margin_pct_override: ln.margin_pct_override != null
      ? Number((ln.margin_pct_override * 100).toFixed(2))
      : null,
    // A margin already PERSISTED on the line was set by a human — nothing
    // writes that column automatically. Stamp it so the save-side gate (which
    // exists to stop the editor's tier auto-fill masquerading as an override)
    // forwards it instead of silently clearing a real one.
    //
    // This is `_marginPersisted`, NOT `_marginUserEdited`: the latter is also
    // the recompute flag and gets cleared by `markPriceOverride` on any price
    // edit, so gating persistence on it meant retyping a price nulled a stored
    // override while the cell still displayed a value.
    // BOTH flags, deliberately. `_marginPersisted` makes saveEdit forward it;
    // `_marginUserEdited` is what `recomputeSell` reads to take the override
    // branch instead of refilling from the tier. Stamping only the first meant
    // every loaded line was treated as tier-priced: typing a cost refilled the
    // cell with the tier margin and the persist flag then SAVED that, turning a
    // real 42% override into the tier's 50%. Both failure modes the gate exists
    // to prevent, in one keystroke.
    _marginPersisted: ln.margin_pct_override != null,
    _marginUserEdited: ln.margin_pct_override != null,
    // Provenance into the editor, plus the price it refers to — without
    // `_provenancePrice` the matrix -> manual downgrade can never fire here,
    // and a reprice would keep claiming the matrix quoted the new number.
    labor_source: ln.labor_source ?? null,
    labor_price_item_id: ln.labor_price_item_id ?? null,
    estimated_man_hours: ln.estimated_man_hours ?? null,
    ...(ln.labor_source === 'matrix'
      ? { _provenancePrice: toNum(ln.unit_price) } : {}),
  }));
  // Without this the Add Labor picker's attested lane is DEAD on this screen:
  // it hides lane 2 when `closeout` is null and never fetches for itself. The
  // office fixing a draft invoice would see "Bill these hours" on /billing/new
  // and not here, for the same job.
  loadCloseoutSuggestion();
  // Seed the rate input. Prefer the invoice's own rate; fall back to the
  // tenant default so legacy invoices get a sensible starting point on
  // first edit instead of showing 0%.
  const startRate = invoice.value.tax_rate != null
    ? toNum(invoice.value.tax_rate)
    : taxRate.value;
  editTaxRatePct.value = Number((startRate * 100).toFixed(4));
  editInvoiceDate.value = invoice.value.invoice_date || "";
  editDueDate.value = invoice.value.due_date || "";
  editNotes.value = invoice.value.notes || "";
  editHideLinePrices.value = Boolean(invoice.value.hide_line_prices);
  editing.value = true;
}

function cancelEdit() {
  editing.value = false;
  editLines.value = [];
}

async function saveEdit() {
  // Diff editLines against the current invoice, fire one request per
  // change. Server runs _recalculate_invoice on every line write so
  // totals/tax stay correct even if the patch sequence is interrupted.
  savingEdit.value = true;
  try {
    const id = route.params.id;
    const original = invoice.value.line_items;
    const originalById = new Map(original.map((ln) => [String(ln.id), ln]));
    const keptIds = new Set();

    // 1. Updates + inserts
    for (const ln of editLines.value) {
      // The deposit-netting line is server-owned: never PATCH it (the 409
      // aside, the Math.max(0, price) clamp below would zero its negative
      // price and flag it "changed" on every save), just keep it.
      if (isDepositNettingLine(ln)) {
        if (ln.id) keptIds.add(String(ln.id));
        continue;
      }
      const desc = (ln.description || "").trim();
      if (!desc) continue;  // skip rows with no description
      const qty = Math.max(1, Math.floor(toNum(ln.quantity) || 1));
      const price = Math.max(0, toNum(ln.unit_price));
      // D-S122b-detail-view-columns — forward category/cost/margin too.
      const category = ln.category || null;
      // Labor provenance (071). Sent together or not at all: the contract
      // rejects a matrix id without labor_source='matrix', and hours without a
      // lane. Without this the Add Labor button on THIS screen wrote rows that
      // could not say what priced them.
      const laborFields = ln.labor_source
        ? {
            labor_source: ln.labor_source,
            // Forward the row id for anything EXCEPT attested. A line
            // downgraded matrix -> manual by a reprice still came from that
            // row, and dropping the id here made both the downgrade's promise
            // ("the row id is KEPT") and the reason manual-with-an-id is legal
            // false on the one screen carrying the Add Labor button.
            ...(ln.labor_source !== 'attested' && ln.labor_price_item_id
              ? { labor_price_item_id: ln.labor_price_item_id } : {}),
            ...(ln.estimated_man_hours != null && toNum(ln.estimated_man_hours) > 0
              ? { estimated_man_hours: toNum(ln.estimated_man_hours) } : {}),
          }
        : {};
      const cost = ln.cost != null && toNum(ln.cost) > 0 ? toNum(ln.cost) : null;
      // An override is a margin a HUMAN chose. The shared editor also
      // auto-fills this column with the tier-implied margin whenever a cost is
      // typed, purely so the operator can see what they're running at — and
      // this view mounts that editor with show-cost + show-margin, so the fill
      // happens here too. Storing that would record a decision nobody made.
      //
      // Forward when EITHER the operator committed a margin this session
      // (`_marginUserEdited`) or the line arrived already carrying one
      // (`_marginPersisted`, stamped in enterEditMode). Whatever is displayed
      // is what gets stored — a price edit recomputes the shown margin to the
      // real one, and persisting that keeps the cell and the DB agreeing.
      const marginOverrideDec = (ln._marginUserEdited || ln._marginPersisted)
        && ln.margin_pct_override != null
        && toNum(ln.margin_pct_override) > 0
        ? toNum(ln.margin_pct_override) / 100
        : null;
      if (ln.id) {
        // Any row with a server id PATCHes — never re-POSTs. The id can be
        // missing from the snapshot when a previous save attempt created
        // the line and then failed before the resync landed; PATCHing with
        // absolute values is idempotent, a second POST is a duplicate.
        keptIds.add(String(ln.id));
        const orig = originalById.get(String(ln.id)) || null;
        const origCost = orig && orig.cost_snapshot != null ? toNum(orig.cost_snapshot) : null;
        const origMargin = orig && orig.margin_pct_override != null ? toNum(orig.margin_pct_override) : null;
        const changed =
          !orig ||
          orig.description !== desc ||
          toNum(orig.quantity) !== qty ||
          toNum(orig.unit_price) !== price ||
          (orig.taxable !== false) !== Boolean(ln.taxable) ||
          (orig.category || null) !== category ||
          Boolean(orig.includes_labor) !== Boolean(ln.includes_labor) ||
          origCost !== cost ||
          origMargin !== marginOverrideDec ||
          (orig.labor_source ?? null) !== (ln.labor_source ?? null);
        if (changed) {
          const patch = {
            description: desc,
            quantity: qty,
            unit_price: price,
            taxable: Boolean(ln.taxable),
          };
          // Auditor catch (round 2): include the field in the PATCH even when
          // the new value is null — backend's exclude_unset=True semantics
          // mean omitted fields stay unchanged, so clearing a cost requires
          // an explicit `cost: null`.
          if (!orig || category !== (orig.category || null)) patch.category = category;
          if (!orig || Boolean(orig.includes_labor) !== Boolean(ln.includes_labor)) {
            patch.includes_labor = Boolean(ln.includes_labor);
          }
          if (!orig || cost !== origCost) patch.cost = cost;
          if (!orig || marginOverrideDec !== origMargin) patch.margin_pct_override = marginOverrideDec;
          // A reprice DOWNGRADES matrix -> manual. That has to reach the DB or
          // the row keeps asserting a quote nobody made.
          if ((orig?.labor_source ?? null) !== (ln.labor_source ?? null)) {
            patch.labor_source = ln.labor_source ?? null;
          }
          await api.patch(`/api/invoices/${id}/lines/${ln.id}`, patch);
        }
      } else {
        const body = {
          description: desc,
          quantity: qty,
          unit_price: price,
          taxable: Boolean(ln.taxable),
        };
        if (category) body.category = category;
        // Carry the part linkage so the backend can claim the part as billed.
        // Without it the office adds a recorded part to the invoice, the money
        // is charged, and every unbilled-parts surface keeps reporting it as
        // missing -- a warning that doing the right thing cannot clear.
        if (ln.part_id) body.part_id = ln.part_id;
        if (ln.includes_labor) body.includes_labor = true;
        if (cost != null) body.cost = cost;
        if (marginOverrideDec != null) body.margin_pct_override = marginOverrideDec;
        // Labor provenance rides the CREATE only. Which lane priced a line is
        // decided when it is added and does not change by editing its text or
        // price, and InvoiceLinePatchIn deliberately does not accept these
        // (it forbids extras, so sending them on a PATCH would 422).
        Object.assign(body, laborFields);
        const lineResp = await api.post(`/api/invoices/${id}/lines`, body);
        // Record the server id on the edit row so a retry after a mid-save
        // failure PATCHes this line instead of POSTing a duplicate (the
        // catch below refetches, which puts it in originalById).
        const createdId = lineResp?.id || lineResp?.data?.id || null;
        if (createdId) ln.id = String(createdId);
        // PR1-billing-capture: surface the F-75 zero-price warning the
        // server attaches in warn-mode — it was emitted but never rendered.
        if (lineResp && lineResp.warning) {
          toast.add({
            severity: 'warn',
            summary: 'Review pricing',
            detail: `${lineResp.warning}: ${desc}`,
            life: 8000,
          });
        }
      }
    }

    // 2. Deletions — anything in original that didn't appear in the
    // post-edit kept set. The netting line is excluded twice over (the
    // editor can't remove it, and this guard) — deleting it would re-bill
    // an already-collected deposit, and the server 409s the attempt.
    for (const orig of original) {
      if (isDepositNettingLine(orig)) continue;
      const oid = String(orig.id ?? "");
      // Only real server line ids (UUIDs) are deletable — fetchInvoice's
      // offline placeholder rows carry ids like 1/2, and firing DELETEs
      // derived from fabricated data must never happen (audit 2026-07-24).
      if (oid.length < 32) continue;
      if (!keptIds.has(oid)) {
        await api.del(`/api/invoices/${id}/lines/${oid}`);
      }
    }

    // 3. Tax rate / dates / notes via PATCH on the invoice. The rate is
    // sent exactly as displayed, INCLUDING an explicit 0 — the server then
    // recomputes tax_amount to $0. (Sending null here instead used to
    // PRESERVE the previously computed tax dollars, so zeroing the field
    // looked like it silently reverted.)
    const ratePct = toNum(editTaxRatePct.value);
    const ratePayload = Number.isFinite(ratePct) ? ratePct / 100 : 0;
    await api.patch(`/api/invoices/${id}`, {
      tax_rate: ratePayload,
      invoice_date: editInvoiceDate.value || null,
      due_date: editDueDate.value || null,
      notes: editNotes.value || null,
      hide_line_prices: editHideLinePrices.value,
    });

    toast.add({ severity: "success", summary: "Saved", detail: "Invoice updated", life: 3000 });
    editing.value = false;
    await fetchInvoice();
  } catch (err) {
    toast.add({
      severity: "error",
      summary: "Save failed",
      detail: err?.message || "Could not save invoice changes",
      life: 5000,
    });
    // The save applies changes sequentially, so a mid-save failure leaves
    // some of them committed. Resync the underlying invoice (edit state is
    // untouched — the user keeps their draft and can retry) so the diff on
    // the next attempt runs against what the server actually has.
    // Deliberately NOT fetchInvoice(): its offline fallback swallows the
    // error and installs placeholder line items, and a retry diffed against
    // fabricated rows would duplicate every real line (audit 2026-07-24).
    // If this fetch fails too, the pre-save snapshot stays — safe, because
    // rows with a server id always PATCH and deletions require real UUIDs.
    try {
      const result = await api.get(`/api/invoices/${route.params.id}`, { suppressErrorToast: true });
      normalizeInvoice(result?.data || result || {});
    } catch {
      /* resync unavailable — keep the pre-save snapshot */
    }
  } finally {
    savingEdit.value = false;
  }
}

async function downloadPdf() {
  try {
    await openAuthedFile(`/api/invoices/${route.params.id}/pdf`);
  } catch (e) {
    console.error("invoice_pdf_failed", e);
    toast.add({
      severity: "error",
      summary: "PDF failed",
      detail: e?.message || "Could not open invoice PDF",
      life: 5000,
    });
  }
}

function confirmDelete() {
  confirmDestructive({
    message: `Delete ${invoice.value.invoice_number}? This cannot be undone.`,
    header: "Confirm Delete",
    accept: async () => {
      try {
        await api.del(`/api/invoices/${route.params.id}`);
        toast.add({ severity: "success", summary: "Deleted", detail: "Invoice deleted", life: 3000 });
        router.push("/billing");
      } catch (err) {
        toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to delete", life: 3000 });
      }
    },
  });
}

function openVoidDialog() {
  // Always start empty — a retained value would let a second void go through
  // on one click, which is exactly the friction this dialog exists to add.
  voidConfirmText.value = "";
  showVoidDialog.value = true;
}

async function voidInvoice() {
  if (!voidConfirmMatches.value) return;
  voiding.value = true;
  try {
    // suppressErrorToast: the server's 409s are the informative part
    // ("invoice has recorded payments — void or remove them first", "void
    // posts into a locked accounting period — …"). A generic toast on top of
    // them tells the operator nothing about what to do next.
    await api.post(
      `/api/invoices/${route.params.id}/void`,
      {},
      { suppressErrorToast: true },
    );
    showVoidDialog.value = false;
    // Re-fetch rather than assign the response. The API speaks `lines`; this
    // view speaks `line_items`, and `normalizeInvoice` is what translates —
    // assigning the raw payload left `line_items` undefined and the totals
    // computed threw "Cannot read properties of undefined (reading 'reduce')".
    // The void had already succeeded server-side, so the operator saw a broken
    // screen after a destructive action that actually worked. Caught in a real
    // browser; jsdom never rendered the crash.
    await fetchInvoice();
    toast.add({
      severity: "success",
      summary: "Voided",
      // Deliberately does NOT say "see the banner below". `fetchUnbilledJobParts`
      // returns early unless the invoice is a DRAFT, so on a freshly-voided
      // invoice the banner is empty by design — the parts really are released
      // (the audit event records the count), they are just billed from the
      // JOB, not from this dead invoice. Promising a panel that renders
      // nothing is how a true statement becomes a lie on screen.
      detail: `${invoice.value?.invoice_number || "Invoice"} is void. Any parts and change orders it claimed are billable again from the job.`,
      life: 5000,
    });
  } catch (err) {
    toast.add({
      severity: "error",
      summary: "Could not void",
      detail: err?.message || "Failed to void this invoice",
      life: 8000,
    });
  } finally {
    voiding.value = false;
  }
}

function pushToQuickbooks() {
  // Push IS destructive — it creates a QB invoice on the live realm and
  // can't be undone from the GDX side (operator has to void in QB if
  // wrong). 2026-05-12 audit-walk accident pushed a draft test invoice
  // to real QB. Confirm before firing.
  const totalLabel = invoice.value?.total != null
    ? currency(invoice.value.total)
    : "this invoice";
  confirmDestructive({
    message: `Push ${invoice.value?.invoice_number || "this invoice"} (${totalLabel}) to QuickBooks? A QB invoice will be created on the live realm.`,
    header: "Push to QuickBooks",
    icon: "pi pi-cloud-upload",
    acceptClass: "p-button-primary",
    acceptLabel: "Push to QB",
    rejectLabel: "Cancel",
    accept: () => doPushToQuickbooks(),
  });
}

async function doPushToQuickbooks() {
  pushingToQb.value = true;
  try {
    const id = route.params.id;
    const result = await api.post(`/api/qb/push/invoice/${id}`);
    const qbId = result?.qb_invoice_id || result?.qb_id;
    toast.add({
      severity: "success",
      summary: "Pushed to QuickBooks",
      detail: qbId ? `QuickBooks invoice ${qbId}` : "Invoice synced",
      life: 3000,
    });
  } catch (err) {
    toast.add({
      severity: "error",
      summary: "Push failed",
      detail: err?.message || "Could not push invoice to QuickBooks",
      life: 4000,
    });
  } finally {
    pushingToQb.value = false;
  }
}

async function loadQbStatus() {
  // Hide the Push to QB button when no QB connection is configured.
  // Falls back to the dashboard endpoint, then status. Either failing
  // means no QB integration on this tenant — leave button hidden.
  try {
    const dash = await api.get("/api/qb/dashboard").catch(() => api.get("/api/qb/status"));
    qbConnected.value = !!dash?.connected;
  } catch {
    qbConnected.value = false;
  }
}

async function loadTaxRate() {
  try {
    const cfg = await api.get("/api/tax/config");
    if (cfg && typeof cfg.default_rate === "number") {
      taxRate.value = cfg.default_rate;
    }
  } catch {
    // tax module may not be wired on this tenant yet — leave default
  }
}

onMounted(() => {
  // ?compose=1 — the Billing list's per-row Send lands here so every send
  // goes through the composer (preview + explicit click) instead of the
  // old fire-and-forget POST /send. Strip the flag so refresh/back doesn't
  // reopen the dialog, and gate on the loaded status so a hand-typed URL
  // can't open the composer on a void invoice (mirrors the button — paid
  // composes as a receipt since 2026-08-17).
  const autoCompose = !!route.query?.compose;
  if (autoCompose) {
    const { compose, ...rest } = route.query;
    router.replace({ query: rest })?.catch?.(() => {});
  }
  Promise.resolve(fetchInvoice()).then(() => {
    const st = String(invoice.value.status || "").toLowerCase();
    if (autoCompose && st !== "void") sendInvoice();
  });
  loadTaxRate();
  loadQbStatus();
  loadGlPosting();
});
</script>

<style scoped>
.overpaid-banner {
  border-color: var(--p-amber-400, #fbbf24);
  background: color-mix(in srgb, var(--p-amber-400, #fbbf24) 12%, transparent);
}
.superseded-banner {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.65rem 0.85rem;
  margin: 0.75rem 0;
  border-radius: 6px;
  border: 1px solid var(--color-info-border);
  background: var(--color-info-bg);
  color: var(--color-info-500);
  font-size: 0.9rem;
}

.detail-header {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.5rem;
}
.detail-header h2 {
  margin: 0.25rem 0;
}
.customer-name {
  color: var(--p-text-muted-color, #6b7280);
  margin: 0;
}
.header-meta {
  text-align: right;
}
.header-meta p {
  margin: 0.2rem 0;
  font-size: 0.875rem;
}

.mb-1 {
  margin-bottom: 1rem;
}

.totals-section {
  max-width: 360px;
  margin-left: auto;
  margin-top: 1rem;
}
.total-row {
  display: flex;
  justify-content: space-between;
  padding: 0.3rem 0;
  font-size: 0.925rem;
}
.total-row.grand {
  font-size: 1.1rem;
  border-top: 2px solid var(--p-content-border-color, #ddd);
  padding-top: 0.5rem;
  margin-top: 0.25rem;
}
.total-row.paid {
  color: var(--p-green-500, #22c55e);
}
.total-row.balance {
  color: var(--p-red-500, #ef4444);
  font-weight: 700;
}

.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin: 1rem 0;
}

.notes-section {
  margin-top: 1rem;
}
.notes-section p {
  white-space: pre-wrap;
}

/* Job-photo picker — checked photos print on the invoice PDF. */
.photo-pick-hint {
  color: var(--p-text-muted-color);
  font-size: 0.85rem;
  margin: 0.25rem 0 0.5rem;
}
.photo-pick-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
}
.photo-pick {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  width: 140px;
  padding: 0.4rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
  cursor: pointer;
}
.photo-pick.selected {
  border-color: var(--p-primary-color);
}
.photo-pick.locked {
  cursor: default;
  opacity: 0.75;
}
.photo-pick-thumb {
  width: 100%;
  height: 96px;
  object-fit: cover;
  border-radius: 4px;
}
.photo-pick-meta {
  font-size: 0.75rem;
  color: var(--p-text-muted-color);
  word-break: break-word;
}

.form-grid-single {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.form-field label {
  font-size: 0.8rem;
  font-weight: 600;
  text-transform: uppercase;
  color: var(--p-text-muted-color, #6b7280);
}
.form-field :deep(.p-dropdown),
.form-field :deep(.p-inputtext),
.form-field :deep(.p-inputnumber) {
  width: 100%;
}

@media (max-width: 900px) {
  .detail-header {
    flex-direction: column;
  }
  .header-meta {
    text-align: left;
  }
  .totals-section {
    max-width: 100%;
  }
}
.link { color: var(--p-primary-color); text-decoration: none; }
.link:hover { text-decoration: underline; }
.form-hint { color: var(--text-muted, #94a3b8); font-size: 0.8rem; margin-top: 0.25rem; }
.overpaid { color: #f59e0b; }

.lines-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 1rem;
  margin-bottom: 0.5rem;
}
.lines-header h3 {
  margin: 0;
}
.lines-header-actions {
  display: flex;
  gap: 0.5rem;
}
.edit-meta-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(160px, 1fr));
  gap: 0.75rem 1rem;
  margin-top: 0.75rem;
  padding: 0.75rem;
  background: var(--p-content-hover-background);
  border-radius: 0.5rem;
}
.edit-field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.edit-field label {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--p-text-color-secondary, #6c757d);
}
.edit-field .hint {
  color: var(--p-text-color-secondary, #6c757d);
  font-size: 0.75rem;
}
.composer-loading { padding: 2rem; text-align: center; color: #6b7280; }
.composer-preview {
  width: 100%;
  height: 420px;
  border: 1px solid var(--p-surface-300, #d1d5db);
  border-radius: 6px;
  /* No background here — the email document inside the iframe paints its
     own; a hardcoded light surface fails the dark-mode contrast gate. */
}
.composer-form { display: flex; flex-direction: column; gap: 0.75rem; }
.composer-form .form-field { display: flex; flex-direction: column; gap: 0.25rem; }
.composer-attachments { display: flex; flex-direction: column; gap: 0.4rem; }
.composer-att-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0.6rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 4px;
  cursor: pointer;
}
.composer-att-row span { flex: 1; word-break: break-word; }
.muted { color: var(--p-text-muted-color, #6b7280); font-size: 0.85em; }

.bill-to-card {
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 6px;
  padding: 0.75rem 1rem;
  margin: 0.5rem 0 1rem;
  background: var(--p-content-background, #fff);
}
.bill-to-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.5rem;
}
.bill-to-header h3 {
  margin: 0;
  font-size: 0.95rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--p-text-muted-color, #6b7280);
}
.bill-to-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0.4rem 1rem;
}
.bill-to-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.95rem;
}
.bill-to-row i { color: var(--p-text-muted-color, #6b7280); width: 1rem; }
.bill-to-row a { color: var(--p-primary-color, #3b82f6); text-decoration: none; }
.bill-to-row a:hover { text-decoration: underline; }
.bill-to-row .add-link { font-style: italic; }
/* Theme tokens only — this sits above the fold on a money screen and has to
   stay readable in dark mode. Walked in a real browser in both themes. */
.unbilled-parts-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
  margin-bottom: 0.75rem;
  padding: 0.6rem 0.9rem;
  border-left: 3px solid var(--p-orange-500, #f97316);
  background: var(--p-content-hover-background, rgba(127, 127, 127, 0.08));
  border-radius: 4px;
}
.unbilled-parts-list {
  display: block;
  color: var(--p-text-muted-color, #6b7280);
  font-size: 0.9rem;
}

/* Void dialog. Theme tokens only — a pale fixed background here would go
   white-on-white in dark mode, which is how the bank-feed nudge broke. */
.void-dialog-body { display: flex; flex-direction: column; gap: 0.85rem; }
.void-lead { margin: 0; }
.void-consequences {
  margin: 0;
  padding: 0.65rem 0.9rem 0.65rem 2rem;
  border-left: 3px solid var(--p-red-500, #ef4444);
  background: var(--p-content-hover-background, rgba(127, 127, 127, 0.08));
  border-radius: 4px;
  color: var(--p-text-color, inherit);
  font-size: 0.9rem;
}
.void-consequences li + li { margin-top: 0.3rem; }
.void-type-label {
  font-size: 0.88rem;
  color: var(--p-text-muted-color, #6b7280);
}
.void-type-label code {
  color: var(--p-text-color, inherit);
  background: var(--p-content-background, transparent);
  border: 1px solid var(--p-content-border-color, rgba(127, 127, 127, 0.3));
  border-radius: 3px;
  padding: 0.05rem 0.3rem;
}
.void-type-input { width: 100%; }
.void-blocked {
  padding: 0.6rem 0.9rem;
  border-left: 3px solid var(--p-orange-500, #f97316);
  background: var(--p-content-hover-background, rgba(127, 127, 127, 0.08));
  border-radius: 4px;
  color: var(--p-text-color, inherit);
  font-size: 0.9rem;
}
</style>
