<template>
    <section class="vendor-statements-view view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">Vendor Statements</h1>
        </template>
        <template #end>
          <Button
            label="Refresh"
            icon="pi pi-refresh"
            severity="secondary"
            :disabled="loading"
            @click="fetchItems"
          />
          <FileUpload
            mode="basic"
            name="file"
            accept="application/pdf"
            :auto="true"
            :customUpload="true"
            chooseLabel="Upload Midwest PDF"
            chooseIcon="pi pi-upload"
            data-testid="vendor-statement-upload"
            @uploader="onUpload"
          />
        </template>
      </Toolbar>

      <div v-if="error" class="error-banner">{{ error }}</div>
      <div v-if="duplicate" class="warn-banner">
        Duplicate document — already uploaded as
        <span class="mono">{{ duplicate.original_name || duplicate.existing_document_id }}</span>.
      </div>

      <div v-if="loading" class="spinner-wrap">
        <ProgressSpinner />
      </div>

      <!-- Current position. A statement is a SNAPSHOT of open items, so an
           unpaid invoice reappears until it clears — summing statements
           double-counts. The latest one is what's owed. -->
      <section v-if="!loading && accounts.length" class="accounts" data-testid="vendor-accounts">
        <div v-for="a in accounts" :key="a.vendor_name" class="account-card">
          <div class="account-head">
            <div>
              <h2 class="account-vendor">{{ a.vendor_name }}</h2>
              <p class="account-asof">
                <span v-if="a.vendor_code" class="mono">{{ a.vendor_code }}</span>
                <span v-if="a.vendor_code"> · </span>
                As of the {{ formatDate(a.as_of) }} statement
                <span v-if="a.statement_count > 1"> · {{ a.statement_count }} on file</span>
              </p>
            </div>
            <div class="account-balance">
              <span class="balance-amount">{{ formatCurrency(a.open_balance) }}</span>
              <span class="balance-label">
                open on {{ a.open_line_count }} invoice{{ a.open_line_count === 1 ? '' : 's' }}
              </span>
            </div>
          </div>

          <div class="aging-row">
            <span v-for="row in agingRows(a)" :key="row.bucket" class="aging-chip">
              <Tag :value="row.label" :severity="agingSeverity(row.bucket)" />
              <span class="aging-amount">{{ formatCurrency(row.amount) }}</span>
            </span>
          </div>

          <p v-if="a.days_oldest_open != null" class="account-note">
            Oldest unpaid charge dates {{ formatDate(a.oldest_line_date) }} —
            <strong>{{ a.days_oldest_open }} days</strong> ago.
          </p>

          <p v-if="a.change" class="account-change" data-testid="account-change">
            Since the {{ formatDate(a.change.previous_statement_date) }} statement:
            <template v-if="a.change.new_invoice_count">
              <strong>{{ a.change.new_invoice_count }}</strong> new
              ({{ formatCurrency(a.change.new_invoice_total) }})
            </template>
            <template v-if="a.change.new_invoice_count && a.change.implied_payment_total > 0"> · </template>
            <template v-if="a.change.implied_payment_total > 0">
              <strong>{{ a.change.cleared_count }}</strong> paid off, and
              <strong>{{ formatCurrency(a.change.implied_payment_total) }}</strong>
              appears to have been paid
              <span
                class="derived-flag"
                v-tooltip="'Worked out by comparing this statement to the previous one, not from a recorded payment. A credit or return looks the same, and two payments between statements show as one.'"
              >(derived)</span>
            </template>
            <template v-if="!a.change.new_invoice_count && !(a.change.implied_payment_total > 0)">
              nothing moved.
            </template>
          </p>

          <!-- Committed spend: ordered, not yet billed. Shown next to the open
               balance but never added to it — a quote is not a debt. -->
          <p
            v-if="onOrderFor(a) && onOrderFor(a).awaiting_bill_count"
            class="account-onorder"
            data-testid="account-on-order"
          >
            Plus <strong>{{ formatCurrency(onOrderFor(a).awaiting_bill_total) }}</strong>
            on order across
            <strong>{{ onOrderFor(a).awaiting_bill_count }}</strong>
            order{{ onOrderFor(a).awaiting_bill_count === 1 ? '' : 's' }} the supplier
            hasn't billed yet — not included in the balance above.
          </p>

          <Button
            v-if="onOrderFor(a) && onOrderFor(a).items.length"
            :label="expandedOrders[a.vendor_name] ? 'Hide orders' : `Show ${onOrderFor(a).items.length} orders`"
            :icon="expandedOrders[a.vendor_name] ? 'pi pi-chevron-up' : 'pi pi-shopping-cart'"
            text
            size="small"
            :data-testid="`toggle-orders-${a.vendor_name}`"
            @click="toggleOrders(a.vendor_name)"
          />

          <p
            v-if="expandedOrders[a.vendor_name] && onOrderFor(a)?.finished_truncated"
            class="text-xs hint-text truncation-note"
            data-testid="on-order-truncated"
          >
            Showing recent orders only — {{ onOrderFor(a).finished_truncated }}
            older completed order(s) not listed.
          </p>

          <DataTable
            v-if="expandedOrders[a.vendor_name] && onOrderFor(a)"
            :value="onOrderFor(a).items"
            stripedRows
            responsiveLayout="scroll"
            class="open-items"
            data-testid="on-order-table"
          >
            <Column header="Order #" style="width: 120px">
              <template #body="{ data }"><span class="mono">{{ data.order_number }}</span></template>
            </Column>
            <Column header="Ordered" style="width: 110px">
              <template #body="{ data }">{{ formatDate(data.order_date) }}</template>
            </Column>
            <Column header="Ship to" style="width: 160px">
              <template #body="{ data }">{{ data.ship_to || '—' }}</template>
            </Column>
            <Column header="Status" style="width: 130px">
              <template #body="{ data }">
                <Tag :value="statusLabel(data.status)" :severity="statusSeverity2(data.status)" />
              </template>
            </Column>
            <Column header="Estimated" style="width: 120px">
              <template #body="{ data }">{{ formatCurrency(data.estimated_total) }}</template>
            </Column>
            <Column header="Billed" style="width: 120px">
              <template #body="{ data }">
                <span v-if="data.billed_total != null">{{ formatCurrency(data.billed_total) }}</span>
                <span v-else class="text-muted">—</span>
              </template>
            </Column>
            <Column header="Variance" style="width: 120px">
              <template #body="{ data }">
                <span v-if="data.variance == null" class="text-muted">—</span>
                <span v-else-if="Number(data.variance) === 0" class="text-muted">none</span>
                <span v-else :class="Number(data.variance) > 0 ? 'variance-up' : 'variance-down'">
                  {{ Number(data.variance) > 0 ? '+' : '' }}{{ formatCurrency(data.variance) }}
                </span>
              </template>
            </Column>
            <Column header="Job" style="width: 230px">
              <template #body="{ data }">
                <span v-if="data.matched_job_id" class="job-linked">
                  <i class="pi pi-check-circle" aria-hidden="true" /> Filed to job
                </span>
                <template v-else>
                  <Button
                    v-if="!jobSuggestions[data.order_number]"
                    label="Find job"
                    icon="pi pi-search"
                    text
                    size="small"
                    :loading="suggestLoading[data.order_number]"
                    :data-testid="`suggest-${data.order_number}`"
                    @click="loadJobSuggestions(data)"
                  />
                  <!-- "matched a customer with no job" and "the reference is
                       junk" are different answers; showing both as "no match"
                       hid six real orders whose customer scored up to 0.89. -->
                  <div
                    v-else-if="!jobSuggestions[data.order_number].suggestions.length
                               && jobSuggestions[data.order_number].customers_without_jobs.length"
                    class="no-job-hint"
                    :data-testid="`no-job-${data.order_number}`"
                  >
                    Matched
                    <strong>{{ jobSuggestions[data.order_number].customers_without_jobs[0].customer_name }}</strong>,
                    who has no job on file. Create the job, then file this order to it.
                  </div>
                  <div
                    v-else-if="!jobSuggestions[data.order_number].suggestions.length"
                    class="text-muted small"
                  >
                    No match — the reference doesn't resemble a customer or a job.
                  </div>
                  <div v-else class="suggestions">
                    <div
                      v-for="sug in jobSuggestions[data.order_number].suggestions.slice(0, 2)"
                      :key="sug.job_id"
                      class="suggestion"
                    >
                      <div class="sug-name">
                        {{ sug.job_title || sug.customer_name }}
                        <span v-if="sug.job_number" class="mono small">{{ sug.job_number }}</span>
                      </div>
                      <div class="sug-why">{{ sug.reason }}</div>
                      <Button
                        label="File here"
                        icon="pi pi-paperclip"
                        size="small"
                        :loading="confirmingOrder === data.order_number"
                        :data-testid="`confirm-${data.order_number}`"
                        @click="confirmJob(data, sug)"
                      />
                    </div>
                  </div>
                </template>
              </template>
            </Column>
            <Column header="Ordered items">
              <template #body="{ data }">
                <div v-for="ln in data.lines" :key="ln.line_no" class="order-line">
                  <span class="qty">{{ ln.quantity }}×</span>
                  <span class="spec">{{ ln.notes || ln.description }}</span>
                </div>
              </template>
            </Column>
          </DataTable>

          <Button
            :label="expandedVendors[a.vendor_name] ? 'Hide open invoices' : `Show ${a.open_line_count} open invoices`"
            :icon="expandedVendors[a.vendor_name] ? 'pi pi-chevron-up' : 'pi pi-chevron-down'"
            text
            size="small"
            :data-testid="`toggle-open-${a.vendor_name}`"
            @click="toggleVendor(a.vendor_name)"
          />

          <DataTable
            v-if="expandedVendors[a.vendor_name]"
            :value="openLines(a)"
            stripedRows
            responsiveLayout="scroll"
            class="open-items"
            data-testid="open-items-table"
          >
            <Column header="Invoice" style="width: 120px">
              <template #body="{ data }">
                <span class="mono">{{ data.invoice_no }}</span>
              </template>
            </Column>
            <Column header="Dated" style="width: 110px">
              <template #body="{ data }">{{ formatDate(data.line_date) }}</template>
            </Column>
            <Column header="Age" style="width: 110px">
              <template #body="{ data }">
                <Tag :value="data.aging_bucket" :severity="agingSeverity(data.aging_bucket)" />
              </template>
            </Column>
            <Column header="Original" style="width: 120px">
              <template #body="{ data }">{{ formatCurrency(data.amount) }}</template>
            </Column>
            <Column header="Paid" style="width: 120px">
              <template #body="{ data }">
                <span :class="data.paid > 0 ? 'paid-some' : 'text-muted'">
                  {{ formatCurrency(data.paid) }}
                </span>
              </template>
            </Column>
            <Column header="Still open" style="width: 130px">
              <template #body="{ data }"><strong>{{ formatCurrency(data.balance) }}</strong></template>
            </Column>
            <Column header="Job / PO" style="width: 160px">
              <template #body="{ data }">
                <span class="mono small">{{ data.vendor_job_no }}</span>
                <span v-if="data.po_ref" class="po-ref"> · {{ data.po_ref }}</span>
              </template>
            </Column>
            <Column header="On statements" style="width: 130px">
              <template #body="{ data }">
                <span v-if="data.statements_seen > 1" class="carried">
                  {{ data.statements_seen }}× since {{ formatDate(data.first_seen_on) }}
                </span>
                <span v-else class="text-muted">first time</span>
              </template>
            </Column>
          </DataTable>
        </div>
      </section>

      <h2 v-if="!loading" class="section-heading">Statement history</h2>

      <DataTable
        v-if="!loading"
        :value="items"
        stripedRows
        responsiveLayout="scroll"
        :rowClass="() => 'row-clickable'"
        data-testid="vendor-statements-table"
        @row-click="(event) => openDetail(event.data)"
      >
        <template #empty>
          <div class="empty-message">
            No vendor statements yet. They arrive automatically from allowlisted
            supplier senders — or upload a Midwest PDF to begin.
          </div>
        </template>

        <Column header="Uploaded" style="width: 180px">
          <template #body="{ data }">{{ formatDateTime(data.created_at) }}</template>
        </Column>
        <Column field="vendor_name" header="Vendor" />
        <Column header="Statement Date" style="width: 160px">
          <template #body="{ data }">{{ formatDate(data.statement_date) }}</template>
        </Column>
        <Column header="Lines" style="width: 90px">
          <template #body="{ data }">{{ data.line_count }}</template>
        </Column>
        <Column header="Total" style="width: 140px">
          <template #body="{ data }">{{ formatCurrency(data.raw_total) }}</template>
        </Column>
        <Column header="Status" style="width: 120px">
          <template #body="{ data }">
            <Tag :value="data.status" :severity="statusSeverity(data.status)" />
          </template>
        </Column>
        <Column header="Source" style="width: 130px">
          <template #body="{ data }">
            <span class="source-cell">
              <i :class="data.source === 'email' ? 'pi pi-envelope' : 'pi pi-upload'" aria-hidden="true" />
              {{ data.source === 'email' ? 'Email' : 'Upload' }}
            </span>
          </template>
        </Column>
        <Column header="" style="width: 80px">
          <template #body="{ data }">
            <Button
              v-tooltip="'View details'"
              aria-label="View details"
              icon="pi pi-arrow-right"
              text
              rounded
              severity="secondary"
              @click.stop="openDetail(data)"
            />
          </template>
        </Column>
      </DataTable>
    </section>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import { useAuthStore } from '../stores/auth'
