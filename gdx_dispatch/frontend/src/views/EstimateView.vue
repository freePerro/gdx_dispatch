<!--
  EstimateView — unified create + detail view. One form, two modes.
    /estimates/new      → empty form, only Save shown.
    /estimates/:id      → form pre-populated, full action bar (send / accept /
                          decline / convert / pdf / copy / print / save).
  Shipping v1 with wholesale line replace on save (delete + re-post all lines)
  for simplicity; per-line diff sync is a follow-up.
-->
<template>
    <section class="estimate-view view-card">
      <header class="page-header">
        <Button icon="pi pi-arrow-left" label="Back to Estimates" text size="small"
          data-testid="back-to-estimates" @click="$router.push('/estimates')" />
        <div class="title-row">
          <h2 class="page-title" data-testid="estimate-label">
            {{ isExisting ? (estimate.label || estimate.estimate_number || 'Estimate') : 'New Estimate' }}
          </h2>
          <Tag v-if="isExisting"
            :value="estimate.status"
            :severity="statusSeverity(estimate.status)"
            data-testid="estimate-status" />
          <Tag v-if="estimate.job_id" value="Converted to Job" severity="info"
            icon="pi pi-briefcase" data-testid="estimate-converted-tag" />
          <Button v-if="!isExisting"
            label="AI Quick Estimate" icon="pi pi-bolt" severity="help" size="small"
            @click="showAiDialog = true" data-testid="ai-quick-estimate-btn" />
        </div>
        <div v-if="isExisting" class="header-meta">
          <span class="customer-name" data-testid="estimate-customer">{{ estimate.customer_name }}</span>
          <span class="meta-sep">·</span>
          <span>Created: {{ formatDate(estimate.created_at) }}</span>
          <span class="meta-sep">·</span>
          <span>Expires: {{ formatDate(estimate.expires_at) }}</span>
        </div>
        <div v-if="estimate.job_id" class="converted-banner" data-testid="estimate-converted-banner">
          <i class="pi pi-briefcase" />
          <span>This estimate has been converted to a job.</span>
          <Button :label="linkedJobLabel" icon="pi pi-arrow-right" icon-pos="right"
            link size="small" data-testid="estimate-view-job-link"
            @click="$router.push(`/jobs/${estimate.job_id}`)" />
        </div>
      </header>

      <!-- AI Quick Estimate Dialog -->
      <Dialog v-model:visible="showAiDialog" header="AI Quick Estimate" :style="{ width: '520px' }" modal>
        <p class="text-sm mb-3">Describe the job and AI will auto-fill line items from the CHI catalog.</p>
        <Textarea v-model="aiDescription" rows="4" class="w-full"
          placeholder="e.g., 16x7 insulated steel door replacement with torsion springs and new opener"
          data-testid="ai-description-input" />
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showAiDialog = false" />
          <Button label="Generate Estimate" icon="pi pi-bolt" :loading="aiLoading"
            :disabled="!aiDescription" @click="runAiEstimate" data-testid="ai-generate-btn" />
        </template>
      </Dialog>

      <Card v-if="!loading">
        <template #content>
          <div class="form-grid">
            <!-- Customer & Title -->
            <div class="form-field">
              <label for="est-customer">Customer *</label>
              <Select id="est-customer" v-model="form.customer_id" :options="customerOptions"
                optionLabel="label" optionValue="value" placeholder="Select customer" filter
                showClear :disabled="form.new_customer || isExisting"
                class="w-full" data-testid="estimate-customer-dropdown" />
              <div v-if="!isExisting" class="toggle-row">
                <ToggleSwitch
                  v-model="form.new_customer"
                  @change="handleNewCustomerToggle"
                  data-testid="estimate-new-customer-toggle"
                />
                <span class="toggle-label">Create new customer instead</span>
              </div>
              <!-- Selected-customer contact panel — shows phone/email/address
                   pulled from the customer record. Read-only; click name to
                   open detail. -->
              <div v-if="selectedCustomer && !form.new_customer" class="customer-contact" data-testid="estimate-customer-contact">
                <div v-if="selectedCustomer.phone" class="contact-row">
                  <i class="pi pi-phone" /> {{ formatPhone(selectedCustomer.phone) }}
                </div>
                <div v-if="selectedCustomer.email" class="contact-row">
                  <i class="pi pi-envelope" /> {{ selectedCustomer.email }}
                </div>
                <div v-if="selectedCustomer.address" class="contact-row">
                  <i class="pi pi-map-marker" />
                  <span style="white-space: pre-line">{{ selectedCustomer.address }}</span>
                  <Button label="Use as jobsite" text size="small"
                    style="margin-left: auto"
                    data-testid="copy-customer-address-to-jobsite"
                    @click="form.jobsite_address = selectedCustomer.address" />
                </div>
              </div>
            </div>
            <div v-if="!isExisting && form.new_customer" class="form-field full-width new-client-section">
              <div class="form-row">
                <div class="form-field">
                  <label for="new-cust-name">Name *</label>
                  <InputText id="new-cust-name" v-model="form.new_cust_name"
                    placeholder="John Smith" class="w-full"
                    data-testid="estimate-new-cust-name-input" />
                </div>
                <div class="form-field">
                  <label for="new-cust-phone">Phone</label>
                  <PhoneInput id="new-cust-phone" v-model="form.new_cust_phone"
                    class="w-full"
                    data-testid="estimate-new-cust-phone-input" />
                </div>
              </div>
              <div class="form-row">
                <div class="form-field">
                  <label for="new-cust-email">Email</label>
                  <InputText id="new-cust-email" v-model="form.new_cust_email"
                    placeholder="john@example.com" class="w-full"
                    data-testid="estimate-new-cust-email-input" />
                </div>
                <div class="form-field">
                  <label for="new-cust-address">Address</label>
                  <Textarea id="new-cust-address" v-model="form.new_cust_address" rows="2"
                    placeholder="123 Main St, City, ST" class="w-full"
                    data-testid="estimate-new-cust-address-input" />
                </div>
              </div>
            </div>
            <div class="form-field">
              <label for="est-label">Job Name</label>
              <InputText id="est-label" v-model="form.label" class="w-full"
                placeholder="e.g. Front garage door replacement" data-testid="estimate-label-input" />
            </div>
            <div class="form-field">
              <label for="est-valid-until">Valid Until</label>
              <DatePicker id="est-valid-until" v-model="form.valid_until" dateFormat="yy-mm-dd"
                :showIcon="true" class="w-full" data-testid="estimate-valid-until" />
            </div>
            <div class="form-field">
              <label for="est-jobsite">Jobsite Address</label>
              <Textarea id="est-jobsite" v-model="form.jobsite_address" rows="2" class="w-full"
                placeholder="Address where the work will be performed (if different from billing address)"
                data-testid="estimate-jobsite-address" />
            </div>

            <!-- Work Description -->
            <div class="form-field full-width">
              <label for="est-description">Description of Work</label>
              <Textarea id="est-description" v-model="form.description" rows="3" class="w-full"
                placeholder="Describe the work to be done..."
                data-testid="estimate-description" />
            </div>

            <!-- Line Items -->
            <div class="form-field full-width">
              <label>Line Items</label>
              <div class="line-items-editor">
                <div class="line-item-header">
                  <span class="col-action"></span>
                  <span class="col-cat">Category</span>
                  <span class="col-desc">Description</span>
                  <span class="col-qty">Qty</span>
                  <span class="col-cost">Cost</span>
                  <span class="col-price">Unit Price</span>
                  <span class="col-margin">Margin</span>
                  <span class="col-total">Total</span>
                  <span class="col-action"></span>
                </div>
                <div v-for="(item, idx) in form.line_items" :key="idx" class="line-item-row">
                  <span class="col-action line-controls">
                    <span class="line-reorder">
                      <Button v-tooltip="'Move line up'" icon="pi pi-chevron-up" aria-label="Move line up" text size="small"
                        :disabled="idx === 0" @click="moveLine(idx, -1)"
                        :data-testid="`est-line-up-${idx}`" />
                      <Button v-tooltip="'Move line down'" icon="pi pi-chevron-down" aria-label="Move line down" text size="small"
                        :disabled="idx === form.line_items.length - 1" @click="moveLine(idx, 1)"
                        :data-testid="`est-line-down-${idx}`" />
                    </span>
                    <Button v-tooltip="'Delete line'" icon="pi pi-trash" aria-label="Delete line" severity="danger" text size="small"
                      @click="removeLineAt(idx)"
                      data-testid="est-line-delete" />
                  </span>
                  <Select v-model="item.category" :options="lineCategories" placeholder="Category"
                    class="col-cat" :data-testid="`est-line-cat-${idx}`"
                    @change="recomputeSell(item)" />
                  <InputText v-model="item.description" placeholder="Description"
                    class="col-desc" :data-testid="`est-line-desc-${idx}`" />
                  <InputNumber v-model="item.quantity" :min="1" :useGrouping="false"
                    class="col-qty" :data-testid="`est-line-qty-${idx}`" />
                  <InputNumber v-model="item.cost" mode="currency" currency="USD" locale="en-US"
                    :min="0" class="col-cost" :data-testid="`est-line-cost-${idx}`"
                    @update:modelValue="recomputeSell(item)" />
                  <InputNumber v-model="item.unit_price" mode="currency" currency="USD" locale="en-US"
                    :min="0" class="col-price" :data-testid="`est-line-price-${idx}`"
                    @update:modelValue="markPriceOverride(item)" />
                  <InputNumber
                    v-if="estimateFeatures.estimates_allow_line_margin_override"
                    v-model="item.margin_pct_override"
                    suffix="%" :min="0" :max="99" :maxFractionDigits="2"
                    placeholder="tier"
                    class="col-margin" :data-testid="`est-line-margin-${idx}`"
                    @update:modelValue="onMarginOverrideChange(item)" />
                  <span v-else class="col-margin line-margin-display">{{ marginDisplay(item) }}</span>
                  <span class="col-total line-total-display">{{ currency(toNum(item.quantity) * toNum(item.unit_price)) }}</span>
                  <Button v-tooltip="'Save this line to the catalog'" icon="pi pi-bookmark" aria-label="Save to catalog" text size="small" class="col-action"
                    :data-testid="`save-to-catalog-${idx}`"
                    @click="openSaveToCatalog(item)" />
                </div>
                <div class="line-item-buttons">
                  <Button label="Add Line" icon="pi pi-plus" text size="small"
                    data-testid="est-add-line-btn"
                    @click="form.line_items.push(defaultLineItem())" />
                  <Button label="Add from Catalog" icon="pi pi-book" text size="small" severity="info"
                    data-testid="est-add-catalog-btn"
                    @click="showCatalogPicker = true" />
                  <!-- PLUGIN INTEGRATION POINT (ADR-013) — DO NOT REMOVE. Shown
                       ONLY when an installed plugin declares an estimate_source
                       (e.g. the CHI pricing plugin); invisible in stock core. -->
                  <Button v-if="estimateSource && isExisting" :label="`Add ${estimateSource.label}`"
                    icon="pi pi-images" text size="small" severity="info"
                    data-testid="est-add-captured-btn"
                    @click="openCapturedPicker" />
                  <Button label="Add Labor" icon="pi pi-wrench" text size="small" severity="info"
                    data-testid="est-add-labor-btn"
                    @click="openLaborPicker" />
                  <Button label="AI Suggest" icon="pi pi-sparkles" text size="small" severity="help"
                    data-testid="est-ai-suggest-btn"
                    :loading="aiSuggesting"
                    @click="aiSuggestLines" />
                </div>
              </div>
            </div>

            <!-- Tax & Discount -->
            <div class="form-field">
              <label for="est-tax-rate">Tax Rate</label>
              <InputNumber id="est-tax-rate" v-model="form.tax_rate" suffix="%" :min="0" :max="100"
                :minFractionDigits="0" :maxFractionDigits="2" class="w-full"
                data-testid="estimate-tax-rate" />
            </div>
            <div class="form-field">
              <label for="est-discount">Discount</label>
              <InputNumber id="est-discount" v-model="form.discount" mode="currency" currency="USD"
                locale="en-US" :min="0" class="w-full" data-testid="estimate-discount" />
            </div>

            <!-- Notes -->
            <div class="form-field full-width">
              <label for="est-notes">Notes</label>
              <Textarea id="est-notes" v-model="form.notes" rows="3" class="w-full"
                data-testid="estimate-notes" />
            </div>

            <!-- Customer price display (total-only option) -->
            <div class="form-field full-width">
              <label for="est-hide-line-prices">Customer price display</label>
              <Select id="est-hide-line-prices" v-model="hideLinePricesChoice"
                :options="hideLinePricesOptions" optionLabel="label" optionValue="value"
                class="w-full" data-testid="estimate-hide-line-prices" />
              <small class="muted">
                Hides the per-line Unit Price / Line Total on the customer PDF, email, and
                install sheet (the subtotal, tax and total still show). The editor always
                shows line prices — this only changes what the customer sees.
              </small>
            </div>
          </div>

          <!-- Attachments (existing estimates only — needs an id) -->
          <template v-if="isExisting">
            <Divider />
            <div class="form-field full-width">
              <label>Attachments <small class="muted">(images, PDFs — 25MB max)</small></label>
              <div class="attachment-actions">
                <input
                  ref="attachmentInput"
                  type="file"
                  multiple
                  accept="image/*,application/pdf"
                  class="hidden-file-input"
                  data-testid="estimate-attachment-input"
                  @change="onAttachmentFiles"
                />
                <Button
                  label="Upload files"
                  icon="pi pi-paperclip"
                  size="small"
                  outlined
                  :loading="attachmentsUploading"
                  data-testid="estimate-attachment-pick"
                  @click="$refs.attachmentInput.click()"
                />
                <span v-if="attachmentsUploading" class="muted">Uploading…</span>
              </div>
              <div v-if="attachments.length" class="attachment-list" data-testid="estimate-attachment-list">
                <div v-for="att in attachments" :key="att.id" class="attachment-item">
                  <button v-if="isImageMime(att.content_type)"
                          type="button"
                          class="attachment-thumb"
                          @click="openAttachment(att)">
                    <img v-if="att._previewUrl" :src="att._previewUrl" :alt="att.original_name" />
                    <i v-else class="pi pi-image attachment-icon-small" />
                  </button>
                  <i v-else class="pi pi-file-pdf attachment-icon" @click="openAttachment(att)" />
                  <div class="attachment-meta">
                    <a href="#" class="attachment-name" @click.prevent="openAttachment(att)">{{ att.original_name }}</a>
                    <small class="muted">{{ formatBytes(att.file_size) }} · {{ att.uploaded_by || 'unknown' }}</small>
                  </div>
                  <Button
                    v-tooltip="'Delete'"
                    aria-label="Delete"
                    icon="pi pi-trash"
                    severity="danger"
                    text
                    size="small"
                    :data-testid="`estimate-attachment-delete-${att.id}`"
                    @click="deleteAttachment(att)"
                  />
                </div>
              </div>
              <div v-else class="muted">No attachments yet.</div>
            </div>
          </template>

          <!-- Summary -->
          <Divider />
          <div class="totals-and-profit">
            <div class="estimate-summary">
              <div class="summary-row">
                <span>Subtotal</span>
                <span class="summary-value" data-testid="estimate-subtotal">{{ currency(subtotal) }}</span>
              </div>
              <div class="summary-row" v-if="form.discount">
                <span>Discount</span>
                <span class="summary-value discount">-{{ currency(form.discount) }}</span>
              </div>
              <div class="summary-row" v-if="form.tax_rate && !tenantTaxLabor && laborSubtotal > 0">
                <span class="muted">Labor (not taxed)</span>
                <span class="summary-value muted" data-testid="estimate-labor-untaxed">{{ currency(laborSubtotal) }}</span>
              </div>
              <div class="summary-row" v-if="form.tax_rate">
                <span>
                  Tax ({{ form.tax_rate }}%)
                  <small v-if="!tenantTaxLabor && laborSubtotal > 0" class="muted">
                    on materials
                  </small>
                </span>
                <span class="summary-value" data-testid="estimate-tax">{{ currency(taxAmount) }}</span>
              </div>
              <Divider />
              <div class="summary-row total-row">
                <span>Total</span>
                <span class="summary-value total" data-testid="estimate-total">{{ currency(total) }}</span>
              </div>
            </div>
            <EstimateProfitPanel :lines="profitPanelLines" />
          </div>
        </template>
      </Card>

      <div v-if="loading" class="loading-spinner"><p>Loading estimate...</p></div>

      <!-- Shared catalog picker (one tab per real catalog) -->
      <CatalogPickerDialog v-model:visible="showCatalogPicker" @add="addFromCatalog" />

      <!-- PLUGIN INTEGRATION POINT (ADR-013) — DO NOT REMOVE. Captured-item picker
           for an installed estimate_source plugin (e.g. CHI pricing). Inert unless
           estimateSource is set. A folder explorer: pick a folder, multi-select
           doors, add each as a line (captured price → cost → margin engine) with
           its photo + full spec. -->
      <Dialog v-model:visible="capturedPickerVisible"
        :header="estimateSource ? `Add ${estimateSource.label}` : 'Add captured item'"
        modal :style="{ width: '700px' }" data-testid="captured-picker-dialog">
        <p v-if="capturedLoading">Loading…</p>
        <template v-else>
          <!-- Folder list -->
          <div v-if="capturedFolder === null">
            <p class="captured-hint">Pick a folder:</p>
            <ul class="captured-folders">
              <li v-for="f in capturedFolderList" :key="f.key"
                class="captured-folder" :data-testid="`captured-folder-${f.key}`"
                @click="openFolder(f.key)">
                <i class="pi pi-folder" />
                <span class="captured-folder__name">{{ f.label }}</span>
                <span class="captured-folder__count">{{ f.count }}</span>
                <i class="pi pi-chevron-right captured-folder__chev" />
              </li>
            </ul>
          </div>
          <!-- Doors in a folder (multi-select) -->
          <div v-else>
            <Button text size="small" icon="pi pi-arrow-left" label="Folders" @click="backToFolders" />
            <span class="captured-breadcrumb">{{ capturedFolderLabel }}</span>
            <DataTable :value="doorsInFolder" dataKey="id"
              v-model:selection="selectedDoors" selectionMode="multiple"
              stripedRows responsiveLayout="scroll"
              :paginator="doorsInFolder.length > 10" :rows="10"
              style="margin-top: 0.5rem" data-testid="captured-doors-table">
              <Column selectionMode="multiple" style="width: 3rem" />
              <Column field="qcd" header="Quote #" />
              <Column field="cart_name" header="Cart" />
              <Column header="Price" style="width: 110px">
                <template #body="{ data }">{{ currency(data.price) }}</template>
              </Column>
              <template #empty><span>No doors in this folder.</span></template>
            </DataTable>
          </div>
        </template>
        <template #footer>
          <Button label="Close" severity="secondary" @click="capturedPickerVisible = false" />
          <Button v-if="capturedFolder !== null"
            :label="`Add ${selectedDoors.length} door${selectedDoors.length === 1 ? '' : 's'}`"
            icon="pi pi-plus" :disabled="!selectedDoors.length || addingDoors" :loading="addingDoors"
            data-testid="captured-add-btn" @click="addSelectedDoors" />
        </template>
      </Dialog>

      <!-- Labor matrix picker (S97 slice 5) -->
      <Dialog v-model:visible="showLaborPicker" header="Add Labor from Matrix" modal
        :style="{ width: '760px' }" data-testid="labor-picker-dialog">
        <InputText v-model="laborSearch" placeholder="Search by description, service, size, or SKU…"
          class="w-full" data-testid="labor-search" style="margin-bottom: 1rem" />
        <div v-if="laborLoading" class="muted">Loading labor matrix…</div>
        <div v-else-if="!laborItems.length" class="muted">
          No labor rows configured. Add rows in <a href="/labor-matrix" target="_blank">Labor Matrix</a>.
        </div>
        <DataTable
      responsiveLayout="scroll" v-else :value="filteredLaborItems"
          :paginator="filteredLaborItems.length > 10" :rows="10"
          selectionMode="multiple" v-model:selection="selectedLaborItems"
          dataKey="id" stripedRows data-testid="labor-table">
          <Column selectionMode="multiple" style="width: 3rem" />
          <Column field="description" header="Description" sortable />
          <Column field="service_type" header="Service" sortable style="width: 110px" />
          <Column header="Size / SKU" style="width: 110px">
            <template #body="{ data }">{{ laborSizeLabel(data) }}</template>
          </Column>
          <Column header="Price" style="width: 100px">
            <template #body="{ data }">{{ currency(data.flat_price) }}</template>
          </Column>
          <Column header="Man-hrs" style="width: 90px">
            <template #body="{ data }">{{ Number(data.assumed_man_hours).toFixed(1) }}h</template>
          </Column>
        </DataTable>
        <template #footer>
          <Button label="Cancel" severity="secondary" @click="showLaborPicker = false" />
          <Button :label="`Add ${selectedLaborItems.length} Labor Item${selectedLaborItems.length !== 1 ? 's' : ''}`"
            icon="pi pi-plus" :disabled="!selectedLaborItems.length"
            @click="addFromLabor" data-testid="labor-add-btn" />
        </template>
      </Dialog>

      <!-- Save-to-catalog dialog -->
      <Dialog
        v-model:visible="saveCatalogOpen"
        modal
        header="Save line to catalog"
        :style="{ width: '440px' }"
        data-testid="save-to-catalog-dialog"
      >
        <div class="save-catalog-form">
          <div class="form-row-stack">
            <label>Item name</label>
            <InputText v-model="saveCatalogForm.description" data-testid="save-catalog-description" />
          </div>
          <div class="form-row-stack">
            <label>Cost ($)</label>
            <InputNumber v-model="saveCatalogForm.cost" mode="currency" currency="USD" locale="en-US" :min="0"
              data-testid="save-catalog-cost" />
            <small class="muted">Sell price will be computed by the pricing engine using your retail margin tier.</small>
          </div>
          <div class="form-row-stack">
            <label>Pricing category</label>
            <Select v-model="saveCatalogForm.pricing_category"
              :options="pricingCategoryOptions"
              option-label="label"
              option-value="value"
              data-testid="save-catalog-pricing-category" />
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" severity="secondary" text @click="saveCatalogOpen = false" />
          <Button label="Save to catalog" icon="pi pi-bookmark"
            :loading="savingCatalog"
            :disabled="!saveCatalogForm.description || saveCatalogForm.cost == null"
            data-testid="save-catalog-submit"
            @click="submitSaveToCatalog" />
        </template>
      </Dialog>

      <!-- Action Bar -->
      <div class="page-actions" v-if="!loading">
        <Button label="Cancel" severity="secondary" data-testid="cancel-estimate"
          @click="$router.push('/estimates')" />

        <template v-if="!isExisting">
          <Button label="Create Estimate" icon="pi pi-check"
            :disabled="!canCreate" :loading="saving"
            data-testid="confirm-create-estimate" @click="createEstimate" />
        </template>

        <template v-else>
          <Button label="Download PDF" icon="pi pi-file-pdf" severity="secondary"
            data-testid="download-pdf-btn" @click="downloadPdf" />
          <Button label="Email Customer" icon="pi pi-send" data-testid="estimate-send"
            :disabled="estimate.status === 'Accepted' || estimate.status === 'Declined'"
            @click="emailEstimate" />
          <Button label="Accept" icon="pi pi-check" severity="success" data-testid="estimate-accept"
            :disabled="estimate.status === 'Accepted' || estimate.status === 'Declined'"
            @click="acceptEstimate" />
          <Button label="Decline" icon="pi pi-times" severity="danger" outlined data-testid="estimate-decline"
            :disabled="estimate.status === 'Declined' || estimate.status === 'Accepted'"
            @click="declineEstimate" />
          <Button v-if="estimate.status === 'Accepted' && !estimate.job_id"
            label="Convert to Job" icon="pi pi-briefcase" severity="success"
            data-testid="estimate-convert-job" :loading="converting"
            @click="convertToJob" />
          <Button v-if="estimate.job_id"
            :label="linkedJobLabel" icon="pi pi-briefcase" severity="info" outlined
            data-testid="estimate-goto-job"
            @click="$router.push(`/jobs/${estimate.job_id}`)" />
          <Button label="Print" icon="pi pi-print" text data-testid="estimate-print"
            @click="window.print()" />
          <Button label="Copy Link" icon="pi pi-link" text data-testid="estimate-copy-link"
            @click="copyPublicLink" />
          <Button label="Duplicate" icon="pi pi-copy" text data-testid="estimate-duplicate"
            :loading="duplicating" @click="duplicateEstimate" />
          <!-- Slice 3: autosave status indicator. Sits where the Save
               button used to. Save button retained for force-flush + as
               an explicit user action when state == 'error'. -->
          <span class="autosave-status" :class="`autosave-${autosaveState}`"
            v-if="!FINALIZED_STATUSES.has(estimate.status)"
            :data-testid="`autosave-${autosaveState}`">
            <i v-if="autosaveState === 'saving' || autosaveState === 'creating'"
               class="pi pi-spin pi-spinner" />
            <i v-else-if="autosaveState === 'saved'" class="pi pi-check-circle" />
            <i v-else-if="autosaveState === 'error'" class="pi pi-exclamation-triangle" />
            <span>{{ autosaveLabel }}</span>
          </span>
          <Button label="Save Changes" icon="pi pi-save" severity="primary"
            :loading="saving" data-testid="estimate-save"
            @click="saveExistingEstimate" />
        </template>
      </div>

      <!-- Email composer (Outlook-backed) -->
      <Dialog v-model:visible="showComposer" header="Email estimate" modal
        :style="{ width: '720px' }" data-testid="estimate-composer">
        <div v-if="composerLoading" class="composer-loading">Building email…</div>
        <div v-else class="composer-form">
          <div class="form-field">
            <label>To</label>
            <InputText v-model="composer.to" placeholder="customer@example.com"
              class="w-full" data-testid="composer-to" />
          </div>
          <div class="form-field">
            <label>Subject</label>
            <InputText v-model="composer.subject" class="w-full" data-testid="composer-subject" />
          </div>
          <div class="form-field">
            <label>Message</label>
            <Textarea v-model="composer.body_text" rows="8" class="w-full" data-testid="composer-body" />
            <small class="muted">Plain text — line breaks are preserved.</small>
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
          </div>
        </div>
        <template #footer>
          <Button label="Cancel" text @click="showComposer = false" />
          <Button label="Send via Outlook" icon="pi pi-send" severity="primary"
            :loading="composerSending" :disabled="composerLoading || !composer.to"
            data-testid="composer-send" @click="sendComposer" />
        </template>
      </Dialog>

      <!-- ConfirmDialog removed 2026-05-12 — AppLayout.vue:49 mounts one globally. -->
      <Toast data-testid="estimate-view-toast" />
    </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRouter, useRoute } from "vue-router";
