<template>
  <section class="mobile-job-detail">
    <header class="detail-head">
      <Button
        icon="pi pi-arrow-left"
        text
        rounded
        aria-label="Back"
        data-testid="mobile-job-detail-back"
        @click="goBack"
      />
      <h1>Job details</h1>
    </header>

    <div v-if="loading" class="state-msg">
      <i class="pi pi-spin pi-spinner" />
      <span>Loading…</span>
    </div>
    <div v-else-if="error" class="state-msg state-msg-error">
      <i class="pi pi-exclamation-triangle" />
      <span>{{ error }}</span>
      <Button label="Retry" size="small" outlined @click="load" />
    </div>

    <template v-else-if="job">
      <div class="detail-card">
        <div class="detail-row detail-row-top">
          <div class="detail-customer" data-testid="mobile-job-detail-customer">
            {{ customer?.name || '—' }}
          </div>
          <span :class="['status-pill', `status-${(job.dispatch_status || 'assigned').replace(' ', '_')}`]">
            {{ statusLabel(job.dispatch_status) }}
          </span>
        </div>
        <div v-if="job.title" class="detail-title">{{ job.title }}</div>
        <div v-if="job.scheduled_at" class="detail-meta">
          <i class="pi pi-calendar" />
          {{ formatScheduled(job.scheduled_at) }}
        </div>
        <div v-else class="detail-meta detail-meta-muted">
          <i class="pi pi-calendar" />
          No date — do when in the area
        </div>
      </div>

      <!-- Why there are no action buttons. Without this, a view-only job
           reads as broken — the 2026-08-17 field report was a tech staring at his
           own new job while every tap said "job not found". -->
      <div v-if="readOnly" class="readonly-banner" data-testid="mjd-readonly-banner">
        <i class="pi pi-lock" />
        <span v-if="accessGrant === 'creator'">
          You created this job but it isn't assigned to you — dispatch will
          schedule and assign it. View only until then.
        </span>
        <span v-else>View only — this job isn't assigned to you.</span>
      </div>

      <!-- PR A: job context the Today card always showed and this screen never
           did. A tech reaching a job from the Jobs list saw no priority, no
           dog-in-the-yard alert, and no note that this is a second trip. -->
      <div
        v-if="job.priority && job.priority !== 'Normal' || job.is_return_visit || (job.alerts && job.alerts.length)"
        class="job-context"
        data-testid="mjd-job-context"
      >
        <Tag
          v-if="job.priority && job.priority !== 'Normal'"
          :value="job.priority"
          :severity="job.priority === 'Emergency' || job.priority === 'High' ? 'danger' : 'warn'"
        />
        <Tag
          v-if="job.is_return_visit"
          value="Return visit"
          severity="warn"
          data-testid="mjd-return-visit"
        />
        <Tag
          v-for="alert in job.alerts || []"
          :key="alert"
          :value="alert.replace(/_/g, ' ')"
          severity="warn"
        />
      </div>

      <div class="detail-card">
        <h2>Customer</h2>
        <!-- Customer-level warnings (dog, gate code, call-ahead). Carried on
             the nested customer, same as Today's card reads it. -->
        <div
          v-if="customer?.notes"
          class="customer-notes"
          data-testid="mjd-customer-notes"
        >
          <i class="pi pi-info-circle" />
          <span>{{ customer.notes }}</span>
        </div>
        <a
          v-if="customer?.phone"
          class="contact-row"
          :href="`tel:${customer.phone}`"
          data-testid="mobile-job-detail-phone"
        >
          <i class="pi pi-phone" />
          <span>{{ customer.phone }}</span>
        </a>
        <!-- The JOBSITE, not necessarily the customer's address: a job bound
             to a customer_locations row is somewhere else, and this row is
             what the tech navigates from. site_address is server-resolved
             (core/job_site.py); a bound site with no address says so instead
             of silently pointing at the HQ. -->
        <a
          v-if="displaySiteAddress"
          class="contact-row"
          :href="navigationLink"
          target="_blank"
          rel="noopener"
          data-testid="mobile-job-detail-address"
        >
          <i class="pi pi-map-marker" />
          <span>
            <span v-if="job.site_label" class="site-label" data-testid="mjd-site-label">{{ job.site_label }}</span>
            {{ displaySiteAddress }}
          </span>
        </a>
        <div
          v-else-if="job?.site_address_missing"
          class="contact-row site-missing"
          data-testid="mjd-site-address-missing"
        >
          <i class="pi pi-map-marker" />
          <span>
            <span v-if="job.site_label" class="site-label">{{ job.site_label }}</span>
            No address on this site — ask dispatch
          </span>
        </div>
        <div
          v-if="job?.site_access_notes"
          class="contact-row site-access-notes"
          data-testid="mjd-site-access-notes"
        >
          <i class="pi pi-key" />
          <span>{{ job.site_access_notes }}</span>
        </div>
        <!-- When the jobsite differs from the customer record, keep the
             customer's own address visible but clearly secondary. -->
        <div
          v-if="customerAddressDiffers"
          class="contact-row contact-row-sub site-customer-address"
          data-testid="mjd-customer-address-secondary"
        >
          <i class="pi pi-home" />
          <span>Customer address: {{ customer.address }}</span>
        </div>

        <!-- The tech is the one standing at the real address (jobsite plan
             PR 4) — same driveway-fix philosophy as the email/contact
             buttons below. Hidden on read-only grants: write access rides
             the job. -->
        <div v-if="!readOnly && !fixSiteOpen" class="contact-actions">
          <Button
            :label="displaySiteAddress ? 'Fix address' : 'Add address'"
            icon="pi pi-pencil"
            size="small"
            text
            data-testid="mjd-fix-address"
            @click="openFixSite"
          />
        </div>
        <div v-if="fixSiteOpen" class="contact-form" data-testid="mjd-fix-address-form">
          <!-- No confirm dialog on the updates-every-job option:
               useDestructiveConfirm auto-accepts silently (issue #215), so
               the honest label IS the guard. -->
          <label class="fix-site-option">
            <input
              type="radio"
              value="source"
              v-model="fixSite.applyTo"
              data-testid="mjd-fix-source"
            />
            <span>{{ fixSourceLabel }}</span>
          </label>
          <label class="fix-site-option">
            <input
              type="radio"
              value="new_site"
              v-model="fixSite.applyTo"
              data-testid="mjd-fix-new-site"
            />
            <span>This job is at a different place — save as a new site for this job only</span>
          </label>
          <InputText
            v-model="fixSite.address"
            placeholder="Street, city"
            data-testid="mjd-fix-address-input"
          />
          <div class="contact-form-actions">
            <Button label="Cancel" text size="small" @click="fixSiteOpen = false" />
            <Button
              label="Save address"
              size="small"
              :disabled="!fixSite.address.trim() || fixSiteBusy"
              :loading="fixSiteBusy"
              data-testid="mjd-fix-address-save"
              @click="saveFixSite"
            />
          </div>
        </div>
        <a
          v-if="customer?.email"
          class="contact-row"
          :href="`mailto:${customer.email}`"
          data-testid="mobile-job-detail-email"
        >
          <i class="pi pi-envelope" />
          <span>{{ customer.email }}</span>
        </a>

        <!-- The people at this account beyond the one name on the record: a
             property manager, a front desk, whoever actually answers. They
             follow the customer, so the next tech on the next job sees them. -->
        <ul v-if="contacts.length" class="contact-list" data-testid="mjd-contact-list">
          <li v-for="c in contacts" :key="c.id">
            <div class="contact-who">
              <span class="contact-name">{{ c.name }}</span>
              <span v-if="c.label" class="contact-label">{{ c.label }}</span>
            </div>
            <a v-if="c.phone" class="contact-row contact-row-sub" :href="`tel:${c.phone}`">
              <i class="pi pi-phone" />
              <span>{{ c.phone }}</span>
            </a>
            <a v-if="c.email" class="contact-row contact-row-sub" :href="`mailto:${c.email}`">
              <i class="pi pi-envelope" />
              <span>{{ c.email }}</span>
            </a>
          </li>
        </ul>

        <div v-if="!customer?.phone && !displaySiteAddress && !job?.site_address_missing" class="detail-meta detail-meta-muted">
          No contact info on file — you're the one who can fix that.
        </div>

        <!-- The tech is the only person standing in front of the customer, so
             they're the only one who can fill in what's missing. 219 of 382
             customers have no email at all. -->
        <div v-if="!contactFormOpen" class="contact-actions">
          <Button
            v-if="!customer?.email"
            label="Add email"
            icon="pi pi-envelope"
            size="small"
            text
            data-testid="mjd-add-email"
            @click="openContactForm('email')"
          />
          <Button
            label="Add contact"
            icon="pi pi-user-plus"
            size="small"
            text
            data-testid="mjd-add-contact"
            @click="openContactForm('contact')"
          />
        </div>

        <div v-else class="contact-form">
          <template v-if="contactFormMode === 'email'">
            <InputText
              v-model="emailDraft"
              type="email"
              inputmode="email"
              maxlength="254"
              placeholder="Customer email"
              data-testid="mjd-email-input"
            />
          </template>
          <template v-else>
            <InputText v-model="contactDraft.name" maxlength="200" placeholder="Name" data-testid="mjd-contact-name" />
            <InputText v-model="contactDraft.phone" type="tel" inputmode="tel" maxlength="50" placeholder="Phone" data-testid="mjd-contact-phone" />
            <InputText v-model="contactDraft.label" maxlength="120" placeholder="Who they are (optional)" data-testid="mjd-contact-label" />
          </template>
          <div class="contact-form-actions">
            <Button label="Cancel" size="small" text severity="secondary" @click="closeContactForm" />
            <Button
              label="Save"
              icon="pi pi-check"
              size="small"
              :loading="contactBusy"
              :disabled="!contactFormValid"
              data-testid="mjd-contact-save"
              @click="saveContactForm"
            />
          </div>
        </div>
      </div>

      <div v-if="job.description" class="detail-card">
        <h2>Description</h2>
        <p class="detail-description">{{ job.description }}</p>
      </div>

      <!-- Install/build spec for the door(s) on this job, captured at quote
           time and carried on the linked estimate. Only renders when there IS
           a captured door — service calls and hand-built installs show nothing.
           Doors are listed by size; tap one to see its full build spec. -->
      <div v-if="doorSpecs.length" class="detail-card" data-testid="mjd-door-specs">
        <h2>Install Specs</h2>
        <DoorSpecList :doors="doorSpecs" />
      </div>

      <!-- PR A: the customer's installed equipment (door + opener specs).
           Collapsed by default and fetched on first expand — an install/service
           tech wants the unit details, but not at the cost of a GET on every
           job open. Gated on the customer being known, same as Today's card. -->
      <div v-if="customer?.id" class="detail-card">
        <h2
          class="equip-head"
          data-testid="mjd-equipment-toggle"
          @click="toggleEquipment"
        >
          <i class="pi pi-box" />
          Install &amp; equipment
          <i :class="['pi', equipOpen ? 'pi-chevron-up' : 'pi-chevron-down', 'equip-chevron']" />
        </h2>
        <template v-if="equipOpen">
          <div v-if="equipLoading" class="muted">Loading…</div>
          <ul v-else-if="(equipment || []).length" class="equip-list" data-testid="mjd-equipment-list">
            <li v-for="e in equipment" :key="e.id" class="equip-item">
              <div class="equip-line">
                <Tag
                  :value="equipTypeLabel(e.equipment_type)"
                  :severity="e.equipment_type === 'garage_door' ? 'info' : 'secondary'"
                />
                <strong>{{ equipTitle(e) }}</strong>
              </div>
              <div
                v-if="e.serial_number || e.installation_date || e.warranty_expires_on"
                class="equip-meta"
              >
                <span v-if="e.serial_number">S/N {{ e.serial_number }}</span>
                <span v-if="e.installation_date">Installed {{ e.installation_date }}</span>
                <span v-if="e.warranty_expires_on">Warranty → {{ e.warranty_expires_on }}</span>
              </div>
              <div v-if="e.notes" class="equip-notes">{{ e.notes }}</div>
            </li>
          </ul>
          <div v-else class="muted">No install/equipment on file for this site.</div>
        </template>
      </div>

      <!-- Always rendered, never `v-if="notes.length"`: the tech with nothing
           written yet is exactly the one who needs somewhere to write. -->
      <div class="detail-card">
        <h2>Notes</h2>
        <ul v-if="notes.length" class="note-list">
          <li v-for="n in notes" :key="n.id">
            <div class="note-body">{{ n.note }}</div>
            <div class="note-when">
              <span v-if="n._failed" class="failed-flag">
                <i class="pi pi-exclamation-triangle" /> didn't send — tap Add note to retry
              </span>
              <span v-else-if="n._pending" class="pending-flag">
                <i class="pi pi-cloud-upload" /> waiting for signal
              </span>
              <span v-else>
                <!-- Who wrote it matters: more than one tech works a job, and
                     "who found the frayed cable" is the next question. Omitted
                     entirely rather than shown as "Unknown" when we genuinely
                     don't know — the office screen's `|| 'Unknown'` read as a
                     display default and hid the fact that NOT ONE note in
                     production had an author recorded. -->
                <span v-if="n.author_name" class="note-author">{{ n.author_name }}</span>
                <span v-if="n.author_name"> · </span>
                <span>{{ formatScheduled(n.created_at) }}</span>
              </span>
            </div>
          </li>
        </ul>
        <div v-else class="detail-meta detail-meta-muted">No notes yet.</div>

        <!-- readOnly (company-wide browsing): composers hidden — the write
             endpoints would 404 a tech with no claim on this job anyway. -->
        <template v-if="!readOnly">
          <Textarea
            v-model="noteDraft"
            rows="2"
            auto-resize
            placeholder="What did you find?"
            data-testid="mjd-note-input"
          />
          <Button
            label="Add note"
            icon="pi pi-plus"
            size="small"
            :loading="noteBusy"
            :disabled="!noteDraft.trim()"
            data-testid="mjd-note-add"
            @click="addNote"
          />
        </template>
      </div>

      <div class="detail-card">
        <div class="photo-head">
          <h2>Photos</h2>
          <span v-if="pendingPhotos" class="photo-pending" data-testid="mjd-photo-pending">
            <i class="pi pi-cloud-upload" />
            {{ pendingPhotos }} waiting for signal
          </span>
        </div>

        <div v-if="photos.length" class="photo-strip">
          <!-- AuthedImage, not a bare <img>: the url needs a Bearer token
               and an <img src> can't send one — it 401s and paints a broken
               icon, which is exactly what a real phone showed. -->
          <div v-for="p in photos" :key="p.id" class="photo-thumb">
            <AuthedImage :src="p.url" :alt="p.caption || p.filename || 'Job photo'">
              <template #fallback>
                <span class="photo-name">{{ p.filename || 'Photo' }}</span>
              </template>
            </AuthedImage>
          </div>
        </div>
        <div v-else class="detail-meta detail-meta-muted">No photos yet.</div>

        <!-- A real file input, not a Button — only an input can open the
             camera. Deliberately NO `capture` attribute: Android honours it by
             forcing a single shot straight to the lens, which kills `multiple`
             AND locks the tech out of the gallery, so a photo taken before the
             app was open can never be attached. Bare accept="image/*" makes
             Android offer Camera or Files, which is both. -->
        <label v-if="!readOnly" class="photo-add" data-testid="mjd-photo-add">
          <input
            ref="photoInput"
            type="file"
            accept="image/*"
            multiple
            @change="onPhotoPicked"
          />
          <span>
            <i class="pi pi-camera" />
            {{ photoBusy ? 'Saving…' : 'Add photo' }}
          </span>
        </label>
      </div>

      <div class="detail-card">
        <h2>Parts</h2>

        <!-- Installed on this job, logged as the work happens (2026-08-12).
             Listed FIRST and apart from requests: "I put this in" and "please
             order me this" are different facts, and the old card could only
             say the second one — parts used had to wait for the closeout
             form, so a tech either held them in their head all afternoon or
             typed them into a note, where nothing bills them. -->
        <div v-if="usedParts.length" class="part-group" data-testid="mjd-used-list">
          <h3 class="part-group-title">Installed on this job</h3>
          <ul class="part-list">
            <li v-for="p in usedParts" :key="p.id">
              <div class="part-main">
                <span class="part-name">{{ p.part_name }}</span>
                <span class="part-qty">×{{ p.quantity || 1 }}</span>
              </div>
              <div class="part-meta">
                <span v-if="p.sku" class="part-sku">{{ p.sku }}</span>
                <span v-if="p._failed" class="failed-flag">
                  <i class="pi pi-exclamation-triangle" /> didn't send
                </span>
                <span v-else-if="p._pending" class="pending-flag">
                  <i class="pi pi-cloud-upload" /> waiting for signal
                </span>
                <span v-else-if="p.billed_invoice_id" class="part-status">billed</span>
                <span v-else class="part-status part-status-used">used</span>
                <!-- Undo, while the office hasn't billed it. A tap on a phone
                     mis-taps; without this the wrong part rides all the way to
                     the customer's invoice. -->
                <button
                  v-if="!readOnly && !p._pending && !p._failed && !p.billed_invoice_id"
                  type="button"
                  class="part-undo"
                  :disabled="partUndoBusy === p.id"
                  data-testid="mjd-part-undo"
                  @click="undoUsedPart(p)"
                >
                  <i class="pi pi-times" /> Remove
                </button>
              </div>
            </li>
          </ul>
        </div>

        <div v-if="requestedParts.length" class="part-group" data-testid="mjd-part-list">
          <h3 v-if="usedParts.length" class="part-group-title">Requested</h3>
          <ul class="part-list">
            <li v-for="p in requestedParts" :key="p.id">
              <div class="part-main">
                <span class="part-name">{{ p.part_name }}</span>
                <span class="part-qty">×{{ p.quantity || 1 }}</span>
              </div>
              <div class="part-meta">
                <span v-if="p.sku" class="part-sku">{{ p.sku }}</span>
                <span v-if="p.urgency === 'urgent'" class="part-urgent">urgent</span>
                <span v-if="p._failed" class="failed-flag">
                  <i class="pi pi-exclamation-triangle" /> didn't send
                </span>
                <span v-else-if="p._pending" class="pending-flag">
                  <i class="pi pi-cloud-upload" /> waiting for signal
                </span>
                <span v-else class="part-status">{{ p.status || 'needed' }}</span>
              </div>
            </li>
          </ul>
        </div>

        <div v-if="!parts.length" class="detail-meta detail-meta-muted">
          No parts logged or requested yet.
        </div>

        <div v-if="!readOnly" class="part-add">
          <!-- Catalog chips, straight from /api/catalogs — never a hardcoded
               list. Custom catalogs are per-tenant data: every business running
               GDX Dispatch defines its own set and renames or adds to them
               whenever it likes, so the only correct list is the one the API
               returns right now. Same source the estimate builder's picker
               reads, so the two can't drift apart. -->
          <div v-if="catalogs.length" class="catalog-chips" data-testid="mjd-catalog-chips">
            <button
              type="button"
              :class="['chip', { 'chip-on': !partCatalogId }]"
              @click="pickCatalog(null)"
            >
              All
            </button>
            <button
              v-for="c in catalogs"
              :key="c.id"
              type="button"
              :class="['chip', { 'chip-on': partCatalogId === c.id }]"
              @click="pickCatalog(c.id)"
            >
              {{ c.name }}
            </button>
          </div>

          <!-- Search is the same catalog the estimate builder searches. Typing
               is never blocked on it: offline, or for a part nobody has
               catalogued, the free-text name is submitted with sku=null and the
               request still reaches dispatch. -->
          <!-- maxlength mirrors JobPartNeeded.part_name String(200). Without
               it a long free-text name 422s — loudly online, and SILENTLY
               offline, where the queue marks it failed and the request is
               simply gone. Stop it at the keyboard instead. -->
          <InputText
            v-model="partQuery"
            maxlength="200"
            placeholder="Search parts, or just type a name"
            data-testid="mjd-part-search"
            @input="onPartQuery"
          />
          <ul v-if="partSuggestions.length" class="suggest-list" data-testid="mjd-part-suggestions">
            <li v-for="s in partSuggestions" :key="`${s.source}-${s.sku}-${s.name}`">
              <button type="button" @click="pickPart(s)">
                <span class="suggest-name">{{ s.name }}</span>
                <span class="suggest-meta">
                  <span v-if="s.sku">{{ s.sku }}</span>
                  <!-- Which list it came off. Two catalogs can carry the same
                       part at different prices, so the catalog is the
                       difference between the right part and the wrong one. -->
                  <span v-if="s.catalog" class="suggest-catalog">{{ s.catalog }}</span>
                  <span v-if="s.qty_on_hand != null" class="suggest-stock">
                    {{ s.qty_on_hand }} on hand
                  </span>
                </span>
              </button>
            </li>
          </ul>
          <div
            v-else-if="partQuery.trim().length >= 2 && !partSearching"
            class="detail-meta detail-meta-muted"
            data-testid="mjd-part-nomatch"
          >
            Nothing in the catalog matches — Request sends what you typed.
          </div>

          <div v-if="partQuery.trim()" class="part-controls">
            <InputNumber
              v-model="partQty"
              :min="1"
              :max="99"
              show-buttons
              button-layout="horizontal"
              data-testid="mjd-part-qty"
            >
              <template #incrementbuttonicon><i class="pi pi-plus" /></template>
              <template #decrementbuttonicon><i class="pi pi-minus" /></template>
            </InputNumber>
            <!-- Urgency belongs to an ORDER, not to a part already in the
                 door: Request sends it, "Used it" ignores it. The composer
                 serves both verbs, so the control stays put and the label
                 says which one it's for rather than appearing and vanishing
                 under the tech's thumb. -->
            <div class="urgent-toggle">
              <Checkbox v-model="partUrgent" input-id="mjd-part-urgent" binary />
              <label for="mjd-part-urgent">Urgent (request)</label>
            </div>
            <!-- Two verbs, because the tech is answering two different
                 questions. "Used" is the one that was missing: it records the
                 part against the job now, and it's already billable — no
                 waiting for the closeout form. -->
            <div class="part-verbs">
              <Button
                label="Used it"
                icon="pi pi-check"
                size="small"
                :loading="partUsedBusy"
                :disabled="partBusy"
                data-testid="mjd-part-used"
                @click="addPartUsed"
              />
              <Button
                label="Request"
                icon="pi pi-plus"
                size="small"
                severity="secondary"
                outlined
                :loading="partBusy"
                :disabled="partUsedBusy"
                data-testid="mjd-part-add"
                @click="addPart"
              />
            </div>
          </div>
        </div>
      </div>

      <!-- Time is shown, never edited here. Arriving starts the job clock and
           completing ends it; that path is the one PR #154 actually guards. A
           Stop button would close the timer and switch that guard off — an
           attested 2h job then bills 5h. Read-only until that is fixed and
           proven on Postgres. -->
      <!-- Also shown when a closeout exists with no arrival stamp (a job
           entered after the fact, or a dispatcher closing for a tech) — the
           attested hours are the point of this card, not the arrival. -->
      <div v-if="job.arrived_at || job.closeout" class="detail-card">
        <h2>Time</h2>
        <div v-if="job.arrived_at" class="detail-meta" data-testid="mobile-job-detail-timer">
          <i class="pi pi-clock" />
          <!-- Deliberately NOT "arrived → completed". A job arrived at in May
               and closed out in July spans two months, and labelling that span
               "tracked" reads as two months of work — the hours the tech
               actually attested at closeout were 1.5. Never imply a duration
               from two stamps that don't bound the work. -->
          <span v-if="job.completed_at">
            Arrived {{ formatScheduled(job.arrived_at) }} · closed out {{ formatScheduled(job.completed_at) }}
          </span>
          <span v-else>Tracking since you arrived, {{ formatScheduled(job.arrived_at) }}</span>
        </div>
        <!-- Plan §1: this card used to NAME the source of the hours and then
             withhold the number — job_closeouts was write-only. Now the
             attested figure and the submitted notes come back so the tech can
             confirm what he sent. -->
        <div v-if="job.closeout" class="detail-meta" data-testid="mobile-closeout-summary">
          <i class="pi pi-check-circle" />
          <span>
            You attested {{ Number(job.closeout.hours_worked).toFixed(2) }} h
            {{ job.closeout.no_parts_used ? '· no parts used' : `· ${job.closeout.parts_count} part line(s)` }}
          </span>
        </div>
        <div v-if="job.closeout && job.closeout.notes" class="detail-meta detail-meta-muted" data-testid="mobile-closeout-notes">
          "{{ job.closeout.notes }}"
        </div>
        <div class="detail-meta detail-meta-muted">
          Hours for this job come from what you entered at close-out. Your paid
          hours come from the day clock.
        </div>
      </div>

      <!-- PR A. These live INLINE, not in the sticky bar, and the browser walk
           is why. Adding three buttons to the floating bar pushed it to three
           rows: it then covered the equipment list, and the FAB sat on top of
           Chat so the button could not be tapped at all. The bar's own comment
           already records it covering four of six part suggestions — it is a
           narrow budget and status actions own it. Quote / change order / chat
           are deliberate, not thumb-urgent, so they sit in the page flow, which
           is also exactly where Today's card puts them. -->
      <div
        v-if="!readOnly"
        class="detail-card secondary-actions"
        data-testid="mjd-secondary-actions"
      >
        <h2>More actions</h2>
        <div class="secondary-actions-row">
          <Button
            v-if="['en_route','on_site','done'].includes(job.dispatch_status)"
            :label="latestActiveQuote ? 'Show quote' : 'Build quote'"
            :icon="latestActiveQuote ? 'pi pi-file' : 'pi pi-pencil'"
            severity="secondary"
            outlined
            :loading="quotesLoading"
            data-testid="mjd-quote"
            @click="openQuote"
          />
          <Button
            v-if="['on_site','done'].includes(job.dispatch_status)"
            label="Change order"
            icon="pi pi-file-plus"
            severity="secondary"
            outlined
            data-testid="mjd-change-order"
            @click="changeOrderOpen = true"
          />
          <Button
            label="Chat"
            icon="pi pi-comment"
            severity="secondary"
            outlined
            data-testid="mjd-chat"
            @click="chatOpen = true"
          />
        </div>
      </div>

      <!-- Sticky so the tech can act without scrolling a long job.
           Hidden for as long as the part composer is open — while suggestions
           show AND while a picked/typed name is pending submit. This bar
           floats over that whole area: on a real phone it covered four of six
           suggestions (a tap meant for the third landed on "On my way",
           firing a dispatch action the tech never chose), and once a part was
           picked it covered the Request button itself. Raising the composer's
           z-index doesn't fix it — the app's bottom nav is a separate stacking
           context and still wins — but yielding the space does, and a tech
           mid-compose isn't reaching for these buttons anyway. -->
      <!-- readOnly: company-wide browsing (techs_see_all_jobs) — the tech
           has no claim on this job, so dispatch actions would only 404. -->
      <div
        v-if="!partComposerOpen && !readOnly"
        class="action-bar"
        data-testid="mobile-job-detail-actions"
      >
        <Button
          v-if="canGoEnRoute"
          label="On my way"
          icon="pi pi-send"
          :loading="advancing"
          data-testid="mjd-en-route"
          @click="onMyWay"
        />
        <Button
          v-if="job.dispatch_status === 'en_route'"
          label="I'm here"
          icon="pi pi-map-marker"
          :loading="advancing"
          data-testid="mjd-arrived"
          @click="imHere"
        />
        <Button
          v-if="job.dispatch_status === 'on_site'"
          label="Complete"
          icon="pi pi-check"
          severity="success"
          data-testid="mjd-complete"
          @click="closeoutOpen = true"
        />
        <Button
          v-if="canBill"
          label="Bill / collect"
          icon="pi pi-receipt"
          severity="secondary"
          data-testid="mjd-bill"
          @click="invoiceOpen = true"
        />
        <Button
          v-if="job.navigation_link"
          label="Navigate"
          icon="pi pi-directions"
          severity="secondary"
          outlined
          data-testid="mjd-navigate"
          @click="openMaps"
        />
      </div>

      <!-- Deposit state (2026-07-23): the truck needs to know money already
           moved before quoting a collect-on-completion number. -->
      <div v-if="job.deposit" class="deposit-banner" data-testid="mjd-deposit">
        <i class="pi pi-wallet" />
        <span v-if="job.deposit.deposit_balance > 0">
          Deposit: ${{ (job.deposit.deposit_paid || 0).toFixed(2) }} paid of
          ${{ (job.deposit.deposit_total || 0).toFixed(2) }} —
          ${{ job.deposit.deposit_balance.toFixed(2) }} still due
        </span>
        <span v-else>
          Deposit paid: ${{ (job.deposit.deposit_paid || 0).toFixed(2) }} — comes off the final bill
        </span>
      </div>

      <MobileJobCloseoutDialog
        v-model:visible="closeoutOpen"
        :job-id="String(job.id)"
        :job-title="job.title || ''"
        :job-type="job.job_type || ''"
        :customer-name="customer?.name || ''"
        @closed-out="onCloseoutDone"
      />
      <MobileInvoiceDialog
        v-model:visible="invoiceOpen"
        :job="job"
        @invoiced="refresh"
      />
      <MobileQuoteBuilderDialog
        v-model:visible="quoteBuilderOpen"
        :job="job"
        @saved="onQuoteBuilt"
        @present="presentQuote"
      />
      <MobileCustomerQuoteDialog
        v-model:visible="customerQuoteOpen"
        :quote="customerQuote"
        @accepted="onQuoteAccepted"
        @declined="onQuoteDeclined"
      />
      <MobileChangeOrderDialog
        v-model:visible="changeOrderOpen"
        :job-id="String(job.id)"
        :job-title="job.title || customer?.name || ''"
        :customer-id="customer?.id || null"
        :customer-name="customer?.name || ''"
      />
      <MobileChatDialog
        v-model:visible="chatOpen"
        :job="job"
      />
    </template>
  </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import Button from 'primevue/button'
import Checkbox from 'primevue/checkbox'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'
import Textarea from 'primevue/textarea'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'
import { queuedWriteStatus, useOfflineSync } from '../composables/useOfflineSync'
import { usePhotoQueue } from '../composables/usePhotoQueue'
import AuthedImage from '../components/AuthedImage.vue'
import DoorSpecList from '../components/DoorSpecList.vue'
import MobileJobCloseoutDialog from '../components/MobileJobCloseoutDialog.vue'
import MobileInvoiceDialog from '../components/MobileInvoiceDialog.vue'
// PR A (one-job-card plan): the quote / change-order / chat / equipment
// surfaces existed ONLY on Today's route card. A tech reaching a job any other
// way — the Jobs list, a notification, an unscheduled "in the area" job — could
// not build a quote, raise a change order, or message dispatch about it at all.
import MobileQuoteBuilderDialog from '../components/MobileQuoteBuilderDialog.vue'
import MobileCustomerQuoteDialog from '../components/MobileCustomerQuoteDialog.vue'
import MobileChangeOrderDialog from '../components/MobileChangeOrderDialog.vue'
import MobileChatDialog from '../components/MobileChatDialog.vue'

const api = useApi()
const toast = useToast()
const route = useRoute()
const router = useRouter()
const { pendingPhotos, capturePhoto } = usePhotoQueue()

// Registers the queue's `online` + `visibilitychange` drain listeners for as
// long as this screen is mounted, and tears them down after.
//
// They live in useOfflineSync()'s onMounted, and until now MobileTodayView was
// the ONLY caller in the app — so a tech who queued a note, a part request or
// an arrival from this screen and then regained signal drained nothing: the
// writes sat in IndexedDB until they happened to navigate back to Today.
// Caught on a real phone by watching a note stay "waiting for signal" with the
// wifi back on. syncNow() guards on a module-level `syncing` ref, so a second
// caller can't double-drain.
const { pendingCount } = useOfflineSync()

const photoInput = ref(null)
const photoBusy = ref(false)

const loading = ref(true)
const error = ref(null)
const job = ref(null)
const notes = ref([])
const photos = ref([])
const parts = ref([])
// Captured door build spec(s) for an install, carried from the estimate. Empty
// for service calls — the section only renders when there's a door to show.
const doorSpecs = ref([])
// True when the server granted view-only access: company-wide browsing
// (techs_see_all_jobs) or the creator of a still-unassigned job — hide
// dispatch actions, they'd only 404. accessGrant carries WHY ("company" |
// "creator" | "assigned" | "manager") so the banner can explain instead of
// leaving the tech staring at a job he just created with no buttons on it.
const readOnly = ref(false)
const accessGrant = ref('')
const advancing = ref(false)
const closeoutOpen = ref(false)
const invoiceOpen = ref(false)

// ─── PR A: quote / change order / chat / equipment ──────────────────
// Every one of these loads ON DEMAND, never at mount. test_mobile_job_cards
// mocks api.get with mockResolvedValueOnce — exactly once — so an extra
// mount-time GET resolves undefined and throws (July plan, trap #3).
const quoteBuilderOpen = ref(false)
const customerQuoteOpen = ref(false)
const customerQuote = ref(null)
const quotes = ref(null)          // null = never fetched, [] = fetched, empty
const quotesLoading = ref(false)
const changeOrderOpen = ref(false)
const chatOpen = ref(false)
const equipOpen = ref(false)
const equipment = ref(null)       // null = never fetched
const equipLoading = ref(false)

const noteDraft = ref('')
const noteBusy = ref(false)

// ─── Fix the jobsite address (PR 4) ─────────────────────────────────
const fixSiteOpen = ref(false)
const fixSiteBusy = ref(false)
const fixSite = reactive({ address: '', applyTo: 'source' })
// Where "fix it" lands, in the tech's words — routed server-side to the row
// the displayed address actually came from.
const fixSourceLabel = computed(() => {
  const src = job.value?.site_source
  if (src === 'location') {
    return `Fix this site's address${job.value?.site_label ? ` (${job.value.site_label})` : ''} — updates this site for all its jobs`
  }
  if (src === 'customer_location') {
    return "Fix the primary site's address — updates every job using it"
  }
  return "Fix the customer's address"
})

function openFixSite() {
  fixSite.address = displaySiteAddress.value || ''
  fixSite.applyTo = 'source'
  fixSiteOpen.value = true
}

async function saveFixSite() {
  if (!fixSite.address.trim() || fixSiteBusy.value) return
  fixSiteBusy.value = true
  try {
    const r = await api.patchQueued(
      `/api/mobile/jobs/${job.value.id}/site`,
      {
        address: fixSite.address.trim(),
        apply_to: fixSite.applyTo,
        // What the tech was LOOKING AT — text AND target. A stale offline
        // replay must not clobber a newer fix, and equal text must not
        // route to a row the tech was never shown (server 422s on either).
        expected_address: displaySiteAddress.value || null,
        expected_source: job.value?.site_source || null,
      },
      { actionType: 'job.site_fix', resourceId: String(job.value.id) },
    )
    fixSiteOpen.value = false
    if (r?.queued) {
      // Show the tech's OWN input as pending rather than re-rendering the
      // stale server state — a screen that still says the old address reads
      // as "didn't take" and invites a second, self-conflicting queue entry
      // (post-code audit §5). Reconciles on the post-drain refresh.
      job.value.site_address = fixSite.address.trim()
      toast.add({ severity: 'warn', summary: 'Saved offline', detail: 'Will apply when you have signal — the address shown is your pending fix.', life: 4000 })
    } else {
      toast.add({ severity: 'success', summary: 'Address updated', life: 2500 })
      await refresh()
    }
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Could not update the address', detail: err?.message || '', life: 4000 })
  } finally {
    fixSiteBusy.value = false
  }
}

const contactFormOpen = ref(false)
const contactFormMode = ref('contact')
const contactBusy = ref(false)
const emailDraft = ref('')
const contactDraft = ref({ name: '', phone: '', label: '' })

// Contacts ride on the customer in the job payload.
const contacts = computed(() => customer.value?.contacts || [])

const contactFormValid = computed(() => {
  if (contactFormMode.value === 'email') {
    // Deliberately not a strict RFC pattern — a tech typing what's on the work
    // order shouldn't be argued with by a regex. The server bounds the length;
    // an address with an @ and a dot is the bar for "worth saving".
    const e = emailDraft.value.trim()
    return e.length >= 5 && e.includes('@') && e.includes('.')
  }
  // A name alone is a real contact: "ask for Jim at the front desk" is worth
  // recording even before anyone has his number.
  return contactDraft.value.name.trim().length > 0
})

function openContactForm(mode) {
  contactFormMode.value = mode
  emailDraft.value = customer.value?.email || ''
  contactDraft.value = { name: '', phone: '', label: '' }
  contactFormOpen.value = true
}

function closeContactForm() {
  contactFormOpen.value = false
  emailDraft.value = ''
  contactDraft.value = { name: '', phone: '', label: '' }
}

async function saveContactForm() {
  if (!contactFormValid.value || contactBusy.value) return
  contactBusy.value = true
  const isEmail = contactFormMode.value === 'email'
  try {
    if (isEmail) {
      // patchQueued/postQueued: a tech gets the email at the door, which is
      // exactly where the signal isn't. The write survives the dead zone.
      await api.patchQueued(
        `/api/mobile/jobs/${job.value.id}/customer`,
        { email: emailDraft.value.trim() },
        { actionType: 'job.customer_contact', resourceId: String(job.value.id) },
      )
    } else {
      const d = contactDraft.value
      await api.postQueued(
        `/api/mobile/jobs/${job.value.id}/customer/contacts`,
        {
          name: d.name.trim(),
          phone: d.phone.trim() || null,
          label: d.label.trim() || null,
        },
        { actionType: 'job.customer_contact', resourceId: String(job.value.id) },
      )
    }
    await refresh()
    toast.add({
      severity: 'success',
      summary: isEmail ? 'Email saved' : 'Contact added',
      life: 2500,
    })
    closeContactForm()
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: isEmail ? 'Could not save email' : 'Could not add contact',
      detail: err?.message || '',
      life: 4000,
    })
  } finally {
    contactBusy.value = false
  }
}