import { formatDate } from '../utils/dates'
import { formatDateTime, formatMoney as formatCurrency } from '../composables/useFormatters'

import Toolbar from 'primevue/toolbar'
import DataTable from 'primevue/datatable'
import Column from 'primevue/column'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import FileUpload from 'primevue/fileupload'
import ProgressSpinner from 'primevue/progressspinner'

const api = useApi()
const toast = useToast()
const auth = useAuthStore()
const router = useRouter()

const items = ref([])
const accounts = ref([])
const onOrder = ref([])
const loading = ref(false)
const error = ref(null)
const duplicate = ref(null)
const expandedVendors = ref({})

// Aging oldest-first — the order money gets chased in.
const AGING_ORDER = ['120+', '90-119', '60-89', '30-59', '0-29', 'current', 'retainage']
const AGING_LABEL = {
  '120+': '120+ days', '90-119': '90-119', '60-89': '60-89',
  '30-59': '30-59', '0-29': '0-29 days', current: 'Current', retainage: 'Retainage',
}

function agingRows(account) {
  return AGING_ORDER
    .filter((b) => account.aging?.[b] != null)
    .map((b) => ({ bucket: b, label: AGING_LABEL[b] || b, amount: account.aging[b] }))
}

// Anything past 60 days is the part worth looking at.
function agingSeverity(bucket) {
  if (bucket === '120+' || bucket === '90-119') return 'danger'
  if (bucket === '60-89') return 'warn'
  return 'secondary'
}

