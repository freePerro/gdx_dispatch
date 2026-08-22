<!--
  One job card, for every mobile surface.

  There used to be three separately-written markups for one concept — the
  Today route card, Today's "in the area" card, and the Jobs list card — and
  the primary one, the card a tech looks at all day, was not tappable at all.
  Commit 67b8a5d fixed exactly that bug on the Jobs list and missed the route
  card in the same file.

  Two things this card refuses to do:

  1. Nest an interactive control inside the link. Today's route card carried
     `@click="openMaps(job)"` on the address text, so tapping the address
     opened maps — the gesture used between every pair of stops. Wrapping the
     card in a <router-link> would either delete that or fire both handlers on
     one tap. Instead the navigate action is a real sibling <button>, outside
     the anchor: valid markup, one gesture per target, and more discoverable
     than "the address happens to be tappable".

  2. Carry route chrome. Stop number, drive-time legs and reorder controls are
     properties of a ROUTE, not of a job — the Jobs list has no stop #4. They
     stay in the list that owns them; this card exposes a `lead` slot for the
     badge and nothing more.
-->
<template>
  <div class="mjc-wrap">
    <router-link
      :to="`/mobile/jobs/${job.id}`"
      class="mjc"
      :data-testid="`${testid}-${job.id}`"
    >
      <div class="mjc-row mjc-row-top">
        <slot name="lead" />
        <span class="mjc-customer">{{ customerName }}</span>
        <span :class="['status-pill', `status-${statusKey}`]">
          <i :class="statusIcon" />
          {{ statusLabel }}
        </span>
      </div>

      <div v-if="whenLabel" class="mjc-when">
        <i class="pi pi-clock" />
        <span>{{ whenLabel }}</span>
      </div>

      <div v-if="techName" class="mjc-tech" data-testid="mobile-job-tech">
        <i class="pi pi-user" />
        <span>{{ techName }}</span>
      </div>

      <div class="mjc-address" :class="{ 'mjc-address-missing': !address }">
        <i class="pi pi-map-marker" />
        <span v-if="job.site_label" class="mjc-site-label">{{ job.site_label }}</span>
        <span class="mjc-address-text">{{ address || 'No address — ask dispatch' }}</span>
      </div>

      <div
        v-if="unseenParts > 0"
        class="mjc-unseen"
        :data-testid="`${testid}-unseen-${job.id}`"
      >
        <i class="pi pi-wrench" />
        <span>{{ unseenParts }} part update{{ unseenParts === 1 ? '' : 's' }} from dispatch</span>
      </div>

      <div class="mjc-row mjc-row-bottom">
        <span v-if="subtitle" class="mjc-title">{{ subtitle }}</span>
        <i class="pi pi-chevron-right mjc-chevron" />
      </div>

      <div v-if="flags.length" class="mjc-flags">
        <span v-for="f in flags" :key="f.label" :class="['mjc-flag', `mjc-flag-${f.tone}`]">
          {{ f.label }}
        </span>
      </div>
    </router-link>

    <!-- Sibling, not a child: see the note at the top of this file. -->
    <button
      v-if="job.navigation_link"
      type="button"
      class="mjc-nav"
      :aria-label="`Navigate to ${customerName}`"
      :data-testid="`${testid}-nav-${job.id}`"
      @click.stop.prevent="$emit('navigate', job)"
    >
      <i class="pi pi-directions" />
    </button>
  </div>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  job: { type: Object, required: true },
  // Each surface keeps the testid it has always had. July plan, trap #8:
  // mobile-job-card-{id} on the Jobs list, mobile-area-job-{id} on Today's
  // "in the area" section, mobile-route-job-{id} on the route itself. Renaming
  // them would quietly unhook existing specs and the e2e touch-target walk.
  testid: { type: String, default: 'mobile-job-card' },
  // Count of parts dispatch has actioned since the tech last looked. Route-only
  // today: it is the one piece of cross-stop state the route screen computes.
  unseenParts: { type: Number, default: 0 },
})
defineEmits(['navigate'])

// One card, three payload shapes until the server finishes converging. The
// jobs-list endpoint still emits flat customer_name/display_address; /today and
// /job/{id} nest under _job_card. Read both rather than let a surface render
// an em-dash where a customer name should be.
const customerName = computed(
  () => props.job.customer?.name || props.job.customer_name || '—',
)
const address = computed(
  () => props.job.site_address || props.job.display_address || '',
)
const techName = computed(() => props.job.assigned_tech_name || '')
const subtitle = computed(() => props.job.title || '')

const statusKey = computed(
  () => String(props.job.dispatch_status || 'assigned').replace(' ', '_'),
)
const statusIcon = computed(() => ({
  en_route: 'pi pi-send',
  on_site: 'pi pi-map-marker',
  done: 'pi pi-check',
  unassigned: 'pi pi-circle',
  assigned: 'pi pi-circle-fill',
}[statusKey.value] || 'pi pi-circle-fill'))
const statusLabel = computed(() => ({
  en_route: 'En route',
  on_site: 'On site',
  done: 'Done',
  unassigned: 'Unassigned',
  assigned: 'Assigned',
}[statusKey.value] || props.job.dispatch_status || 'Assigned'))