const partQuery = ref('')
const partSku = ref(null)
// Only an inventory-sourced suggestion carries a real parts.id. Catalog rows
// (custom catalogs, CHI feeds) have none — those record as free text, exactly
// like a free-text closeout line. Sending a catalog id here would violate
// job_parts.part_id's FK to parts.id.
const partPartId = ref(null)
const partQty = ref(1)
const partUrgent = ref(false)
const partBusy = ref(false)
const partUsedBusy = ref(false)
const partUndoBusy = ref(null)
const partSearching = ref(false)
const partSuggestions = ref([])

// The card's two answers, split by which write made the row. 'mobile' is this
// screen's live-capture write; 'request' is the order queue. The server sends
// only those two sources (mobile.py) — a closeout's own rows are deliberately
// not here, because a re-closeout rewrites them under the tech.
const usedParts = computed(() => parts.value.filter((p) => p.source === 'mobile'))
const requestedParts = computed(() => parts.value.filter((p) => p.source !== 'mobile'))
// One screenful and a bit. The list scrolls inside itself; a tech narrows with
// the search box rather than thumbing a whole catalog.
const BROWSE_PAGE_SIZE = 25
const catalogs = ref([])
const partCatalogId = ref(null)
let partSearchTimer = null
let partSearchSeq = 0