// The table under "open invoices" must contain exactly the invoices the count
// promises. A line still listed at a nil balance is settled, not open — it
// stays visible on the statement detail page.
function openLines(account) {
  return (account.lines || []).filter((l) => Number(l.balance) > 0)
}

// Committed spend for an account. Deliberately NOT added to the open balance:
// an order is a supplier quote that may still change, an open invoice is a
// debt. Summing them would overstate what's due.
function onOrderFor(account) {
  const all = onOrder.value || []
  // Exact account match first. The fallback matters: statements read
  // `CUSTOMER CODE:` while order confirmations read `CUSTOMER NO:`, and if the
  // supplier ever prints those differently an exact-only match would render
  // nothing at all, silently. Degrading to the vendor name shows the orders.
  return all.find((o) => o.vendor_name === account.vendor_name
                      && o.vendor_code === account.vendor_code)
      || all.find((o) => o.vendor_name === account.vendor_name)
}

function statusLabel(status) {
  if (status === 'awaiting_bill') return 'Not billed yet'
  if (status === 'billed') return 'Billed'
  if (status === 'settled') return 'Settled'
  return 'On statement'
}

function statusSeverity2(status) {
  if (status === 'awaiting_bill') return 'warn'
  if (status === 'billed') return 'info'
  if (status === 'settled') return 'success'
  return 'secondary'
}