import { useToast } from "primevue/usetoast";
import EstimateProfitPanel from "../components/EstimateProfitPanel.vue";
import CatalogPickerDialog from "../components/CatalogPickerDialog.vue";
import PhoneInput from "../components/PhoneInput.vue";
import { useApi } from "../composables/useApi";
import { useApiWithToast } from "../composables/useApiWithToast";
import { formatDate, formatMoney, formatPercent, formatPhone } from "../composables/useFormatters";
import { openAuthedFile, createAuthedBlobUrl } from "../composables/useAuthedFile";
import Button from "primevue/button";
import Card from "primevue/card";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import DatePicker from "primevue/datepicker";
import Dialog from "primevue/dialog";
import Divider from "primevue/divider";
import InputNumber from "primevue/inputnumber";
import InputText from "primevue/inputtext";
import Select from "primevue/select";
import Tag from "primevue/tag";
import Textarea from "primevue/textarea";
import Toast from "primevue/toast";
import ToggleSwitch from "primevue/toggleswitch";
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmDestructive, confirmAsync } = useDestructiveConfirm();

const router = useRouter();
const route = useRoute();
const api = useApiWithToast();
const apiRaw = useApi();
const toast = useToast();

const loading = ref(true);
const saving = ref(false);
const converting = ref(false);
const duplicating = ref(false);
const showAiDialog = ref(false);
const aiDescription = ref("");
const aiLoading = ref(false);
const customers = ref([]);
const attachments = ref([]);
const attachmentsUploading = ref(false);
const attachmentInput = ref(null);
const showComposer = ref(false);
const composerLoading = ref(false);
const composerSending = ref(false);
const composer = ref({ to: "", subject: "", body_text: "", pdf: null, extras: [] });
const lineCategories = ["Doors", "Openers", "Springs", "Labor", "Parts", "Other"];