// The customer rides on the job (same shape the Today cards read), so the
// actions can reach job.customer without caring which screen mounted them.
const customer = computed(() => job.value?.customer || null)

// True from the first keystroke in the part search until the request is filed
// or the field is cleared — i.e. exactly while the suggestion list or the
// qty/urgent/Request row is on screen and needs the sticky bar out of the way.
const partComposerOpen = computed(
  () => partSuggestions.value.length > 0 || partQuery.value.trim().length > 0,
)

async function load() {
  loading.value = true
  error.value = null
  try {
    const r = await api.get(`/api/mobile/job/${route.params.id}`)
    job.value = r?.job || null
    notes.value = r?.notes || []
    photos.value = r?.photos || []
    parts.value = r?.parts || []
    doorSpecs.value = r?.door_specs || []
    readOnly.value = Boolean(r?.read_only)
    accessGrant.value = r?.access_grant || ''
    if (!job.value) error.value = 'Job not found'
  } catch (err) {
    // The ownership gate 404s jobs that aren't yours — same message either way.
    error.value = err?.status === 404 ? 'Job not found' : (err?.message || 'Could not load job')
  } finally {
    loading.value = false
  }
}

// After an action, NOT on first paint. A refetch that fails must never take the
// job off the screen: `error` out-ranks `job` in the template, so routing a
// dead-zone refetch through load() means the tech taps "On my way", is told
// "Saved offline", and then watches the job vanish — the write succeeded and
// the screen broke anyway. Keep what we have; the queue will drain later.
async function refresh() {
  try {
    const r = await api.get(`/api/mobile/job/${route.params.id}`)
    if (r?.job) {
      job.value = r.job
      notes.value = await withStillQueued(r.notes || [], notes.value)
      photos.value = r.photos || []
      parts.value = await withStillQueued(r.parts || [], parts.value)
      doorSpecs.value = r.door_specs || []
    }
  } catch {
    // Offline or a blip. The queued write still lands on reconnect.
  }
}