const expandedOrders = ref({})
// Suggestions are fetched per order on demand — they scan every customer name,
// so loading them for rows nobody expands would be waste.
const jobSuggestions = ref({})
const suggestLoading = ref({})
const confirmingOrder = ref(null)

async function loadJobSuggestions(order) {
  if (order.matched_job_id || jobSuggestions.value[order.order_number]) return
  suggestLoading.value = { ...suggestLoading.value, [order.order_number]: true }
  try {
jobSuggestions.value = {
      ...jobSuggestions.value,
      [order.order_number]: await api.get(
        `/api/vendor-statements/orders/${order.order_id}/job-suggestions`,
      ) || { suggestions: [], customers_without_jobs: [] },
    }
  } catch {
    jobSuggestions.value = {
      ...jobSuggestions.value,
      [order.order_number]: { suggestions: [], customers_without_jobs: [] },
    }
  } finally {
    suggestLoading.value = { ...suggestLoading.value, [order.order_number]: false }
  }
}

async function confirmJob(order, suggestion) {
  confirmingOrder.value = order.order_number
  try {
    const res = await api.post(
      `/api/vendor-statements/orders/${order.order_id}/confirm-job`,
      { job_id: suggestion.job_id },
    )
    toast.add({
      severity: 'success',
      summary: 'Filed to job',
      detail: res.newly_filed_count
        ? `${res.newly_filed_count} document(s) filed on the job.`
        : 'Job linked. The paperwork was already filed elsewhere and was left alone.',
      life: 5000,
    })
    await fetchItems()
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Could not file', detail: err?.message || 'Unknown error', life: 6000 })
  } finally {
    confirmingOrder.value = null
  }
}
function toggleOrders(name) {
  expandedOrders.value = { ...expandedOrders.value, [name]: !expandedOrders.value[name] }
}