const isExisting = computed(() => Boolean(route.params.id));

// Server-side estimate metadata (read-only on existing).
const estimate = ref({
  id: null,
  estimate_number: "",
  customer_name: "",
  status: "Draft",
  created_at: "",
  expires_at: "",
  job_id: null,
});

// Linked job (set when this estimate was converted). Loaded best-effort for
// a friendlier link label; the link itself only needs estimate.job_id.
const linkedJob = ref(null);
const linkedJobLabel = computed(() => {
  const num = linkedJob.value?.job_number;
  return num ? `View Job ${num}` : "View Job";
});

async function loadLinkedJob() {
  if (!estimate.value.job_id) return;
  try {
    const result = await apiRaw.get(`/api/jobs/${estimate.value.job_id}`);
    linkedJob.value = result?.data || result || null;
  } catch {
    linkedJob.value = null;
  }
}

// --- Pricing tiers + features ---
const tierSetsByCategory = ref({});
const estimateFeatures = ref({ estimates_allow_line_margin_override: true, estimates_hide_line_prices: false });

// Tri-state per-estimate "total-only" control. The persisted form value is
// null (inherit tenant default) | false (show) | true (hide). PrimeVue Select
// treats a null option value as "no selection" and renders blank, so the
// dropdown uses string sentinels and this computed maps them to/from the
// nullable boolean the API expects.
const hideLinePricesOptions = computed(() => [
  { label: `Company default (${estimateFeatures.value.estimates_hide_line_prices ? "hide prices" : "show prices"})`, value: "inherit" },
  { label: "Show line prices", value: "show" },
  { label: "Hide line-item prices", value: "hide" },
]);
const hideLinePricesChoice = computed({
  get() {
    const v = form.value.hide_line_prices;
    return v === true ? "hide" : v === false ? "show" : "inherit";
  },
  set(choice) {
    form.value.hide_line_prices = choice === "hide" ? true : choice === "show" ? false : null;
  },
});

// Tenant-wide default tax rate (Settings → Tax). Used when an estimate has
// no per-estimate tax_rate. Stored on the server as decimal (0.0825); we
// surface as percent (8.25) in the form.
const tenantDefaultTaxPct = ref(0);
// Tenant-wide "tax labor lines?" toggle. Default false — most US states
// don't tax service labor. Mirrors compute_estimate_totals server logic so
// the on-screen total matches the PDF + saved invoice exactly.
const tenantTaxLabor = ref(false);
// 2026-05-05 — tenant-default loaded technician $/hr (wage + burden). Used by
// addFromLabor to set cost on labor lines so the live profit panel matches
// what the backend will store. Server-side _labor_cost_snapshot is the source
// of truth on save; this is for in-form parity only.
const tenantLoadedLaborRate = ref(0);
async function loadPricingSettings() {
  try {
    const s = await api.get("/api/pricing-engine/settings");
    tenantLoadedLaborRate.value = Number(s?.loaded_labor_cost_per_hour) || 0;
  } catch { /* default 0 — labor lines render at 100% margin */ }
}
async function loadTenantTaxDefault() {
  try {
    const cfg = await api.get("/api/tax/config");
    // Round to 4 decimals to avoid 0.0738 * 100 = 7.380000000000001 artifacts.
    tenantDefaultTaxPct.value = Math.round((Number(cfg?.default_rate) || 0) * 1000000) / 10000;
    tenantTaxLabor.value = Boolean(cfg?.tax_labor);
  } catch { /* default 0 — frontend still computes correctly */ }
}

async function loadEstimateFeatures() {
  try {
    const f = await api.get("/api/estimates-features");
    if (f) estimateFeatures.value = f;
  } catch { /* default permissive */ }
}

function categoryToPricingCategory(cat) {
  const c = (cat || "").toLowerCase();
  if (c === "springs") return "parts";
  if (["doors", "openers", "parts", "labor", "other"].includes(c)) return c;
  return "other";
}

function findTierMargin(pricingCategory, cost) {
  const tiers = tierSetsByCategory.value[pricingCategory];
  if (!tiers || tiers.length === 0 || cost == null || cost < 0) return null;
  const match = tiers.find(t =>
    cost >= Number(t.cost_min ?? 0) &&
    (t.cost_max == null || cost < Number(t.cost_max))
  );
  return match ? Number(match.margin_pct) : null;
}

function defaultLineItem() {
  return {
    category: "Doors",
    description: "",
    quantity: 1,
    cost: null,
    unit_price: 0,
    _priceOverridden: false,
    margin_pct_override: null,
    _marginUserEdited: false,
    _suppressMarginUserEdit: false,
    // S97 slice 5 — null on free-form lines, populated when picked from
    // the Labor matrix.
    labor_price_item_id: null,
    estimated_man_hours: null,
    // Reorder position; renumbered on move, sent on autosave PATCH. New lines
    // leave it null → server assigns max+1 on POST.
    sort_order: null,
  };
}

function recomputeSell(item) {
  if (item._priceOverridden) return;
  const cost = Number(item.cost) || 0;
  const override = Number(item.margin_pct_override);
  let margin;
  if (
    estimateFeatures.value.estimates_allow_line_margin_override
    && item._marginUserEdited
    && Number.isFinite(override)
    && override > 0
    && override < 100
  ) {
    margin = override / 100;
  } else {
    const pc = categoryToPricingCategory(item.category);
    margin = findTierMargin(pc, Number(item.cost));
    if (margin != null && margin < 1
        && estimateFeatures.value.estimates_allow_line_margin_override) {
      const pct = Math.round(margin * 1000) / 10;
      if (Number(item.margin_pct_override) !== pct) {
        item._suppressMarginUserEdit = true;
        item.margin_pct_override = pct;
      }
    }
  }
  if (margin == null || margin >= 1) return;
  const sell = cost / (1 - margin);
  item.unit_price = Math.round(sell * 100) / 100;
}

function onMarginOverrideChange(item) {
  if (item._suppressMarginUserEdit) {
    item._suppressMarginUserEdit = false;
    return;
  }
  if (item.margin_pct_override == null || item.margin_pct_override === "") {
    item._marginUserEdited = false;
    item._priceOverridden = false;
    recomputeSell(item);
    return;
  }
  item._marginUserEdited = true;
  item._priceOverridden = false;
  recomputeSell(item);
}

function markPriceOverride(item) {
  const pc = categoryToPricingCategory(item.category);
  const margin = findTierMargin(pc, Number(item.cost));
  const cost = Number(item.cost);
  const sell = Number(item.unit_price);
  if (margin == null || item.cost == null) {
    item._priceOverridden = false;
  } else {
    const expected = (cost || 0) / (1 - margin);
    item._priceOverridden = Math.abs(expected - (sell || 0)) > 0.01;
  }
  // Reflect the *actual* margin in the Margin column so the user sees what
  // they'd really be running at after changing the price. Clear the
  // user-edited flag so a subsequent recompute (cost/category change) won't
  // snap unit_price back to the tier-implied price.
  if (estimateFeatures.value.estimates_allow_line_margin_override
      && cost > 0 && sell > 0) {
    const actualPct = Math.round(((sell - cost) / sell) * 1000) / 10;
    if (Number(item.margin_pct_override) !== actualPct) {
      item._suppressMarginUserEdit = true;
      item.margin_pct_override = actualPct;
    }
    item._marginUserEdited = false;
  }
}