/**
 * Server rows + the optimistic rows whose own write hasn't landed yet.
 *
 * The server list is authoritative for everything it knows about, but it
 * cannot know about a write still sitting in the queue. Overwriting wholesale
 * would erase a note the tech wrote in a dead zone the moment any later
 * refresh runs — the write is safe in the queue, but it looks lost, which is
 * the same failure the vanishing-job split above exists to prevent.
 *
 * A row is dropped only when ITS OWN key leaves the queue, so it can never
 * linger beside the server's copy of itself. A write the server REJECTED stays
 * on screen flagged "didn't send": it isn't coming back on a refresh, and
 * quietly deleting the tech's work is worse than showing it failed.
 */
async function withStillQueued(serverRows, currentRows) {
  const local = (currentRows || []).filter((r) => r._pending || r._failed)
  if (!local.length) return serverRows
  const survivors = []
  for (const row of local) {
    const state = await queuedWriteStatus(row._key)
    if (state === 'waiting') survivors.push({ ...row, _pending: true, _failed: false })
    else if (state === 'failed') survivors.push({ ...row, _pending: false, _failed: true })
    // null → it landed; the server row above is the real one.
  }
  return [...serverRows, ...survivors]
}

const navigationLink = computed(() => job.value?.navigation_link || null)
// Effective site to display. Server payloads always carry site_address now,
// but a payload cached OFFLINE before this field existed has neither
// site_address nor site_address_missing — degrade to the customer address
// there (the pre-field behavior) rather than blanking the row in a dead zone.
const displaySiteAddress = computed(() => {
  if (job.value?.site_address) return job.value.site_address
  if (job.value?.site_address_missing) return null
  return customer.value?.address || null
})
// The customer's own address is worth a secondary line ONLY when the job's
// site is somewhere else — same address twice is noise in a driveway.
const customerAddressDiffers = computed(() => {
  const cust = (customer.value?.address || '').trim()
  if (!cust) return false
  const site = (job.value?.site_address || '').trim()
  if (!site) return Boolean(job.value?.site_address_missing)
  return site !== cust
})