function toggleVendor(name) {
  expandedVendors.value = { ...expandedVendors.value, [name]: !expandedVendors.value[name] }
}

function statusSeverity(s) {
  if (!s) return 'secondary'
  const k = String(s).toLowerCase()
  if (k === 'reconciled') return 'success'
  if (k === 'review') return 'warn'
  if (k === 'parsed') return 'info'
  return 'secondary'
}

const fetchItems = async () => {
  loading.value = true
  error.value = null
  try {
    const [statements, accts, orders] = await Promise.all([
      api.get('/api/vendor-statements'),
      api.get('/api/vendor-statements/accounts'),
      api.get('/api/vendor-statements/on-order'),
    ])
    items.value = statements || []
    accounts.value = accts || []
    onOrder.value = orders || []
  } catch (err) {
    error.value = err.message || 'Failed to load'
  } finally {
    loading.value = false
  }
}

const onUpload = async (event) => {
  duplicate.value = null
  error.value = null
  const file = event.files?.[0]
  if (!file) return
  loading.value = true
  try {
    const fd = new FormData()
    fd.append('file', file)
    fd.append('vendor', 'midwest')

    const headers = {}
    if (auth.accessToken) headers.Authorization = `Bearer ${auth.accessToken}`

    const resp = await fetch('/api/vendor-statements/upload', {
      method: 'POST',
      headers,
      credentials: 'include',
      body: fd,
    })

    if (resp.status === 409) {
      const body = await resp.json().catch(() => ({}))
      duplicate.value = body?.detail || { detail: 'duplicate' }
      await fetchItems()
      return
    }

    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}))
      throw new Error(typeof body.detail === 'string' ? body.detail : `upload failed (${resp.status})`)
    }

    const stmt = await resp.json()
    await fetchItems()
    if (stmt?.id) router.push(`/vendor-statements/${stmt.id}`)
  } catch (err) {
    error.value = err.message || 'Upload failed'
  } finally {
    loading.value = false
  }
}