function marginDisplay(item) {
  const cost = Number(item.cost);
  const sell = Number(item.unit_price);
  if (!cost || !sell || sell <= 0) return "—";
  const margin = (sell - cost) / sell;
  if (margin < 0) return `${formatPercent(margin)} (loss)`;
  return formatPercent(margin);
}

async function loadPricingTiers() {
  try {
    const sets = await api.get("/api/pricing-engine/tier-sets");
    const byCat = {};
    for (const s of sets || []) {
      if (s.pricing_class !== "retail") continue;
      byCat[s.pricing_category] = (s.tiers || []).slice().sort(
        (a, b) => (Number(a.cost_min) || 0) - (Number(b.cost_min) || 0)
      );
    }
    tierSetsByCategory.value = byCat;
  } catch {
    tierSetsByCategory.value = {};
  }
}

// --- Catalog picker ---
const showCatalogPicker = ref(false);

// --- Plugin estimate source (ADR-013) — DO NOT REMOVE (this is a plugin hook). ---
// An installed plugin (e.g. the CHI pricing plugin) can declare an estimate_source
// in its manifest; we then offer "Add <label>" to pull its captured items in as
// estimate lines. estimateSource stays null when no such plugin is installed, so
// every binding below is inert in stock core.
const estimateSource = ref(null);          // { label, list_endpoint, draft_endpoint }
const capturedPickerVisible = ref(false);
const capturedItems = ref([]);             // all captures (summary rows incl. folder)
const capturedLoading = ref(false);
const capturedFolder = ref(null);          // selected folder key; null = folder list view
const selectedDoors = ref([]);             // multi-selected door rows
const addingDoors = ref(false);
const CAPTURED_ALL = "__all__";
const CAPTURED_NONE = "__none__";

// File-explorer model: folders derived from the captures' folder field.
const capturedFolderList = computed(() => {
  const items = capturedItems.value || [];
  const counts = {};
  let none = 0;
  for (const it of items) {
    if (it.folder) counts[it.folder] = (counts[it.folder] || 0) + 1;
    else none += 1;
  }
  const list = [{ key: CAPTURED_ALL, label: "All doors", count: items.length }];
  Object.keys(counts).sort().forEach((f) => list.push({ key: f, label: f, count: counts[f] }));
  if (none) list.push({ key: CAPTURED_NONE, label: "(No folder)", count: none });
  return list;
});
const capturedFolderLabel = computed(() => {
  if (capturedFolder.value === CAPTURED_ALL) return "All doors";
  if (capturedFolder.value === CAPTURED_NONE) return "(No folder)";
  return capturedFolder.value;
});
const doorsInFolder = computed(() => {
  const items = capturedItems.value || [];
  if (capturedFolder.value === CAPTURED_ALL) return items;
  if (capturedFolder.value === CAPTURED_NONE) return items.filter((it) => !it.folder);
  return items.filter((it) => it.folder === capturedFolder.value);
});

async function _discoverEstimateSource() {
  try {
    const plugins = await api.get("/api/plugins");
    const p = (Array.isArray(plugins) ? plugins : []).find((x) => x?.ui?.estimate_source);
    if (p) estimateSource.value = p.ui.estimate_source;
  } catch {
    /* no plugin-host / no plugins → feature stays hidden */
  }
}

async function openCapturedPicker() {
  if (!estimateSource.value) return;
  capturedPickerVisible.value = true;
  capturedFolder.value = null;
  selectedDoors.value = [];
  capturedLoading.value = true;
  try {
    capturedItems.value = (await api.get(estimateSource.value.list_endpoint)) || [];
  } catch {
    capturedItems.value = [];
  } finally {
    capturedLoading.value = false;
  }
}

function openFolder(key) {
  capturedFolder.value = key;
  selectedDoors.value = [];
}
function backToFolders() {
  capturedFolder.value = null;
  selectedDoors.value = [];
}

// Add every selected door as its own line — one door = one line item. Captured
// price → cost → margin engine; photo + full spec ride along.
async function addSelectedDoors() {
  if (!selectedDoors.value.length || !estimateSource.value) return;
  addingDoors.value = true;
  let added = 0;
  let noPhoto = 0;
  try {
    for (const item of selectedDoors.value) {
      let draft;
      try {
        draft = await api.get(estimateSource.value.draft_endpoint.replace("{id}", item.id));
      } catch {
        continue;
      }
      const li = defaultLineItem();
      li.category = draft.category || "Doors";
      li.description = draft.description || "";
      li.cost = draft.cost ?? null;
      li.quantity = draft.quantity || 1;
      li._capturedMeta = draft.line_metadata || null;   // → line_metadata on POST
      recomputeSell(li);                                 // captured cost → engine markup
      form.value.line_items.push(li);                    // deep watcher autosaves it
      if (draft.image && isExisting.value) {
        await _attachCapturedImage(draft.image, item);
      } else if (!draft.image) {
        noPhoto += 1;
      }
      added += 1;
    }
    capturedPickerVisible.value = false;
    toast.add({
      severity: "success",
      summary: `Added ${added} door${added === 1 ? "" : "s"}`,
      detail: noPhoto ? `${noPhoto} had no photo (re-capture to attach).` : undefined,
      life: 3000,
    });
  } finally {
    addingDoors.value = false;
  }
}

async function _attachCapturedImage(dataUrl, item) {
  // Door photo → a core Document on the estimate (best-effort; the line + spec
  // already persist via autosave regardless of the photo).
  try {
    const blob = await (await fetch(dataUrl)).blob();
    const ext = (blob.type.split("/")[1] || "png").replace("jpeg", "jpg");
    const fd = new FormData();
    fd.append("file", blob, `chi-${item.qcd || item.id}.${ext}`);
    await api.post(`/api/estimates/${route.params.id}/attachments`, fd);
    await loadAttachments();   // refresh the panel so the photo shows immediately
    toast.add({ severity: "success", summary: "Door photo attached", life: 2500 });
  } catch {
    toast.add({ severity: "warn", summary: "Photo not attached",
      detail: "The line and spec were saved; the photo upload failed.", life: 4000 });
  }
}
const aiSuggesting = ref(false);

// --- Labor matrix picker (S97 slice 5) ---
// Reads tenant's flat-rate labor rows from /api/labor-pricing/items?active=true
// and on select pushes a new line with description, flat_price as unit_price,
// labor_price_item_id (FK), and estimated_man_hours (snapshot).
const showLaborPicker = ref(false);
const laborItems = ref([]);
const laborSearch = ref("");
const selectedLaborItems = ref([]);
const laborLoading = ref(false);

async function openLaborPicker() {
  showLaborPicker.value = true;
  laborSearch.value = "";
  selectedLaborItems.value = [];
  if (laborItems.value.length) return;
  laborLoading.value = true;
  try {
    const result = await api.get("/api/labor-pricing/items?active=true");
    laborItems.value = (result?.data || result || []).filter((i) => i.active !== false);
  } catch {
    laborItems.value = [];
  } finally {
    laborLoading.value = false;
  }
}

function laborSizeLabel(item) {
  if (item.width_ft && item.height_ft) {
    const w = Math.round(item.width_ft / 12);
    const h = Math.round(item.height_ft / 12);
    return `${w}x${h}`;
  }
  return item.sku || "—";
}

const filteredLaborItems = computed(() => {
  const q = laborSearch.value.toLowerCase();
  if (!q) return laborItems.value;
  return laborItems.value.filter((i) =>
    (i.description || "").toLowerCase().includes(q)
    || (i.service_type || "").toLowerCase().includes(q)
    || (i.sku || "").toLowerCase().includes(q)
    || laborSizeLabel(i).toLowerCase().includes(q)
  );
});

function addFromLabor() {
  const rate = Number(tenantLoadedLaborRate.value) || 0;
  for (const item of selectedLaborItems.value) {
    const hours = Number(item.assumed_man_hours) || 0;
    const cost = Math.round(rate * hours * 100) / 100;
    form.value.line_items.push({
      ...defaultLineItem(),
      category: "Labor",
      description: item.description,
      quantity: 1,
      unit_price: Number(item.flat_price) || 0,
      // Cost derived from tenant loaded labor rate × assumed_man_hours so the
      // live profit panel matches what the backend stamps on save. Doug
      // 2026-05-05 — fixes EST-000026 silent drop in profit calculator.
      cost,
      _priceOverridden: true, // suppress engine recompute — flat_price is the price
      labor_price_item_id: item.id,
      estimated_man_hours: hours,
    });
  }
  selectedLaborItems.value = [];
  showLaborPicker.value = false;
}

// --- Save-to-catalog dialog ---
const saveCatalogOpen = ref(false);
const savingCatalog = ref(false);
const saveCatalogForm = ref({ description: "", cost: null, pricing_category: "parts" });
// Hardcoded fallback; replaced at mount by the data-driven list from
// /api/catalogs/pricing-categories so admin-seeded tier categories appear.
const pricingCategoryOptions = ref([
  { label: "Doors", value: "doors" },
  { label: "Openers", value: "openers" },
  { label: "Parts", value: "parts" },
  { label: "Labor", value: "labor" },
  { label: "Other", value: "other" },
]);

async function loadPricingCategories() {
  try {
    const cats = await api.get("/api/catalogs/pricing-categories", { suppressErrorToast: true });
    if (Array.isArray(cats) && cats.length) {
      pricingCategoryOptions.value = cats.map((c) => ({
        label: c.charAt(0).toUpperCase() + c.slice(1),
        value: c,
      }));
    }
  } catch (e) {
    /* keep the hardcoded fallback */
  }
}

function openSaveToCatalog(line) {
  saveCatalogForm.value = {
    description: line.description || "",
    cost: line.cost ?? null,
    pricing_category: categoryToPricingCategory(line.category),
  };
  saveCatalogOpen.value = true;
}

async function submitSaveToCatalog() {
  if (!saveCatalogForm.value.description || saveCatalogForm.value.cost == null) return;
  savingCatalog.value = true;
  try {
    const result = await api.post("/api/catalogs/save-from-estimate-line", {
      description: saveCatalogForm.value.description,
      cost: Number(saveCatalogForm.value.cost),
      pricing_category: saveCatalogForm.value.pricing_category,
    }, { successMessage: null });
    toast.add({
      severity: "success",
      summary: "Saved to catalog",
      detail: `${result.name} — sells at ${formatMoney(result.price)} (cost ${formatMoney(result.cost)})`,
      life: 5000,
    });
    saveCatalogOpen.value = false;
  } catch { /* useApiWithToast already toasts */ }
  finally { savingCatalog.value = false; }
}

// --- Form state ---
function defaultValidUntil() {
  const d = new Date();
  d.setDate(d.getDate() + 30);
  return d;
}

const form = ref({
  customer_id: null,
  new_customer: false,
  new_cust_name: "",
  new_cust_phone: "",
  new_cust_email: "",
  new_cust_address: "",
  label: "",
  description: "",
  jobsite_address: "",
  valid_until: defaultValidUntil(),
  notes: "",
  // tax_rate is a percent (e.g. 8.25). null = "use tenant default" — we
  // don't bind null directly to the InputNumber; the form watcher copies
  // tenantDefaultTaxPct in once it loads.
  tax_rate: 0,
  discount: 0,
  // "Total-only" override. null = inherit tenant default; true/false explicit.
  hide_line_prices: null,
  line_items: [defaultLineItem()],
});

const selectedCustomer = computed(() => {
  if (!form.value.customer_id) return null;
  return customers.value.find((c) => String(c.id) === String(form.value.customer_id)) || null;
});

function extractId(payload) {
  if (payload == null) return null;
  if (typeof payload === "number" || typeof payload === "string") return payload;
  return payload.id ?? payload.data?.id ?? null;
}

function handleNewCustomerToggle(value) {
  if (value) {
    form.value.customer_id = null;
    return;
  }
  form.value.new_cust_name = "";
  form.value.new_cust_phone = "";
  form.value.new_cust_email = "";
  form.value.new_cust_address = "";
}