const canGoEnRoute = computed(() => {
  const s = job.value?.dispatch_status
  return !s || s === 'assigned' || s === 'unassigned'
})

// Today's guards are dispatch_status-only, which is safe there because Today is
// only ever today. This screen opens ANY job, including one invoiced in April —
// a status-only guard would cheerfully offer to bill it again.
//
// `billed` is derived server-side from real invoices. Do NOT reach for
// job.billing_status: it looks like the answer and is a dead column that only
// ever says "unbilled" (core/billing_predicates.py).
// Requires an explicit false: if the server didn't say, we don't know, and
// inviting a second invoice is the one mistake here that costs money.
const canBill = computed(() => {
  const j = job.value
  // not_billable (055): the office dismissed this job from Ready-for-Billing
  // — don't nudge the tech to invoice it. Loose !== true on purpose: an old
  // server that doesn't send the key must not hide the button.
  return Boolean(j) && j.dispatch_status === 'done' && j.billed === false && j.not_billable !== true
})

function openMaps() {
  if (navigationLink.value) window.open(navigationLink.value, '_blank', 'noopener')
}

// ─── PR A: quotes ───────────────────────────────────────────────────
// Same contract as Today's card: fetch on first tap, then either present the
// live quote to the customer or open the builder. Nothing is fetched until the
// tech asks, so this adds no mount-time GET.
async function ensureQuotesLoaded() {
  if (quotes.value !== null) return
  if (!job.value?.id) return
  quotesLoading.value = true
  try {
    const data = await api.get(`/api/mobile/jobs/${job.value.id}/quote`)
    quotes.value = data.quotes || []
  } catch (err) {
    // Leave quotes null so the next tap retries rather than silently
    // insisting there are none — "no quote" and "couldn't ask" are different
    // answers and the tech is standing in front of the customer.
    quotes.value = null
    toast.add({ severity: 'error', summary: 'Could not load quotes', detail: err?.message || '', life: 4000 })
  } finally {
    quotesLoading.value = false
  }
}
const latestActiveQuote = computed(() => (quotes.value || []).find(q => q.status !== 'declined') || null)
const acceptedQuote = computed(() => (quotes.value || []).find(q => q.status === 'accepted') || null)

async function openQuote() {
  await ensureQuotesLoaded()
  if (quotes.value === null) return   // load failed; toast already shown
  const q = latestActiveQuote.value
  if (q) presentQuote(q)
  else quoteBuilderOpen.value = true
}
function presentQuote(quote) {
  customerQuote.value = quote
  customerQuoteOpen.value = true
}
function onQuoteBuilt(quote) {
  quotes.value = [quote, ...(quotes.value || [])]
}
function patchQuote(updated) {
  const list = quotes.value || []
  const i = list.findIndex(q => q.id === updated.id)
  if (i >= 0) quotes.value = list.map((q, n) => (n === i ? { ...q, ...updated } : q))
}
function onQuoteAccepted(updated) {
  patchQuote(updated)
  toast.add({ severity: 'success', summary: 'Customer accepted', life: 2500 })
  // Acceptance can make the job billable — re-read rather than infer.
  refresh()
}
function onQuoteDeclined(updated) {
  patchQuote(updated)
}

// ─── PR A: installed equipment ──────────────────────────────────────
const EQUIP_TYPE_LABELS = {
  garage_door: 'Door',
  opener: 'Opener',
  gate: 'Gate',
  other: 'Equipment',
}
function equipTypeLabel(t) {
  return EQUIP_TYPE_LABELS[t] || 'Equipment'
}
function equipTitle(e) {
  const parts = [e.manufacturer, e.model].filter(Boolean).join(' ')
  return parts || equipTypeLabel(e.equipment_type)
}
async function toggleEquipment() {
  equipOpen.value = !equipOpen.value
  if (!equipOpen.value) return
  const cid = customer.value?.id
  if (!cid || equipment.value !== null) return
  equipLoading.value = true
  try {
    const r = await api.get(`/api/customers/${cid}/equipment`)
    equipment.value = Array.isArray(r) ? r : r?.items || r?.data || []
  } catch {
    // equipment_tracking is an optional module — fail quiet, show "none".
    equipment.value = []
  } finally {
    equipLoading.value = false
  }
}