const whenLabel = computed(() => {
  const iso = props.job.time_window?.start || props.job.scheduled_at
  if (!iso) return ''
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return ''
    return d.toLocaleString([], {
      weekday: 'short', month: 'short', day: 'numeric',
      hour: 'numeric', minute: '2-digit',
    })
  } catch {
    return ''
  }
})

// Priority, return visit and customer alerts. These used to live only on the
// route card; a tech reaching the same job from the Jobs list saw none of it.
const flags = computed(() => {
  const out = []
  const p = props.job.priority
  if (p && p !== 'Normal') {
    const low = String(p).toLowerCase()
    out.push({ label: p, tone: low === 'urgent' || low === 'emergency' || low === 'high' ? 'danger' : 'warn' })
  }
  if (props.job.is_return_visit) out.push({ label: 'Return visit', tone: 'warn' })
  // "You are not alone on this one." The old route card worked this out by
  // resolving the caller's tech row out of sessionStorage and listing the
  // others — its own comment conceded that technicians.id and users.id do not
  // match and the rule was approximate. The count is the part a tech acts on,
  // and it needs no identity resolution to be correct.
  const crew = Array.isArray(props.job.assignments) ? props.job.assignments.length : 0
  if (crew > 1) out.push({ label: `${crew} techs`, tone: 'plain' })
  for (const a of props.job.alerts || []) {
    out.push({ label: String(a).replace(/_/g, ' '), tone: 'warn' })
  }
  return out
})
</script>

<style scoped>
.mjc-wrap { position: relative; }
.mjc {
  background: var(--p-content-background, #fff);
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  border-radius: 0.6rem;
  padding: 0.75rem 1rem;
  /* Room for the navigate button so long text never runs under it. */
  padding-right: 3.5rem;
  display: flex; flex-direction: column; gap: 0.35rem;
  color: inherit; text-decoration: none;
}
.mjc:active { background: var(--p-content-hover-background, #f3f4f6); }
.mjc-row { display: flex; align-items: center; gap: 0.5rem; }
.mjc-row-top { justify-content: space-between; }
.mjc-row-bottom { justify-content: space-between; }
.mjc-customer {
  font-size: 1rem; font-weight: 700;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.mjc-when, .mjc-tech {
  display: flex; align-items: center; gap: 0.35rem;
  font-size: 0.85rem; color: var(--p-text-muted-color, #6b7280);
}
.mjc-address {
  display: flex; align-items: center; gap: 0.35rem;
  font-size: 0.9rem; color: var(--p-primary-color, #2563eb);
  min-height: 24px;
}
.mjc-address-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mjc-address-missing { color: var(--p-text-muted-color, #9ca3af); font-style: italic; }
.mjc-site-label {
  font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
  border: 1px solid currentColor; border-radius: 4px; padding: 0.02rem 0.28rem;
}
.mjc-title { color: var(--p-text-muted-color, #6b7280); font-size: 0.85rem;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mjc-chevron { color: var(--p-text-muted-color, #9ca3af); font-size: 0.8rem; flex: 0 0 auto; }
.mjc-unseen {
  display: flex; align-items: center; gap: 0.35rem;
  font-size: 0.8rem; font-weight: 600;
  color: var(--p-orange-600, #ea580c);
}
.mjc-flags { display: flex; flex-wrap: wrap; gap: 0.3rem; }
.mjc-flag {
  font-size: 0.7rem; font-weight: 700; border-radius: 999px;
  padding: 0.1rem 0.45rem;
  background: var(--p-content-hover-background, #f3f4f6);
  color: var(--p-text-color, #111827);
}
.mjc-flag-danger { background: var(--p-red-500, #ef4444); color: #fff; }
.mjc-flag-warn { background: var(--p-orange-500, #f97316); color: #1f2937; }

/* 44px minimum, and it sits OUTSIDE the anchor so the two gestures never
   fight. Vertically centred against the card's first row. */
.mjc-nav {
  position: absolute; top: 0.5rem; right: 0.5rem;
  width: 44px; height: 44px;
  display: flex; align-items: center; justify-content: center;
  border-radius: 0.5rem; cursor: pointer;
  border: 1px solid var(--p-content-border-color, #e5e7eb);
  background: var(--p-content-background, #fff);
  color: var(--p-primary-color, #2563eb);
}
.mjc-nav:active { background: var(--p-content-hover-background, #f3f4f6); }

.status-pill {
  display: inline-flex; align-items: center; gap: 0.3rem;
  padding: 0.2rem 0.55rem; border-radius: 999px;
  font-size: 0.75rem; font-weight: 600; letter-spacing: 0.02em;
  flex: 0 0 auto;
}
.status-pill i { font-size: 0.7rem; }
.status-assigned   { background: #475569; color: #fff; }
.status-unassigned { background: #6b7280; color: #fff; }
.status-en_route   { background: #f59e0b; color: #1f2937; }
.status-on_site    { background: #2563eb; color: #fff; }
.status-done       { background: #15803d; color: #fff; }
</style>