const customerOptions = computed(() =>
  customers.value.map((c) => ({ label: c.name, value: c.id }))
);

function toNum(v) {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function currency(amount) {
  return formatMoney(toNum(amount));
}

function statusSeverity(status) {
  const map = { Draft: "secondary", Sent: "info", Accepted: "success", Declined: "danger", Converted: "contrast" };
  return map[status] || "secondary";
}

function _titleCase(s) {
  if (!s) return s;
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}

const subtotal = computed(() =>
  form.value.line_items.reduce((sum, li) => sum + toNum(li.quantity) * toNum(li.unit_price), 0)
);
// Mirror compute_estimate_totals server logic: when tenantTaxLabor=false,
// lines with category == 'labor' (case-insensitive) are excluded from the
// taxable subtotal. Discount is then applied to the remaining materials.
const laborSubtotal = computed(() =>
  form.value.line_items.reduce((sum, li) => {
    const cat = (li.category || '').trim().toLowerCase();
    if (cat !== 'labor') return sum;
    return sum + toNum(li.quantity) * toNum(li.unit_price);
  }, 0)
);
const taxableSubtotal = computed(() => {
  const base = tenantTaxLabor.value ? subtotal.value : (subtotal.value - laborSubtotal.value);
  return Math.max(base - toNum(form.value.discount), 0);
});
const taxAmount = computed(() =>
  taxableSubtotal.value * toNum(form.value.tax_rate) / 100
);
const total = computed(() =>
  Math.max(subtotal.value - toNum(form.value.discount), 0) + taxAmount.value
);

// Shape lines into the form EstimateProfitPanel expects:
//   { id, description, quantity, unit_price, cost_snapshot, margin_pct_snapshot,
//     margin_pct_override, pricing_source }
// The panel filters on `cost_snapshot != null && margin_pct_snapshot != null`,
// so we have to derive a margin from cost+sell whenever cost is present (engine
// already did the math when the user edited cost or margin; this re-computes
// the implied margin so live edits surface in the panel without round-tripping).
const profitPanelLines = computed(() =>
  form.value.line_items.map((l, idx) => {
    const cost = Number(l.cost);
    const sell = Number(l.unit_price);
    const hasCost = Number.isFinite(cost) && cost >= 0 && l.cost != null;
    const margin = hasCost && sell > 0 ? (sell - cost) / sell : null;
    const overrideDecimal = l._marginUserEdited
      && Number.isFinite(Number(l.margin_pct_override))
      && Number(l.margin_pct_override) > 0
      ? Number(l.margin_pct_override) / 100
      : null;
    return {
      id: l.id || `tmp-${idx}`,
      description: l.description,
      quantity: Number(l.quantity) || 0,
      unit_price: Number.isFinite(sell) ? sell : 0,
      cost_snapshot: hasCost ? cost : null,
      margin_pct_snapshot: margin,
      margin_pct_override: overrideDecimal,
      pricing_source: l._marginUserEdited ? "line_override" : "tier",
    };
  })
);

const canCreate = computed(() =>
  (form.value.customer_id || (form.value.new_customer && form.value.new_cust_name?.trim())) &&
  form.value.line_items.some((li) => li.description && li.unit_price > 0)
);

// Items come from the shared <CatalogPickerDialog>, normalized to
// { name, description, category, cost, price, pricing_category }.
function addFromCatalog(items) {
  for (const item of items) {
    const cost = Number(item.cost) > 0 ? Number(item.cost) : null;
    const pc = cost ? (item.pricing_category || categoryToPricingCategory(item.category)) : null;
    // Display category drives the dropdown; derive from the canonical pricing
    // bucket (title-cased) so it always matches an option and reflects how the
    // line is priced. Falls back to the item's free-form category otherwise.
    const titleCased = pc ? pc.charAt(0).toUpperCase() + pc.slice(1) : null;
    const displayCat = titleCased && lineCategories.includes(titleCased)
      ? titleCased : (item.category || "Parts");
    const line = {
      ...defaultLineItem(),
      category: displayCat,
      description: item.description || item.name,
      quantity: 1,
      unit_price: Number(item.price) || 0,
      // Carry cost + pricing bucket so the tier engine computes the marked-up
      // sell. Without these the line posts at the catalog price (zero markup).
      cost,
      pricing_category: pc,
    };
    form.value.line_items.push(line);
    // Show the marked-up sell in the builder immediately, matching what the
    // backend engine will persist on save (instead of displaying raw cost).
    if (cost) recomputeSell(line);
  }
}

async function aiSuggestLines() {
  const desc = [form.value.label, form.value.description, form.value.notes].filter(Boolean).join(". ");
  if (!desc || desc.length < 3) return;
  aiSuggesting.value = true;
  try {
    const result = await api.post("/api/ai/estimates/suggest", {
      job_description: desc,
      job_type: "Service",
    });
    const lines = result?.suggested_lines || result?.data?.suggested_lines || [];
    for (const line of lines) {
      form.value.line_items.push({
        ...defaultLineItem(),
        category: line.category || "Parts",
        description: line.description,
        quantity: line.quantity || 1,
        unit_price: line.unit_price || 0,
      });
    }
  } catch { /* toast handled by api */ }
  finally { aiSuggesting.value = false; }
}

async function runAiEstimate() {
  aiLoading.value = true;
  try {
    const result = await api.post("/api/ai/instant-estimate", { description: aiDescription.value });
    const items = (result.line_items || []).map((li) => ({
      ...defaultLineItem(),
      category: li.source === "labor" ? "Labor" : "Doors",
      description: li.name,
      quantity: li.qty || 1,
      unit_price: li.unit_price || 0,
    }));
    if (items.length) {
      form.value.line_items = items;
      if (result.suggested_door?.description) {
        form.value.description = result.suggested_door.description.slice(0, 200);
      }
    }
    showAiDialog.value = false;
    aiDescription.value = "";
    toast.add({ severity: "success", summary: "AI Estimate", detail: `${items.length} line items generated — $${result.total}`, life: 4000 });
  } catch {
    toast.add({ severity: "error", summary: "AI Failed", detail: "Could not generate estimate from description", life: 4000 });
  } finally {
    aiLoading.value = false;
  }
}

async function loadCustomers() {
  try {
    const data = await api.get("/api/customers?per_page=500");
    const list = Array.isArray(data) ? data : data?.items || data?.data || [];
    customers.value = list;
  } catch {
    customers.value = [];
  }
}

// --- Load existing estimate ---
async function fetchEstimate() {
  if (!route.params.id) {
    loading.value = false;
    return;
  }
  try {
    const result = await api.get(`/api/estimates/${route.params.id}`);
    const data = result?.data || result || {};
    const rawStatus = data.status || "Draft";
    const normalizedStatus = rawStatus
      ? rawStatus.charAt(0).toUpperCase() + rawStatus.slice(1).toLowerCase()
      : "Draft";
    estimate.value = {
      id: data.id,
      estimate_number: data.estimate_number || data.estimateNumber || `EST-${String(data.id).substring(0, 8)}`,
      customer_name: data.customer_name || data.customer || (typeof data.customer === 'object' ? data.customer?.name : '') || '',
      status: normalizedStatus,
      created_at: data.created_at || data.createdAt || data.created || "",
      expires_at: data.expires_at || data.expiresAt || data.expiry_date || data.valid_until || "",
      job_id: data.job_id || null,
    };
    loadLinkedJob();
    // tax_rate on server is decimal (0.0825); null means "use tenant default".
    const serverRate = data.tax_rate ?? data.taxRate;
    let taxPct;
    if (serverRate == null) {
      taxPct = tenantDefaultTaxPct.value;
    } else {
      const rate = toNum(serverRate);
      // Round to 4 decimals to dodge float-precision artifacts (7.380000000000001).
      taxPct = rate > 1
        ? Math.round(rate * 10000) / 10000
        : Math.round(rate * 1000000) / 10000;
    }
    const lineSrc = data.lines || data.line_items || data.items || [];
    form.value = {
      customer_id: data.customer_id ?? null,
      new_customer: false,
      new_cust_name: "", new_cust_phone: "", new_cust_email: "", new_cust_address: "",
      label: data.label || "",
      description: data.description || "",
      jobsite_address: data.jobsite_address || "",
      valid_until: data.valid_until || data.expires_at || defaultValidUntil(),
      notes: data.notes || "",
      tax_rate: taxPct,
      discount: toNum(data.discount ?? 0),
      // null = inherit tenant default; true/false = explicit per-estimate override.
      hide_line_prices: data.hide_line_prices ?? null,
      // Snapshot whether the loaded estimate had its own tax_rate. If null on
      // server and user doesn't change the form value, we save back null on
      // PATCH so the estimate continues to track the tenant default if it
      // changes later.
      _tax_rate_was_null: data.tax_rate == null && data.taxRate == null,
      line_items: lineSrc.length
        ? lineSrc.map((li, i) => ({
            ...defaultLineItem(),
            id: li.id,  // preserve server line id for future PATCH support
            sort_order: li.sort_order ?? (i + 1),  // for reorder up/down
            category: li.category || "Other",
            description: li.description || "",
            quantity: toNum(li.quantity ?? 1),
            cost: li.cost_snapshot ?? li.cost ?? null,
            unit_price: toNum(li.unit_price ?? 0),
            // 2 decimals of percent = the backend's 4-decimal margin quantum
            // (0.0001) exactly — toFixed(1) lost precision here, so the
            // load→autosave round-trip degraded stored overrides.
            margin_pct_override: li.margin_pct_override != null
              ? Number((Number(li.margin_pct_override) * 100).toFixed(2))
              : null,
            _marginUserEdited: li.margin_pct_override != null,
            labor_price_item_id: li.labor_price_item_id ?? null,
            estimated_man_hours: li.estimated_man_hours ?? null,
          }))
        : [defaultLineItem()],
    };
    await loadAttachments();
  } catch {
    toast.add({ severity: "warn", summary: "Load failed", detail: "Could not load estimate", life: 4000 });
  } finally {
    loading.value = false;
  }
}

// --- Attachments ---
function isImageMime(ct) {
  return typeof ct === "string" && ct.startsWith("image/");
}

function formatBytes(n) {
  const v = Number(n) || 0;
  if (v < 1024) return `${v} B`;
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`;
  return `${(v / (1024 * 1024)).toFixed(1)} MB`;
}

async function loadAttachments() {
  if (!route.params.id) return;
  try {
    const res = await api.get(`/api/estimates/${route.params.id}/attachments`);
    const list = Array.isArray(res) ? res : (res?.data || []);
    // Revoke any prior preview URLs to avoid leaking blobs.
    for (const a of attachments.value) {
      if (a._previewUrl) {
        try { URL.revokeObjectURL(a._previewUrl); } catch { /* ignore */ }
      }
    }
    attachments.value = list;
    // Fetch authed thumbnail blob URLs for images so <img> works without
    // a session cookie on the file endpoint.
    for (const att of list) {
      if (isImageMime(att.content_type)) {
        try {
          att._previewUrl = await createAuthedBlobUrl(att.download_url);
        } catch { /* ignore — will show placeholder icon */ }
      }
    }
    attachments.value = [...list];
  } catch {
    attachments.value = [];
  }
}

async function openAttachment(att) {
  if (!att?.download_url) return;
  try {
    await openAuthedFile(att.download_url);
  } catch {
    toast.add({ severity: "error", summary: "Open failed", life: 4000 });
  }
}

async function onAttachmentFiles(ev) {
  const files = Array.from(ev?.target?.files || []);
  if (!files.length || !route.params.id) return;
  attachmentsUploading.value = true;
  try {
    for (const f of files) {
      const fd = new FormData();
      fd.append("file", f);
      try {
        await api.post(`/api/estimates/${route.params.id}/attachments`, fd);
      } catch (e) {
        toast.add({ severity: "error", summary: "Upload failed", detail: f.name, life: 4000 });
      }
    }
    await loadAttachments();
  } finally {
    attachmentsUploading.value = false;
    if (attachmentInput.value) attachmentInput.value.value = "";
  }
}

async function deleteAttachment(att) {
  if (!route.params.id || !att?.id) return;
  if (!(await confirmAsync({ header: 'Confirm', message: `Delete ${att.original_name}?` }))) return;
  try {
    await api.del(`/api/estimates/${route.params.id}/attachments/${att.id}`);
    attachments.value = attachments.value.filter((a) => a.id !== att.id);
  } catch {
    toast.add({ severity: "error", summary: "Delete failed", life: 4000 });
  }
}

// --- S-autosave Slice 1: draft-on-first-customer-pick ------------------
// On /estimates/new, the moment the user picks an existing customer (or
// arrives via ?customer_id=…), debounce ~500ms and POST a minimal draft.
// router.replace flips the URL to /estimates/:id without unmounting this
// component (route component is the same EstimateView). After that point
// `isExisting` becomes true and the existing-mode action bar kicks in.
//
// Why "first customer pick" and not "first keystroke": POST /api/estimates
// requires job_id OR customer_id (router validation). Typing a label or
// notes with no customer is a no-op for the backend. New-customer path
// (form.new_customer === true) still goes through the manual "Create
// Estimate" button — needs a customer create first, which has its own
// required fields and validation.
const autosaveState = ref("idle"); // idle | creating | saving | saved | error
const autosaveLastAt = ref(null);
const autosaveError = ref("");
let _autosaveDraftTimer = null;
let _autosaveCreating = false;

async function _createDraftFromForm() {
  if (_autosaveCreating || isExisting.value) return;
  if (!form.value.customer_id) return;
  _autosaveCreating = true;
  autosaveState.value = "creating";
  try {
    const formPct = Number(form.value.tax_rate) || 0;
    const persistTax = Math.abs(formPct - tenantDefaultTaxPct.value) > 0.001;
    // Minimal payload — just the binding to a customer. Lines, label,
    // notes etc come through the existing manual "Save Changes" path
    // (Slice 2 will switch those to incremental PATCH/POST/DELETE).
    const payload = {
      customer_id: form.value.customer_id,
      label: form.value.label || null,
      jobsite_address: form.value.jobsite_address || null,
      notes: form.value.notes || null,
      tax_rate: persistTax ? formPct / 100 : null,
      discount: Number(form.value.discount) > 0 ? Number(form.value.discount) : null,
      hide_line_prices: form.value.hide_line_prices ?? null,
    };
    const result = await apiRaw.post("/api/estimates", payload);
    const created = result?.data || result;
    if (created?.id) {
      await router.replace(`/estimates/${created.id}`);
      // Refresh form from the server snapshot so estimate.id /
      // estimate_number / created_at populate and the existing-mode
      // toolbar renders correctly.
      await fetchEstimate();
      autosaveState.value = "saved";
      autosaveLastAt.value = Date.now();
    }
  } catch (err) {
    autosaveError.value = err?.message || "Could not start draft";
    autosaveState.value = "error";
  } finally {
    _autosaveCreating = false;
  }
}

watch(
  () => form.value.customer_id,
  (val) => {
    if (isExisting.value) return;
    if (!val) return;
    if (form.value.new_customer) return;
    if (_autosaveDraftTimer) clearTimeout(_autosaveDraftTimer);
    _autosaveDraftTimer = setTimeout(_createDraftFromForm, 500);
  },
);

// --- S-autosave Slice 2: incremental autosave on existing estimates ----
// Once the URL is /estimates/:id (either we got there via Slice 1 draft
// creation, or via a direct link), debounce form changes and persist
// without a manual save click.
//
// Strategy: PATCH header on every flush (cheap; last-write-wins). For
// lines, every line with a server `id` is PATCHed; every line without an
// id is POSTed (and the returned id captured back onto the line); every
// id in `pendingLineDeletes` is DELETEd. No per-line dirty tracking — the
// cost of one PATCH per line per flush is acceptable since flushes are
// rate-limited to once every 800ms after edits stop. Idempotent, simpler
// than diffing, and recovers cleanly from any single failed call.
//
// Single in-flight lock with one queued retry — if changes arrive while a
// flush is running, we don't overlap; we run one more flush after the
// current one settles.
const pendingLineDeletes = ref([]);
let _autosaveDebounce = null;
let _autosaveInFlight = false;
let _autosaveInFlightPromise = null;
let _autosaveQueued = false;
const FINALIZED = new Set(["accepted", "declined", "Accepted", "Declined"]);
// Exposed to the template for the Slice 3 status pill — Vue's <template>
// can't reach a const declared in <script setup> unless we re-declare it.
const FINALIZED_STATUSES = FINALIZED;

const _autosaveTick = ref(0);
let _autosaveTickInterval = null;
const autosaveLabel = computed(() => {
  // Touch _autosaveTick so the relative timestamp re-renders every 10s.
  void _autosaveTick.value;
  if (autosaveState.value === "creating") return "Starting draft…";
  if (autosaveState.value === "saving") return "Saving…";
  if (autosaveState.value === "error") return autosaveError.value || "Save failed — retry";
  if (autosaveState.value === "saved" && autosaveLastAt.value) {
    const secs = Math.max(0, Math.floor((Date.now() - autosaveLastAt.value) / 1000));
    if (secs < 5) return "All changes saved";
    if (secs < 60) return `Saved ${secs}s ago`;
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `Saved ${mins}m ago`;
    return `Saved ${Math.floor(mins / 60)}h ago`;
  }
  return "";
});

function removeLineAt(idx) {
  const li = form.value.line_items[idx];
  if (li?.id) pendingLineDeletes.value.push(li.id);
  if (form.value.line_items.length > 1) {
    form.value.line_items.splice(idx, 1);
  } else {
    // Keep at least one editable row visible; reset in place. If it had a
    // server id, the DELETE was already queued above; the replacement row
    // is a fresh blank line that will POST on next flush only if the user
    // fills it in.
    form.value.line_items.splice(idx, 1, defaultLineItem());
  }
}

function moveLine(idx, dir) {
  // dir: -1 = up, +1 = down. Swap in place, then renumber sort_order to match
  // the visual order so the autosave PATCH persists it (read-back sorts by it).
  const items = form.value.line_items;
  const j = idx + dir;
  if (j < 0 || j >= items.length) return;
  const [moved] = items.splice(idx, 1);
  items.splice(j, 0, moved);
  items.forEach((li, i) => { li.sort_order = i + 1; });
  // The deep watcher autosaves; nudge it so order persists promptly.
  _scheduleFlush();
}

function _lineHasContent(li) {
  // Only POST a line once it has a description and a non-zero price. Keeps
  // empty placeholder rows out of the database.
  return Boolean(li?.description && Number(li?.unit_price) > 0);
}

function _linePostPayload(li) {
  // Doug 2026-05-07 / EST-000030: labor-matrix lines bypass the tier
  // engine. Sending pricing_category alongside labor_price_item_id used to
  // route the line through price_line() which overwrote flat_price. The
  // backend now ignores cost/pricing_category when labor_price_item_id is
  // set, but we omit them here too — fail loud if any client regresses.
  const isLaborMatrix = li.labor_price_item_id != null;
  const payload = {
    description: li.description,
    category: li.category || null,
    quantity: li.quantity,
    unit_price: li.unit_price,
    cost: isLaborMatrix ? null : (li.cost ?? null),
    pricing_category: isLaborMatrix ? null : (li.pricing_category || categoryToPricingCategory(li.category)),
    labor_price_item_id: li.labor_price_item_id ?? null,
    estimated_man_hours: li.estimated_man_hours ?? null,
  };
  // Plugin integration (ADR-013) — carry the captured source spec onto the line.
  if (li._capturedMeta) payload.line_metadata = li._capturedMeta;
  if (estimateFeatures.value.estimates_allow_line_margin_override
      && li._marginUserEdited
      && Number.isFinite(Number(li.margin_pct_override))
      && Number(li.margin_pct_override) > 0
      && Number(li.margin_pct_override) < 100) {
    payload.margin_pct_override = Number(li.margin_pct_override) / 100;
  } else if (li._priceOverridden
             && Number(li.cost) > 0
             && Number(li.unit_price) > Number(li.cost)) {
    const cost = Number(li.cost);
    const sell = Number(li.unit_price);
    const impliedMargin = (sell - cost) / sell;
    if (impliedMargin > 0 && impliedMargin < 1) {
      payload.margin_pct_override = impliedMargin;
    }
  }
  return payload;
}

function _linePatchPayload(li) {
  // PATCH endpoint accepts a subset; mirror what create does. cost can
  // re-resolve sell server-side, so always send — except for labor-matrix
  // lines, where matrix flat_price is authoritative and a client-supplied
  // cost would re-trigger the engine path we just locked out (S3).
  const isLaborMatrix = li.labor_price_item_id != null;
  const payload = {
    description: li.description,
    category: li.category || null,
    quantity: li.quantity,
    unit_price: li.unit_price,
    cost: isLaborMatrix ? null : (li.cost ?? null),
  };
  if (Number.isFinite(Number(li.sort_order))) {
    payload.sort_order = Number(li.sort_order);
  }
  if (estimateFeatures.value.estimates_allow_line_margin_override) {
    if (li._marginUserEdited
        && Number.isFinite(Number(li.margin_pct_override))
        && Number(li.margin_pct_override) > 0
        && Number(li.margin_pct_override) < 100) {
      payload.margin_pct_override = Number(li.margin_pct_override) / 100;
    } else {
      // No user margin override → clear any previously stored one.
      payload.clear_margin_override = true;
    }
  }
  return payload;
}

async function _flushNow() {
  if (!isExisting.value) return;
  if (FINALIZED.has(estimate.value.status)) return;
  if (_autosaveInFlight) {
    _autosaveQueued = true;
    return;
  }
  _autosaveInFlight = true;
  let _resolveInFlight;
  _autosaveInFlightPromise = new Promise((resolve) => { _resolveInFlight = resolve; });
  autosaveState.value = "saving";
  autosaveError.value = "";
  const id = route.params.id;
  try {
    // 1. Header.
    const formPct = Number(form.value.tax_rate) || 0;
    const persistTax = Math.abs(formPct - tenantDefaultTaxPct.value) > 0.001;
    await apiRaw.patch(`/api/estimates/${id}`, {
      label: form.value.label || null,
      jobsite_address: form.value.jobsite_address || null,
      description: form.value.description || null,
      notes: form.value.notes || null,
      tax_rate: persistTax ? formPct / 100 : null,
      discount: Number(form.value.discount) > 0 ? Number(form.value.discount) : null,
      hide_line_prices: form.value.hide_line_prices ?? null,
    });

    // 2. Pending deletes — drain first so newly-added lines don't collide.
    if (pendingLineDeletes.value.length > 0) {
      const toDelete = pendingLineDeletes.value.splice(0);
      for (const lineId of toDelete) {
        try {
          await apiRaw.del(`/api/estimates/${id}/lines/${lineId}`);
        } catch {
          // Already gone is fine — last-write-wins.
        }
      }
    }

    // 3. Lines: POST new, PATCH existing. New lines need content before the
    // first POST (keeps blank placeholder rows out of the DB); existing lines
    // PATCH whenever they still have a description AND a price value — a
    // price edited down to $0 is a real edit that must reach the DB (and the
    // PDF), but a transiently CLEARED price input (null, mid-retype) must
    // not: the backend treats null as "derive for me" (409s manual lines).
    for (const li of form.value.line_items) {
      if (li.id ? !(li.description && li.unit_price != null) : !_lineHasContent(li)) continue;
      if (li.id) {
        try {
          await apiRaw.patch(`/api/estimates/${id}/lines/${li.id}`, _linePatchPayload(li));
        } catch (err) {
          // 404 = server line was deleted out from under us; drop the id
          // so the next flush re-creates it.
          if (err?.status === 404) li.id = undefined;
          else throw err;
        }
      } else {
        const result = await apiRaw.post(`/api/estimates/${id}/lines`, _linePostPayload(li));
        const created = result?.data || result;
        if (created?.id) li.id = created.id;
      }
    }

    autosaveLastAt.value = Date.now();
    autosaveState.value = "saved";
  } catch (err) {
    autosaveError.value = err?.message || "Save failed";
    autosaveState.value = "error";
  } finally {
    _autosaveInFlight = false;
    _resolveInFlight();
    _autosaveInFlightPromise = null;
    if (_autosaveQueued) {
      _autosaveQueued = false;
      // Re-arm the debounce — give a brief breather instead of an
      // immediate retry, in case the queued change was followed by more
      // user input.
      _scheduleFlush();
    }
  }
}

// Force-persist the current form state before any action that renders it
// back from the server (PDF download, email compose, manual save). A bare
// `await _flushNow()` is NOT sufficient: it early-returns (queues a retry)
// while another flush is in flight — exactly the racing case. This drains
// the in-flight flush, cancels any re-armed debounce, then runs one final
// authoritative flush of the current form state to completion.
async function forceFlush() {
  if (_autosaveDebounce) { clearTimeout(_autosaveDebounce); _autosaveDebounce = null; }
  while (_autosaveInFlightPromise) {
    await _autosaveInFlightPromise;
  }
  // The drained flush's finally may have re-armed the debounce for a queued
  // retry; the flush below supersedes it.
  if (_autosaveDebounce) { clearTimeout(_autosaveDebounce); _autosaveDebounce = null; }
  _autosaveQueued = false;
  await _flushNow();
}

function _scheduleFlush() {
  if (!isExisting.value) return;
  if (FINALIZED.has(estimate.value.status)) return;
  if (_autosaveDebounce) clearTimeout(_autosaveDebounce);
  _autosaveDebounce = setTimeout(_flushNow, 800);
}

// Deep watcher on the form. Vue fires deep watchers for nested edits.
// fetchEstimate reassigns form.value wholesale, which also fires this
// watcher exactly once — but the resulting flush is a no-op (header
// PATCH with same values, no new lines). Acceptable; the alternative is
// a "loading" guard, which adds bug surface.
watch(
  () => form.value,
  () => { if (isExisting.value) _scheduleFlush(); },
  { deep: true },
);

// --- Save (create) ---
async function createEstimate() {
  saving.value = true;
  try {
    let customerId = form.value.customer_id;
    if (form.value.new_customer) {
      const newCustomerPayload = {
        name: form.value.new_cust_name.trim(),
        phone: form.value.new_cust_phone?.trim() || null,
        email: form.value.new_cust_email?.trim() || null,
        address: form.value.new_cust_address?.trim() || null,
      };
      const createdCustomer = await api.post("/api/customers", newCustomerPayload);
      customerId = extractId(createdCustomer);
      if (!customerId) {
        saving.value = false;
        return;
      }
    }
    const validUntil = form.value.valid_until instanceof Date
      ? form.value.valid_until.toISOString().slice(0, 10)
      : form.value.valid_until;
    // Only persist a per-estimate tax_rate if the user changed it from the
    // tenant default. Otherwise leave null so the estimate tracks tenant.
    const formPct = Number(form.value.tax_rate) || 0;
    const persistTax = Math.abs(formPct - tenantDefaultTaxPct.value) > 0.001;
    const payload = {
      customer_id: customerId,
      label: form.value.label,
      description: form.value.description,
      jobsite_address: form.value.jobsite_address || null,
      valid_until: validUntil || null,
      notes: form.value.notes,
      tax_rate: persistTax ? formPct / 100 : null,
      discount: Number(form.value.discount) > 0 ? Number(form.value.discount) : null,
      hide_line_prices: form.value.hide_line_prices ?? null,
      line_items: form.value.line_items
        .filter((li) => li.description && li.unit_price > 0)
        .map((li) => ({
          category: li.category,
          description: li.description,
          quantity: li.quantity,
          unit_price: li.unit_price,
          cost: li.cost ?? null,
          labor_price_item_id: li.labor_price_item_id ?? null,
          estimated_man_hours: li.estimated_man_hours ?? null,
        })),
    };
    const result = await api.post("/api/estimates", payload, { successMessage: "Estimate created" });
    const created = result?.data || result;
    if (created?.id) {
      router.push(`/estimates/${created.id}`);
    } else {
      router.push("/estimates");
    }
  } finally {
    saving.value = false;
  }
}

// --- Save (existing) — force-flush the autosave queue ---
// Slice 2: the manual "Save Changes" button is now a synchronous wrapper
// around the same incremental autosave path. Cancels any pending debounce,
// awaits the flush, and shows a toast on result. Removed the V1 wholesale
// delete-and-re-post path — it raced with autosave and would nuke
// autosave-created line ids mid-flight.
async function saveExistingEstimate() {
  saving.value = true;
  try {
    await forceFlush();
    if (autosaveState.value === "error") {
      toast.add({ severity: "error", summary: "Save failed", detail: autosaveError.value || "Could not save", life: 4000 });
    } else {
      toast.add({ severity: "success", summary: "Saved", detail: "Estimate saved", life: 2000 });
    }
  } finally {
    saving.value = false;
  }
}

// --- Status transitions ---
async function downloadPdf() {
  try {
    // The PDF renders server-side from the DB — persist pending edits first
    // or the document silently omits them (the debounce window race).
    await forceFlush();
    if (autosaveState.value === "error") {
      toast.add({ severity: "warn", summary: "Unsaved changes", detail: "Latest edits could not be saved — the PDF may not include them", life: 6000 });
    }
    await openAuthedFile(`/api/estimates/${route.params.id}/pdf`);
  } catch (e) {
    toast.add({ severity: "error", summary: "PDF failed", detail: e?.message || "Could not open estimate PDF", life: 5000 });
  }
}

async function emailEstimate() {
  // The composer snapshots the PDF server-side at open — flush first, and
  // refuse to open on a failed save: emailing a customer a stale-price PDF
  // is the worst failure mode this screen has.
  await forceFlush();
  if (autosaveState.value === "error") {
    toast.add({ severity: "error", summary: "Save failed", detail: "Fix the save error before emailing — the attached PDF would not include your latest changes", life: 6000 });
    return;
  }
  composerLoading.value = true;
  showComposer.value = true;
  composer.value = { to: "", subject: "", body_text: "", pdf: null, extras: [] };
  try {
    const data = await api.get(`/api/estimates/${route.params.id}/email-compose`);
    const payload = data?.data || data;
    composer.value = {
      to: (payload.to && payload.to[0]) || "",
      subject: payload.subject || "",
      body_text: payload.body_text || "",
      pdf: payload.pdf,
      extras: (payload.extra_attachments || []).map((a) => ({ ...a, _include: true })),
    };
  } catch (err) {
    showComposer.value = false;
    toast.add({ severity: "error", summary: "Compose failed", detail: err?.message || "", life: 4000 });
  } finally {
    composerLoading.value = false;
  }
}

async function _blobToBase64(blob) {
  return await new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onerror = () => reject(r.error);
    r.onload = () => {
      const s = String(r.result || "");
      const i = s.indexOf(",");
      resolve(i >= 0 ? s.slice(i + 1) : s);
    };
    r.readAsDataURL(blob);
  });
}

async function sendComposer() {
  if (!composer.value.to) return;
  composerSending.value = true;
  try {
    // Gather attachments. PDF is already base64 from the server.
    const atts = [
      {
        name: composer.value.pdf.name,
        content_type: composer.value.pdf.content_type,
        content_base64: composer.value.pdf.content_base64,
      },
    ];
    for (const ex of composer.value.extras) {
      if (!ex._include) continue;
      try {
        const blobUrl = await createAuthedBlobUrl(
          `/api/estimates/${route.params.id}/attachments/${ex.id}/download`,
        );
        const blob = await (await fetch(blobUrl)).blob();
        URL.revokeObjectURL(blobUrl);
        atts.push({
          name: ex.name,
          content_type: ex.content_type,
          content_base64: await _blobToBase64(blob),
        });
      } catch (e) {
        toast.add({ severity: "warn", summary: "Skipping attachment", detail: ex.name, life: 3000 });
      }
    }
    // Plain-text body → wrap in <pre> so newlines survive.
    const bodyHtml = `<pre style="font-family:Arial,sans-serif;font-size:14px;white-space:pre-wrap">${
      composer.value.body_text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    }</pre>`;
    try {
      // Use the raw API client so a 409 (Outlook not connected) doesn't fire
      // useApiWithToast's generic error toast — we handle it as a fallback.
      await apiRaw.post("/api/outlook/send", {
        to: [composer.value.to],
        subject: composer.value.subject,
        body_html: bodyHtml,
        attachments: atts,
      });
      try {
        const result = await api.post(`/api/estimates/${route.params.id}/mark-sent`, {});
        estimate.value.status = _titleCase(result?.status || "sent");
      } catch { /* status flip best-effort */ }
      toast.add({
        severity: "success",
        summary: "Sent",
        detail: `Estimate emailed to ${composer.value.to}. Check your Sent folder.`,
        life: 5000,
      });
      showComposer.value = false;
    } catch (err) {
      const status = err?.status || err?.response?.status;
      if (status === 409) {
        // Outlook not connected for this user — fall back to mailto + download.
        toast.add({
          severity: "info",
          summary: "Opening your mail client",
          detail: "Outlook isn't connected for this user — using your default mail client instead.",
          life: 5000,
        });
        await _emailViaMailtoFallback(composer.value, atts[0]);
        showComposer.value = false;
      } else {
        toast.add({ severity: "error", summary: "Send failed", detail: err?.message || "Outlook rejected the send", life: 5000 });
      }
    }
  } finally {
    composerSending.value = false;
  }
}

async function _emailViaMailtoFallback(c, pdfAtt) {
  // Save PDF locally so user can drag-attach.
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
    const result = await api.post(`/api/estimates/${route.params.id}/mark-sent`, {});
    estimate.value.status = _titleCase(result?.status || "sent");
  } catch { /* ignore */ }
}

// 2026-05-12 audit — Accept / Decline / Convert all commit state changes
// that downstream surfaces (customer portal, scheduling, billing) read
// immediately. One-click misses on the button row used to flip an estimate
// without a chance to back out. Gate behind confirmDestructive().
function acceptEstimate() {
  confirmDestructive({
    message: `Mark ${estimate.value?.label || "this estimate"} as Accepted? The customer-portal view flips immediately.`,
    header: "Accept Estimate",
    icon: "pi pi-check",
    acceptClass: "p-button-success",
    acceptLabel: "Accept",
    rejectLabel: "Cancel",
    accept: () => doAcceptEstimate(),
  });
}

async function doAcceptEstimate() {
  try {
    const result = await api.post(`/api/estimates/${route.params.id}/accept`, {});
    estimate.value.status = _titleCase(result?.status || "accepted");
    // Accept can auto-convert server-side; without hydrating job_id the
    // Convert button stays visible and a second click 409s.
    const jobId = result?.auto_converted_job_id || result?.job_id;
    if (jobId) estimate.value.job_id = jobId;
    toast.add({ severity: "success", summary: "Accepted", detail: "Estimate accepted", life: 3000 });
  } catch (err) {
    toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to accept", life: 3000 });
  }
}

function declineEstimate() {
  confirmDestructive({
    message: `Mark ${estimate.value?.label || "this estimate"} as Declined? The customer-portal view flips immediately.`,
    header: "Decline Estimate",
    icon: "pi pi-times",
    acceptClass: "p-button-danger",
    acceptLabel: "Decline",
    rejectLabel: "Cancel",
    accept: () => doDeclineEstimate(),
  });
}

async function doDeclineEstimate() {
  try {
    const result = await api.post(`/api/estimates/${route.params.id}/decline`, {});
    estimate.value.status = _titleCase(result?.status || "declined");
    toast.add({ severity: "warn", summary: "Declined", detail: "Estimate declined", life: 3000 });
  } catch (err) {
    toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to decline", life: 3000 });
  }
}

function convertToJob() {
  confirmDestructive({
    message: `Convert ${estimate.value?.label || "this estimate"} into a new job? This creates a job record and navigates away.`,
    header: "Convert to Job",
    icon: "pi pi-briefcase",
    acceptClass: "p-button-success",
    acceptLabel: "Create Job",
    rejectLabel: "Cancel",
    accept: () => doConvertToJob(),
  });
}

async function doConvertToJob() {
  converting.value = true;
  try {
    const result = await api.post(`/api/estimates/${route.params.id}/convert-to-job`, {});
    estimate.value.status = "Converted";
    const jobId = result?.job_id || result?.data?.job_id;
    if (jobId) estimate.value.job_id = jobId;
    toast.add({ severity: "success", summary: "Converted", detail: "Estimate converted to job", life: 3000 });
    if (jobId) router.push(`/jobs/${jobId}`);
  } catch (err) {
    if (err?.status === 409) {
      // Stale tab: the estimate was already converted elsewhere.
      toast.add({ severity: "info", summary: "Already converted", detail: "This estimate already has a job.", life: 4000 });
      await fetchEstimate();
    } else {
      toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to convert to job", life: 3000 });
    }
  } finally {
    converting.value = false;
  }
}

function copyPublicLink() {
  const url = `${window.location.origin}/portal/estimate/${route.params.id}`;
  navigator.clipboard.writeText(url).then(() => {
    toast.add({ severity: "info", summary: "Copied", detail: "Public link copied to clipboard", life: 2000 });
  }).catch(() => {
    toast.add({ severity: "warn", summary: "Copy failed", detail: url, life: 5000 });
  });
}

function duplicateEstimate() {
  const sourceNumber = estimate.value?.estimate_number || "this estimate";
  confirmDestructive({
    message: `Create a new draft estimate from ${sourceNumber}? The duplicate copies the customer, jobsite address, line items, and notes. It starts unattached to a job — edit jobsite or lines before sending.`,
    header: "Duplicate Estimate",
    icon: "pi pi-copy",
    // Not a delete — keep the accept button primary, not the wrapper's
    // danger default.
    acceptClass: "p-button-primary",
    acceptLabel: "Duplicate",
    rejectLabel: "Cancel",
    accept: () => doDuplicateEstimate(),
  });
}

async function doDuplicateEstimate() {
  duplicating.value = true;
  try {
    const result = await api.post(`/api/estimates/${route.params.id}/duplicate`, {});
    const newId = result?.id || result?.data?.id;
    const newNumber = result?.estimate_number || result?.data?.estimate_number || "new estimate";
    toast.add({ severity: "success", summary: "Duplicated", detail: `Created ${newNumber}`, life: 3000 });
    if (newId) router.push(`/estimates/${newId}`);
  } catch (err) {
    toast.add({ severity: "error", summary: "Error", detail: err.message || "Failed to duplicate estimate", life: 4000 });
  } finally {
    duplicating.value = false;
  }
}

onMounted(async () => {
  // Slice 3 — refresh "Saved Xs ago" label every 10s. onUnmounted is not
  // strictly necessary because the route component lives for the page,
  // but be polite — clear if Vue tears us down.
  _autosaveTickInterval = setInterval(() => { _autosaveTick.value++; }, 10000);
  await loadCustomers();
  loadPricingTiers();
  loadPricingSettings();
  loadPricingCategories();
  loadEstimateFeatures();
  _discoverEstimateSource();   // plugin hook (ADR-013) — no-op when none installed
  await loadTenantTaxDefault();
  if (isExisting.value) {
    await fetchEstimate();
  } else {
    // New estimate: prefill tax rate with tenant default once it loads.
    form.value.tax_rate = tenantDefaultTaxPct.value;
    loading.value = false;
  }
  // Pre-select customer when /estimates/new?customer_id=… is visited.
  if (!isExisting.value) {
    const cid = route?.query?.customer_id;
    if (cid && customers.value.some((c) => String(c.id) === String(cid))) {
      form.value.customer_id = String(cid);
    }
  }
});

onUnmounted(() => {
  if (_autosaveTickInterval) clearInterval(_autosaveTickInterval);
  if (_autosaveDebounce) clearTimeout(_autosaveDebounce);
  if (_autosaveDraftTimer) clearTimeout(_autosaveDraftTimer);
});
</script>

<style scoped>
.estimate-view {
  max-width: 1280px;
  margin: 0 auto;
}

/* Captured-door folder explorer (plugin picker) */
.captured-hint { margin: 0 0 0.5rem; color: var(--p-text-color-secondary, #6b7280); }
.captured-folders { list-style: none; margin: 0; padding: 0; }
.captured-folder {
  display: flex; align-items: center; gap: 0.6rem; padding: 0.55rem 0.5rem;
  border-radius: 6px; cursor: pointer; border: 1px solid transparent;
}
.captured-folder:hover { background: rgba(128, 128, 128, 0.12); border-color: var(--surface-border, #ccc); }
.captured-folder__name { flex: 1; }
.captured-folder__count {
  font-size: 0.8rem; color: var(--p-text-color-secondary, #6b7280);
  background: rgba(128, 128, 128, 0.15); border-radius: 10px; padding: 0 0.5rem;
}
.captured-folder__chev { color: var(--p-text-color-secondary, #6b7280); }
.captured-breadcrumb { margin-left: 0.5rem; font-weight: 600; }

/* Slice 3 — autosave status pill in the action bar. */
.autosave-status {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.3rem 0.7rem;
  border-radius: 999px;
  font-size: 0.85rem;
  font-weight: 500;
  background: var(--p-content-hover-background);
  color: var(--p-text-muted-color);
}
.autosave-status.autosave-saving,
.autosave-status.autosave-creating {
  background: var(--p-blue-50);
  color: var(--p-blue-700);
}
.autosave-status.autosave-saved {
  background: var(--p-green-50);
  color: var(--p-green-700);
}
.autosave-status.autosave-error {
  background: var(--p-red-50);
  color: var(--p-red-700);
}

.page-header { margin-bottom: 1rem; }
.title-row { display: flex; align-items: center; gap: 0.75rem; margin-top: 0.25rem; flex-wrap: wrap; }
.page-title { margin: 0; font-size: 1.4rem; font-weight: 700; }
.header-meta {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  font-size: 0.875rem;
  color: var(--p-text-muted-color, #6b7280);
  margin-top: 0.4rem;
}
.header-meta .customer-name { color: var(--text-primary, inherit); font-weight: 600; }
.meta-sep { opacity: 0.5; }

/* Banner shown when the estimate has already been converted to a job.
   Translucent blue tint (not a palette-50 solid) so it reads correctly on
   both light and dark themes. */
.converted-banner {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  margin-top: 0.6rem;
  padding: 0.45rem 0.9rem;
  border-radius: 8px;
  font-size: 0.9rem;
  background: color-mix(in srgb, var(--p-blue-500, #3b82f6) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--p-blue-500, #3b82f6) 45%, transparent);
  color: var(--text-primary, inherit);
}
.converted-banner .pi-briefcase { color: var(--p-blue-500, #3b82f6); }
.converted-banner .p-button { padding: 0 0.25rem; }

.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
}

.form-field {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.form-field.full-width { grid-column: 1 / -1; }

.form-field label {
  font-size: 0.8rem;
  font-weight: 600;
  color: var(--p-text-muted-color, #6b7280);
  text-transform: uppercase;
}

.w-full { width: 100%; }

.line-items-editor {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  overflow-x: auto;
}

.line-item-header,
.line-item-row {
  display: grid;
  grid-template-columns: 64px 120px minmax(160px, 1fr) 70px 110px 110px 80px 90px 36px;
  gap: 0.5rem;
  align-items: center;
  min-width: 840px;
}

/* Reorder + delete controls share the first column. */
.line-controls { display: flex; align-items: center; justify-content: center; gap: 1px; }
.line-reorder { display: flex; flex-direction: column; }
.line-reorder :deep(.p-button) { width: 1.4rem; height: 1.05rem; padding: 0; }
.line-reorder :deep(.p-button .p-button-icon) { font-size: 0.7rem; }

.line-item-header {
  font-size: 0.75rem;
  font-weight: 600;
  text-transform: uppercase;
  color: var(--p-text-muted-color, #6b7280);
  padding-bottom: 0.25rem;
}

.col-margin { text-align: right; font-size: 0.85em; color: var(--text-muted, var(--p-text-muted-color)); }
.col-total { text-align: right; }
.col-action { display: flex; align-items: center; justify-content: center; }

.line-item-row :deep(.p-inputnumber),
.line-item-row :deep(.p-inputnumber input),
.line-item-row :deep(.p-select),
.line-item-row :deep(.p-inputtext) {
  width: 100%;
  min-width: 0;
}

.line-total-display,
.line-margin-display {
  font-weight: 600;
  font-size: 0.875rem;
  align-self: center;
}

.line-item-buttons {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.totals-and-profit {
  display: flex;
  gap: 16px;
  align-items: flex-start;
  flex-wrap: wrap;
  margin-top: 0.5rem;
}
.totals-and-profit .estimate-summary { flex: 1; min-width: 260px; max-width: 360px; margin-left: auto; }

.summary-row {
  display: flex;
  justify-content: space-between;
  padding: 0.35rem 0;
  font-size: 0.95rem;
}
.summary-value { font-weight: 600; }
.summary-value.discount { color: #ef4444; }
.total-row { font-size: 1.15rem; }
.summary-value.total {
  font-weight: 700;
  color: var(--p-primary-color, #3b82f6);
  font-size: 1.25rem;
}

.customer-contact {
  margin-top: 0.5rem;
  padding: 0.625rem 0.75rem;
  border: 1px solid var(--border-subtle, #dee2e6);
  border-radius: 6px;
  background: var(--surface-elevated, var(--p-content-hover-background));
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  font-size: 0.85rem;
}
.customer-contact .contact-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  color: var(--text-primary, inherit);
}
.customer-contact .contact-row i {
  color: var(--text-muted, var(--p-text-muted-color));
  font-size: 0.85rem;
}

.toggle-row { display: flex; align-items: center; gap: 0.5rem; margin-top: 0.25rem; }
.toggle-label { font-size: 0.85rem; color: var(--p-text-muted-color); }
.new-client-section {
  border: 1px solid var(--surface-border, var(--border-subtle, #dee2e6));
  border-radius: 8px;
  padding: 0.75rem;
  background: var(--surface-elevated, var(--surface-card, #ffffff));
}
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem; }

.page-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 1.25rem;
}

.loading-spinner { padding: 4rem 0; text-align: center; }

/* Save-to-catalog dialog */
.save-catalog-form { display: flex; flex-direction: column; gap: 12px; }
.save-catalog-form .form-row-stack { display: flex; flex-direction: column; gap: 4px; }
.save-catalog-form .form-row-stack label { font-size: 0.85em; font-weight: 600; color: var(--text-primary, inherit); }
.save-catalog-form .muted { color: var(--text-muted, var(--p-text-muted-color)); font-size: 0.8em; }

@media (max-width: 768px) {
  .form-grid { grid-template-columns: 1fr; }
  .line-item-header { display: none; }
  .totals-and-profit { flex-direction: column; }
  .totals-and-profit > * { width: 100%; }
}

.hidden-file-input { display: none; }
.attachment-actions { display: flex; align-items: center; gap: 0.75rem; margin: 0.5rem 0; }
.attachment-list { display: flex; flex-direction: column; gap: 0.5rem; margin-top: 0.75rem; }
.attachment-item {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.5rem;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 6px;
}
.attachment-thumb {
  background: none;
  border: 0;
  padding: 0;
  cursor: pointer;
}
.attachment-thumb img {
  width: 56px;
  height: 56px;
  object-fit: cover;
  border-radius: 4px;
  display: block;
}
.attachment-icon-small { font-size: 1.5rem; color: #6b7280; }
.composer-loading { padding: 2rem; text-align: center; color: #6b7280; }
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
.attachment-icon { font-size: 2rem; color: #b91c1c; width: 56px; text-align: center; }
.attachment-meta { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.attachment-name { font-weight: 600; color: var(--p-primary-color, #0057a8); text-decoration: none; word-break: break-word; }
.attachment-name:hover { text-decoration: underline; }
</style>