// Queued, not posted: a tech taps these in driveways and dead zones. postQueued
// lands the row locally and drains on reconnect; a 4xx still throws (a real
// answer is not an outage).
// The status flip happens AFTER the queued write resolves and BEFORE the
// refetch, and that ordering is the whole fix.
//
// This used to refetch instead of flipping, on the stated grounds that "Today
// flips the status before checking the result and never rolls it back". That
// premise is false: MobileTodayView's onMyWay/imHere both assign
// dispatch_status *after* their `await postQueued(...)`, inside the try, so a
// throw skips the flip. Today never had the bug this was avoiding — and
// avoiding it cost the tech the one thing that actually matters in a garage.
//
// What it cost: offline, postQueued QUEUES the write and resolves {queued:true}
// — the tap is durably recorded. Then refresh() threw into the outer catch, so
// the tech got "Saved offline" AND "Could not save" together and the button
// still read "On my way". Today, which flips, worked. Same job, two answers,
// depending only on which screen you opened it from.
//
// So: a real 4xx still throws before the flip (no false advance). A queued
// write flips. The refetch is best-effort and cannot undo a durable write or
// contradict the toast the tech just read.
async function advance(path, body, actionType, okMsg, nextStatus) {
  advancing.value = true
  try {
    const r = await api.postQueued(`/api/mobile/jobs/${job.value.id}/${path}`, body, {
      actionType, resourceId: String(job.value.id),
    })
    if (job.value && nextStatus) job.value.dispatch_status = nextStatus
    if (r?.queued) {
      toast.add({ severity: 'warn', summary: 'Saved offline', detail: 'Sends when you have signal', life: 3000 })
    } else {
      toast.add({ severity: 'success', summary: okMsg, life: 2000 })
    }
    try {
      await refresh()
    } catch {
      // No signal. The write is queued and the local flip already reflects it;
      // a second error toast here would just contradict "Saved offline".
    }
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Could not save', detail: err?.message || '', life: 4000 })
  } finally {
    advancing.value = false
  }
}

function onMyWay() {
  return advance('en-route', {}, 'job.en_route', 'On my way', 'en_route')
}

async function imHere() {
  return advance('arrived', await currentPosition(), 'job.arrived', "You're here", 'on_site')
}

// Best-effort: a tech in a metal building may never get a fix, and arriving
// must not depend on it.
function currentPosition() {
  return new Promise((resolve) => {
    if (!navigator.geolocation) return resolve({})
    const done = (v) => resolve(v)
    const timer = setTimeout(() => done({}), 3000)
    navigator.geolocation.getCurrentPosition(
      (p) => { clearTimeout(timer); done({ lat: p.coords.latitude, lng: p.coords.longitude, accuracy: p.coords.accuracy }) },
      () => { clearTimeout(timer); done({}) },
      { timeout: 3000 },
    )
  })
}

function onCloseoutDone() {
  closeoutOpen.value = false
  refresh()
}

// ─── Notes ───────────────────────────────────────────────────────────────
async function addNote() {
  const body = noteDraft.value.trim()
  if (!body || noteBusy.value) return
  noteBusy.value = true
  try {
    // The mobile endpoint, not the office one at /api/jobs/{id}/notes. Both
    // write JobNote.body and both work; this one goes through the same
    // _assert_job_access gate as the rest of this screen, which is the reason
    // to prefer it.
    //
    // (It also *tries* to record author_name for per-tech attribution — but
    // resolves it from name/full_name/email on the user dict, and the JWT
    // carries only sub/role/tenant_id, so it always writes NULL. Verified
    // against a real technician token: the column is empty for every note in
    // the DB. Don't count that feature as working.)
    //
    // Its field is `note`; the office endpoint's is `body`; the read side
    // aliases the column back (SELECT body AS note). Three names, one column —
    // so what you post is not what you get back. Posting the wrong one 422s.
    const r = await api.postQueued(
      `/api/mobile/jobs/${job.value.id}/notes`,
      { note: body },
      { actionType: 'job.note', resourceId: String(job.value.id) },
    )
    if (r?.queued) {
      // Show it now, flagged. It is safe in the queue; the tech needs to see
      // that what they wrote wasn't dropped.
      notes.value = [
        ...notes.value,
        { id: `pending-${r.idempotency_key}`, note: body, _pending: true, _key: r.idempotency_key },
      ]
      toast.add({ severity: 'info', summary: 'Saved offline', detail: 'Sends when you have signal', life: 3000 })
    } else {
      await refresh()
    }
    noteDraft.value = ''
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Could not add note', detail: err?.message || '', life: 4000 })
  } finally {
    noteBusy.value = false
  }
}

// ─── Parts ───────────────────────────────────────────────────────────────
function onPartQuery() {
  // Typing a name by hand is a valid request on its own, so clear any SKU the
  // tech previously picked — otherwise an edited name keeps riding the old
  // sku and dispatch orders the wrong part.
  partSku.value = null
  const q = partQuery.value.trim()
  clearTimeout(partSearchTimer)
  // With a catalog picked, an empty box means "show me that catalog". Search
  // alone is not enough: a tenant names its items however it likes, and one
  // real catalog here is 77 items specced like "207X2.000X20.0 Left" — not one
  // of which contains the word its catalog is named after. Browsing is the
  // interaction; typing only narrows. Without a catalog there is nothing to
  // browse, so wait for a couple of characters.
  if (!partCatalogId.value && q.length < 2) {
    partSuggestions.value = []
    return
  }
  partSearchTimer = setTimeout(() => searchParts(q), 250)
}

/**
 * A catalog row in the shape pickSuggestion/addPart expect.
 *
 * source stays 'catalog' so part_id is never set: job_parts.part_id is an FK to
 * parts.id and a custom_catalog_items id would violate it. sku is legitimately
 * empty on catalog rows — a whole catalog of real parts here has none — so the
 * request rides on the name alone, which the create endpoint accepts.
 */
function _catalogRowToSuggestion(row, catalogName) {
  return {
    source: 'catalog',
    catalog_id: partCatalogId.value,
    catalog: catalogName,
    sku: row.sku || null,
    name: row.name || row.description || row.sku || '',
    vendor: row.vendor || null,
    category: row.category || null,
    qty_on_hand: null,
  }
}

async function searchParts(q) {
  const seq = ++partSearchSeq
  partSearching.value = true
  try {
    let rows
    if (partCatalogId.value) {
      // Browsing one catalog: the estimate builder's own endpoint — paginated,
      // searchable within the catalog, and it already handles the virtual CHI
      // feeds. Reused rather than reimplemented so the tech's list and the
      // estimate's list cannot drift apart.
      const cat = catalogs.value.find((c) => c.id === partCatalogId.value)
      const r = await api.get(
        `/api/catalogs/${encodeURIComponent(partCatalogId.value)}/items`
          + `?search=${encodeURIComponent(q)}&per_page=${BROWSE_PAGE_SIZE}`,
        { suppressErrorToast: true },
      )
      rows = (r?.items || []).map((row) => _catalogRowToSuggestion(row, cat?.name))
    } else {
      // No catalog picked: search across all of them at once.
      const r = await api.get(
        `/api/parts-needed/sku-suggest?q=${encodeURIComponent(q)}&limit=6`,
        { suppressErrorToast: true },
      )
      rows = Array.isArray(r) ? r : []
    }
    // A slow earlier request must not overwrite a newer one's results.
    if (seq !== partSearchSeq) return
    partSuggestions.value = rows
  } catch {
    // Offline, or the search is down. Free-text still works — say nothing and
    // let the tech type.
    if (seq === partSearchSeq) partSuggestions.value = []
  } finally {
    if (seq === partSearchSeq) partSearching.value = false
  }
}

/**
 * The catalogs the tech can search, exactly as the server lists them.
 *
 * Never hardcoded: catalogs are tenant data that someone adds and removes from
 * the Catalogs page, so the only correct list is the one the API returns today.
 * Failure is silent on purpose — search still works across everything without
 * the chips, and a toast about catalogs while a tech is trying to order a
 * spring is noise.
 */
async function loadCatalogs() {
  try {
    const r = await api.get('/api/catalogs', { suppressErrorToast: true })
    catalogs.value = (Array.isArray(r) ? r : [])
      .filter((c) => c?.id && c?.name)
      .map((c) => ({ id: String(c.id), name: String(c.name) }))
  } catch {
    catalogs.value = []
  }
}

function pickCatalog(id) {
  partCatalogId.value = id
  clearTimeout(partSearchTimer)
  const q = partQuery.value.trim()
  // Tapping a catalog lists it immediately — that IS the interaction, because
  // the items aren't findable by typing their category. Tapping "All" with an
  // empty box has nothing to show, so clear.
  if (id) searchParts(q)
  else if (q.length >= 2) searchParts(q)
  else partSuggestions.value = []
}

function pickPart(s) {
  partQuery.value = s.name || s.sku || ''
  partSku.value = s.sku || null
  // See partPartId: inventory rows only. A catalog row's id is not a parts.id.
  partPartId.value = s.source === 'parts' ? (s.part_id || null) : null
  partSuggestions.value = []
}

function clearPartComposer() {
  partQuery.value = ''
  partSku.value = null
  partPartId.value = null
  partQty.value = 1
  partUrgent.value = false
  partSuggestions.value = []
  partCatalogId.value = null
}

async function addPart() {
  const name = partQuery.value.trim()
  if (!name || partBusy.value) return
  partBusy.value = true
  // Clamp here, not just on the input. `:max` only clamps on blur, so a tech
  // who types a qty and taps Request without leaving the field submits whatever
  // is in it — and the server accepts anything up to 999 (PartNeededIn), so a
  // fat-fingered 267 would reach dispatch as a real order for 267 rollers. The
  // bound belongs on the path that sends the value.
  const qty = Math.min(99, Math.max(1, Math.trunc(Number(partQty.value) || 1)))
  const urgency = partUrgent.value ? 'urgent' : 'normal'
  try {
    const r = await api.postQueued(
      `/api/jobs/${job.value.id}/parts-needed`,
      { part_name: name, sku: partSku.value, quantity: qty, urgency },
      { actionType: 'job.part_needed', resourceId: String(job.value.id) },
    )
    if (r?.queued) {
      parts.value = [
        ...parts.value,
        {
          id: `pending-${r.idempotency_key}`,
          part_name: name,
          sku: partSku.value,
          quantity: qty,
          urgency,
          status: 'needed',
          _pending: true,
          _key: r.idempotency_key,
        },
      ]
      toast.add({ severity: 'info', summary: 'Saved offline', detail: 'Sends when you have signal', life: 3000 })
    } else {
      await refresh()
      toast.add({ severity: 'success', summary: 'Part requested', life: 2500 })
    }
    clearPartComposer()
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Could not request part', detail: err?.message || '', life: 4000 })
  } finally {
    partBusy.value = false
  }
}