const openDetail = (row) => {
  router.push(`/vendor-statements/${row.id}`)
}

onMounted(fetchItems)
</script>

<style scoped>
.vendor-statements-view {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.view-heading {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 600;
}
.row-clickable { cursor: pointer; }
.error-banner {
  background: var(--p-red-50, #fef2f2);
  color: var(--p-red-700, #b91c1c);
  border: 1px solid var(--p-red-200, #fecaca);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
}
.warn-banner {
  background: var(--p-yellow-50, #fffbeb);
  color: var(--p-yellow-800, #92400e);
  border: 1px solid var(--p-yellow-200, #fde68a);
  border-radius: 6px;
  padding: 0.5rem 0.75rem;
}
.mono { font-family: var(--font-mono, ui-monospace, monospace); }
.accounts { display: flex; flex-direction: column; gap: 1rem; }
.account-card {
  border: 1px solid var(--p-content-border-color);
  border-radius: 8px;
  padding: 1rem;
  /* Theme tokens only — this has to stay legible on the dark surface. */
  background: var(--p-content-hover-background);
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}
.account-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
  flex-wrap: wrap;
}
.account-vendor { margin: 0; font-size: 1.05rem; font-weight: 600; }
.account-asof { margin: 0.15rem 0 0; font-size: 0.8rem; color: var(--p-text-muted-color); }
.account-balance { display: flex; flex-direction: column; align-items: flex-end; }
.balance-amount { font-size: 1.6rem; font-weight: 700; line-height: 1.1; }
.balance-label { font-size: 0.78rem; color: var(--p-text-muted-color); }
.aging-row { display: flex; flex-wrap: wrap; gap: 0.75rem; align-items: center; }
.aging-chip { display: inline-flex; align-items: center; gap: 0.35rem; }
.aging-amount { font-size: 0.85rem; font-variant-numeric: tabular-nums; }
.account-note, .account-change { margin: 0; font-size: 0.85rem; color: var(--p-text-color); }
.derived-flag {
  color: var(--p-text-muted-color);
  font-style: italic;
  cursor: help;
  border-bottom: 1px dotted var(--p-text-muted-color);
}
.open-items { margin-top: 0.5rem; }
.account-onorder {
  margin: 0;
  font-size: 0.85rem;
  color: var(--p-text-color);
}
.variance-up { color: var(--p-orange-500, #f59e0b); font-weight: 600; }
.variance-down { color: var(--p-green-500, #22c55e); font-weight: 600; }
.truncation-note { margin: 0.25rem 0 0; }
.order-line { font-size: 0.8rem; line-height: 1.4; }
.job-linked { color: var(--p-green-500, #22c55e); font-size: 0.85rem; }
.suggestions { display: flex; flex-direction: column; gap: 0.5rem; }
.suggestion { display: flex; flex-direction: column; gap: 0.2rem; align-items: flex-start; }
.sug-name { font-size: 0.85rem; color: var(--p-text-color); }
.sug-why { font-size: 0.72rem; color: var(--p-text-muted-color); line-height: 1.3; }
.no-job-hint { font-size: 0.8rem; color: var(--p-orange-500, #f59e0b); line-height: 1.35; }
.order-line .qty { color: var(--p-text-muted-color); margin-right: 0.35rem; }
.order-line .spec { color: var(--p-text-color); }
.paid-some { color: var(--p-green-500, #22c55e); }
.carried { font-size: 0.8rem; color: var(--p-orange-500, #f59e0b); }
.po-ref, .small { font-size: 0.8rem; color: var(--p-text-muted-color); }
.section-heading { margin: 0.5rem 0 0; font-size: 1rem; font-weight: 600; }
.source-cell {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  /* Theme token, no light-only literal — this cell has to stay legible on the
     dark surface too (see the 2026-07 dark-mode sweep). */
  color: var(--p-text-muted-color);
  white-space: nowrap;
}
.spinner-wrap { display: flex; justify-content: center; padding: 2rem; }
.empty-message {
  text-align: center;
  padding: 1.5rem;
  color: var(--p-text-muted-color);
}
</style>