/**
 * Log a part the tech has ALREADY installed, mid-job.
 *
 * The write that was missing (Doug 2026-08-12): parts used could only be
 * entered in the closeout form, so everything installed before the last five
 * minutes of the job had to be remembered — or typed into a note, where
 * nothing orders, counts, or bills it.
 *
 * Same billable spine as the closeout (job_parts_needed, status='used'), but
 * tagged source='mobile' so the two can't collide: a re-closeout replaces only
 * its OWN unbilled rows, and the closeout's require-parts gate counts these,
 * so nothing here has to be re-typed at completion.
 *
 * Queued like every other write on this screen — techs work in dead zones.
 */
async function addPartUsed() {
  const name = partQuery.value.trim()
  if (!name || partUsedBusy.value) return
  partUsedBusy.value = true
  // Same clamp rationale as addPart: `:max` only clamps on blur, so the bound
  // belongs on the path that sends the value.
  const qty = Math.min(99, Math.max(1, Math.trunc(Number(partQty.value) || 1)))
  try {
    const r = await api.postQueued(
      `/api/mobile/jobs/${job.value.id}/parts-used`,
      { parts: [{ part_id: partPartId.value, name, sku: partSku.value, qty }] },
      { actionType: 'job.part_used', resourceId: String(job.value.id) },
    )
    if (r?.queued) {
      parts.value = [
        ...parts.value,
        {
          id: `pending-${r.idempotency_key}`,
          part_name: name,
          sku: partSku.value,
          quantity: qty,
          status: 'used',
          source: 'mobile',
          _pending: true,
          _key: r.idempotency_key,
        },
      ]
      toast.add({ severity: 'info', summary: 'Saved offline', detail: 'Sends when you have signal', life: 3000 })
    } else {
      await refresh()
      toast.add({ severity: 'success', summary: 'Part logged', detail: `${name} ×${qty}`, life: 2500 })
    }
    clearPartComposer()
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Could not log part', detail: err?.message || '', life: 4000 })
  } finally {
    partUsedBusy.value = false
  }
}

/**
 * Take back a part logged by mistake. Not queued: this is a correction the
 * tech is watching, and a queued delete of a row the server may not have yet
 * is a race with no honest UI. Offline it fails loudly and stays on the list.
 */
async function undoUsedPart(p) {
  if (!p?.id || partUndoBusy.value) return
  partUndoBusy.value = p.id
  try {
    await api.del(`/api/mobile/jobs/${job.value.id}/parts-used/${p.id}`)
    parts.value = parts.value.filter((row) => row.id !== p.id)
    toast.add({ severity: 'success', summary: 'Removed', detail: p.part_name || '', life: 2500 })
    await refresh()
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Could not remove part', detail: err?.message || '', life: 4000 })
  } finally {
    partUndoBusy.value = null
  }
}


async function onPhotoPicked(e) {
  const files = Array.from(e?.target?.files || [])
  if (!files.length) return
  photoBusy.value = true
  let queued = 0
  try {
    for (const f of files) {
      const r = await capturePhoto(job.value.id, f)
      if (r?.queued) queued += 1
    }
    // The photo is SAVED either way — that's the point of storing the blob
    // before uploading. Say which happened; "Uploaded" when it's sitting in
    // IndexedDB is the lie that makes a tech re-shoot a door.
    if (queued) {
      toast.add({
        severity: 'warn',
        summary: queued === files.length ? 'Saved on your phone' : 'Some saved on your phone',
        detail: 'Uploads when you have signal',
        life: 3500,
      })
    } else {
      toast.add({ severity: 'success', summary: files.length > 1 ? 'Photos added' : 'Photo added', life: 2000 })
    }
    // The 201 carries no url — the strip can only render after a refetch.
    await refresh()
  } catch (err) {
    toast.add({
      severity: 'error',
      summary: err?.code === 'photo_backlog_full' ? 'Too many photos waiting' : 'Could not save photo',
      detail: err?.code === 'photo_backlog_full'
        ? 'Get some signal so these upload before adding more.'
        : (err?.message || ''),
      life: 5000,
    })
  } finally {
    photoBusy.value = false
    // Let the same file be picked again (Chrome won't re-fire change otherwise).
    if (photoInput.value) photoInput.value.value = ''
  }
}

function goBack() {
  if (window.history.length > 1) router.back()
  else router.push('/mobile/jobs')
}

function statusLabel(s) {
  return ({
    en_route: 'En route',
    on_site: 'On site',
    done: 'Done',
    unassigned: 'Unassigned',
    assigned: 'Assigned',
  })[s] || s || 'Assigned'
}

function formatScheduled(iso) {
  if (!iso) return ''
  try {
    return new Date(iso).toLocaleString([], {
      weekday: 'short', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
    })
  } catch { return iso }
}

// Draining is silent — it happens in the queue, not here — so without this the
// tech watches "waiting for signal" sit there after the write has already
// landed. Refetch whenever the queue shrinks: withStillQueued() then finds the
// key gone and swaps the optimistic row for the server's, timestamp and all.
watch(pendingCount, (now, before) => {
  if (now < before) refresh()
})

onMounted(() => {
  load()
  loadCatalogs()
})
</script>

<style scoped>
.mobile-job-detail { padding: 0.75rem; max-width: 800px; margin: 0 auto; display: flex; flex-direction: column; gap: 0.6rem; }
.detail-head { display: flex; align-items: center; gap: 0.25rem; }
.detail-head h1 { margin: 0; font-size: 1.25rem; font-weight: 700; }
.detail-card {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.6rem; padding: 0.85rem 1rem;
  display: flex; flex-direction: column; gap: 0.45rem;
}
.detail-card h2 { margin: 0; font-size: 0.8rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; color: var(--p-text-muted-color, #6b7280); }
.detail-row { display: flex; align-items: center; gap: 0.5rem; }
.detail-row-top { justify-content: space-between; }
.detail-customer { font-size: 1.1rem; font-weight: 700; }
.detail-title { font-size: 0.95rem; }
.detail-meta { color: var(--p-text-muted-color, #6b7280); font-size: 0.9rem; display: flex; align-items: center; gap: 0.35rem; }
.detail-meta-muted { font-style: italic; }
.detail-description { margin: 0; white-space: pre-wrap; font-size: 0.95rem; }
.contact-row {
  display: flex; align-items: center; gap: 0.5rem;
  color: var(--p-primary-color, #2563eb); text-decoration: none;
  font-size: 0.95rem; padding: 0.25rem 0;
}
.contact-list { list-style: none; margin: 0.4rem 0 0; padding: 0; }
.contact-list li {
  padding-top: 0.5rem; border-top: 1px solid var(--p-content-border-color, #e5e7eb);
}
.contact-who { display: flex; align-items: baseline; gap: 0.5rem; flex-wrap: wrap; }
.contact-name { font-weight: 600; font-size: 0.95rem; }
.contact-label {
  font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.02em;
  color: var(--p-text-muted-color, #9ca3af);
}
.contact-row-sub { min-height: 40px; }
.site-label {
  font-size: 0.72rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.02em; margin-right: 0.35rem;
  color: var(--p-primary-color, #2563eb);
  border: 1px solid currentColor; border-radius: 4px; padding: 0.05rem 0.3rem;
}
.site-missing { color: var(--p-orange-500, #f59e0b); font-style: italic; }
.site-access-notes { color: var(--p-text-color, #374151); }
.site-customer-address { color: var(--p-text-muted-color, #6b7280); font-size: 0.85rem; }
.fix-site-option { display: flex; align-items: flex-start; gap: 0.5rem; font-size: 0.9rem; cursor: pointer; }
.fix-site-option input { margin-top: 0.2rem; }
.contact-actions { display: flex; flex-wrap: wrap; gap: 0.4rem; margin-top: 0.4rem; }
.contact-actions :deep(.p-button) { min-height: 44px; }
.contact-form { display: flex; flex-direction: column; gap: 0.5rem; margin-top: 0.5rem; }
.contact-form :deep(.p-inputtext) { width: 100%; min-height: 44px; }
.contact-form-actions { display: flex; justify-content: flex-end; gap: 0.5rem; }
.contact-form-actions :deep(.p-button) { min-height: 44px; }
.note-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.6rem; }
.note-body { font-size: 0.95rem; white-space: pre-wrap; }
.note-when { font-size: 0.75rem; color: var(--p-text-muted-color, #9ca3af); }
.note-author { font-weight: 600; }
.photo-head { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.photo-pending {
  display: inline-flex; align-items: center; gap: 0.3rem;
  font-size: 0.75rem; font-weight: 600;
  color: var(--p-amber-600, #b45309);
}
/* A styled <label> wrapping a hidden file input — capture="environment" is
   what jumps straight to the back camera, and only a real input gets that. */
.photo-add {
  display: flex; align-items: center; justify-content: center;
  min-height: 44px; border-radius: 0.5rem; cursor: pointer;
  border: 1px dashed var(--p-content-border-color, #d1d5db);
  color: var(--p-primary-color, #2563eb);
  font-size: 0.95rem; font-weight: 600;
}
.photo-add input { position: absolute; width: 1px; height: 1px; opacity: 0; pointer-events: none; }
.photo-add span { display: inline-flex; align-items: center; gap: 0.4rem; }
.photo-strip { display: flex; gap: 0.5rem; overflow-x: auto; }
.photo-thumb { flex: 0 0 auto; width: 96px; height: 96px; border-radius: 0.4rem; overflow: hidden; border: 1px solid var(--p-content-border-color, #e5e7eb); display: flex; align-items: center; justify-content: center; }
.photo-thumb :deep(img) { width: 100%; height: 100%; object-fit: cover; }
.photo-name { font-size: 0.7rem; padding: 0.25rem; word-break: break-all; }
.status-pill {
  display: inline-flex; align-items: center; gap: 0.3rem;
  padding: 0.2rem 0.55rem; border-radius: 999px;
  font-size: 0.75rem; font-weight: 600; letter-spacing: 0.02em;
  border: 1px solid transparent;
}
.status-assigned { background: #475569; color: #fff; }
.status-unassigned { background: #6b7280; color: #fff; }
.status-en_route { background: #f59e0b; color: #1f2937; }
.status-on_site { background: #2563eb; color: #fff; }
.status-done { background: #15803d; color: #fff; }
.state-msg {
  text-align: center; padding: 2rem 1rem;
  color: var(--p-text-muted-color, #6b7280);
  display: flex; flex-direction: column; align-items: center; gap: 0.5rem;
}
.state-msg-error { color: #b91c1c; }
/* Sticky so a long job doesn't hide the actions. 44px is the tap-target floor
   from e2e/mobile-touch-targets.spec.js, which now opens the first job and
   walks this screen too — it previously only covered param-less routes, which
   is how the screen a tech works from went uncovered. */
.action-bar {
  position: sticky; bottom: 0; z-index: 5;
  display: flex; flex-wrap: wrap; gap: 0.5rem;
  padding: 0.6rem; margin: 0 -0.75rem -0.75rem;
  background: var(--p-content-background, #fff);
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
}
.action-bar:empty { display: none; }
.action-bar :deep(.p-button) { flex: 1 1 auto; min-height: 44px; }

/* Notes + parts composers. Every control here clears the same 44px tap floor
   the action bar does — a tech taps these wearing gloves. */
.detail-card :deep(.p-textarea) { width: 100%; margin-top: 0.6rem; }
.detail-card :deep(.p-inputtext) { width: 100%; min-height: 44px; }
.detail-card > :deep(.p-button) { margin-top: 0.5rem; min-height: 44px; }

.pending-flag {
  display: inline-flex; align-items: center; gap: 0.3rem;
  color: var(--p-primary-color, #3b82f6);
}
.failed-flag {
  display: inline-flex; align-items: center; gap: 0.3rem;
  color: var(--p-red-500, #ef4444); font-weight: 600;
}

.part-list { list-style: none; margin: 0 0 0.6rem; padding: 0; display: flex; flex-direction: column; gap: 0.6rem; }
.part-list li { border-bottom: 1px solid var(--p-content-border-color, #e5e7eb); padding-bottom: 0.5rem; }
.part-list li:last-child { border-bottom: 0; }
.part-main { display: flex; justify-content: space-between; gap: 0.5rem; font-size: 0.95rem; }
.part-name { font-weight: 500; }
.part-qty { color: var(--p-text-muted-color, #9ca3af); white-space: nowrap; }
.part-meta {
  display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center;
  font-size: 0.75rem; color: var(--p-text-muted-color, #9ca3af); margin-top: 0.15rem;
}
.part-sku { font-family: ui-monospace, monospace; }
.part-urgent {
  color: var(--p-red-500, #ef4444); font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.02em;
}
.part-status { text-transform: capitalize; }
/* Installed reads as settled, not as another thing waiting on the office.
   Both tokens are theme variables, so this holds in dark mode too. */
.part-status-used { color: var(--p-green-600, #16a34a); font-weight: 600; }

.part-group + .part-group { margin-top: 0.9rem; }
.part-group-title {
  margin: 0 0 0.4rem; font-size: 0.78rem; font-weight: 600;
  text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--p-text-muted-color, #9ca3af);
}
/* 44px tap floor like every other control on this screen — this one undoes a
   billable row, so a near-miss is expensive. */
.part-undo {
  margin-left: auto; min-height: 44px; padding: 0 0.5rem; cursor: pointer;
  border: 0; background: none; font: inherit; font-size: 0.75rem;
  color: var(--p-red-500, #ef4444);
}
.part-undo:disabled { opacity: 0.5; }

.part-add { display: flex; flex-direction: column; gap: 0.5rem; }
/* Chips scroll sideways rather than wrapping into a wall: the count is tenant
   data and grows whenever someone adds a catalog. */
.catalog-chips {
  display: flex; gap: 0.4rem; overflow-x: auto; padding-bottom: 0.2rem;
  scrollbar-width: none;
}
.catalog-chips::-webkit-scrollbar { display: none; }
.chip {
  flex: 0 0 auto; min-height: 36px; padding: 0 0.7rem; cursor: pointer;
  border: 1px solid var(--p-content-border-color, #e5e7eb); border-radius: 999px;
  background: var(--p-content-background, #fff); color: inherit;
  font: inherit; font-size: 0.8rem; white-space: nowrap;
}
.chip-on {
  background: var(--p-primary-color, #3b82f6);
  border-color: var(--p-primary-color, #3b82f6); color: #fff; font-weight: 600;
}
.suggest-list {
  list-style: none; margin: 0; padding: 0;
  border: 1px solid var(--p-content-border-color, #e5e7eb); border-radius: 6px;
  overflow: hidden;
  /* The catalog is 2,600 items; a long match list must scroll inside itself
     rather than push the Request button off-screen. */
  max-height: 40vh; overflow-y: auto;
}
.suggest-list button {
  width: 100%; min-height: 44px; text-align: left; cursor: pointer;
  display: flex; flex-direction: column; gap: 0.15rem;
  padding: 0.5rem 0.6rem; border: 0; border-bottom: 1px solid var(--p-content-border-color, #e5e7eb);
  background: var(--p-content-background, #fff); color: inherit; font: inherit;
}
.suggest-list li:last-child button { border-bottom: 0; }
.suggest-list button:active { background: var(--p-content-hover-background, #f3f4f6); }
.suggest-name { font-size: 0.9rem; }
.suggest-meta {
  display: flex; gap: 0.5rem; font-size: 0.72rem;
  color: var(--p-text-muted-color, #9ca3af); font-family: ui-monospace, monospace;
}
.suggest-stock { color: var(--p-green-600, #16a34a); font-family: inherit; }
.suggest-catalog { font-family: inherit; font-style: italic; }

.part-controls { display: flex; flex-wrap: wrap; gap: 0.6rem; align-items: center; }
.part-controls :deep(.p-inputnumber-input) { width: 3rem; text-align: center; }
.part-controls :deep(.p-inputnumber .p-button) { min-width: 44px; min-height: 44px; }
.urgent-toggle { display: flex; align-items: center; gap: 0.4rem; min-height: 44px; }
.urgent-toggle label { font-size: 0.9rem; }
.part-controls :deep(.p-button) { min-height: 44px; }
/* Both verbs stay on one row and never shrink below the tap floor; on a narrow
   phone they wrap together rather than splitting across the controls row. */
.part-verbs { display: flex; gap: 0.5rem; flex: 1 1 auto; }
.part-verbs :deep(.p-button) { flex: 1 1 auto; justify-content: center; }

.deposit-banner {
  display: flex; align-items: center; gap: 0.5rem;
  margin-top: 0.6rem; padding: 0.55rem 0.75rem;
  border-radius: 0.5rem; font-size: 0.85rem;
  background: var(--p-highlight-background, #eff6ff);
  border: 1px solid var(--p-content-border-color, #bfdbfe);
}

.readonly-banner {
  display: flex; align-items: flex-start; gap: 0.5rem;
  padding: 0.55rem 0.75rem;
  border-radius: 0.5rem; font-size: 0.85rem;
  background: var(--p-highlight-background, #eff6ff);
  border: 1px solid var(--p-content-border-color, #bfdbfe);
  color: var(--p-text-muted-color, #6b7280);
}
.readonly-banner .pi { margin-top: 0.1rem; }

/* ── PR A: job context, customer warnings, installed equipment ────────
   Theme tokens throughout, never literal colors: this screen is used in a
   dark garage and in a bright driveway, and jsdom applies no media queries
   so only a real browser proves either one. */
.job-context {
  display: flex; flex-wrap: wrap; gap: 0.35rem;
  padding: 0 0.1rem;
}
.customer-notes {
  display: flex; align-items: flex-start; gap: 0.45rem;
  font-size: 0.87rem; line-height: 1.35;
  color: var(--p-text-color, #111827);
  background: var(--p-content-hover-background, #f3f4f6);
  border-left: 3px solid var(--p-orange-500, #f97316);
  border-radius: 0.35rem; padding: 0.45rem 0.6rem;
}
.equip-head {
  display: flex; align-items: center; gap: 0.45rem;
  cursor: pointer; min-height: 44px;
}
.equip-chevron { margin-left: auto; font-size: 0.75rem; }
.equip-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }
.equip-item {
  border-top: 1px solid var(--p-content-border-color, #e5e7eb);
  padding-top: 0.5rem;
}
.equip-item:first-child { border-top: 0; padding-top: 0; }
.equip-line { display: flex; align-items: center; gap: 0.4rem; flex-wrap: wrap; }
.equip-meta {
  display: flex; flex-wrap: wrap; gap: 0.6rem;
  font-size: 0.8rem; color: var(--p-text-muted-color, #6b7280); margin-top: 0.2rem;
}
.equip-notes { font-size: 0.83rem; margin-top: 0.2rem; color: var(--p-text-color, #111827); }
.muted { color: var(--p-text-muted-color, #6b7280); font-size: 0.87rem; }
.secondary-actions-row { display: flex; flex-wrap: wrap; gap: 0.5rem; }
.secondary-actions-row > * { flex: 1 1 auto; min-height: 44px; }
</style>
