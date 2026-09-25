<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import Dialog from 'primevue/dialog'
import Select from 'primevue/select'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
import Textarea from 'primevue/textarea'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../composables/useApi'

// SimpleFIN bank feed — Settings → Integrations card (Doug 2026-08-13).
// Connect = paste a setup token generated at the SimpleFIN Bridge; GDX
// claims it once and never sees bank credentials. Data renders in the
// Bank Feeds view like any other provider; this card owns connection,
// schedule (frequency + local-time fetch hours + daily cap), quota,
// backfill progress, and the feed↔statement tie-out report.

const api = useApi()
const toast = useToast()

const loading = ref(true)
const status = ref(null)
const loadError = ref(null)

const setupToken = ref('')
const connecting = ref(false)

const relinkVisible = ref(false)
const relinkProposals = ref([])
const relinkApplying = ref(false)
// Every account the bridge re-issued under a new id, and the ones a mapping was
// actually posted for. The at-risk set is the ORPHANS, not the proposals: the
// server only proposes where a name matched, so a bridge that re-auths and
// reformats its account names yields zero proposals — no dialog, and without
// this the user would get a green "connected" toast while half the feed quietly
// stopped receiving data. Orphans keep sync_enabled and nothing ever marks them
// inactive, so the only symptom is a "Stale" badge nobody is watching for.
const relinkOrphans = ref([])
const relinkMappedIds = ref([])

const settings = ref({ frequency: 'manual', windowStart: '', windowEnd: '', cap: 20 })
const settingsSaving = ref(false)

const syncQueuing = ref(false)
const disconnectArmed = ref(false)
const disconnecting = ref(false)

const tieout = ref({ month: '', feedAccountId: null, bankAccountId: null })
const tieoutRunning = ref(false)
const tieoutResult = ref(null)
const bankAccounts = ref([])

// Server scores a re-link proposal 1.0 exact name, 0.8 stored mask found in the
// candidate name, 0.6 bare name substring (simplefin_router._propose_matches).
// ONLY 1.0 is pre-checked, because neither lower tier is identity evidence:
//   0.6 is just one display name containing the other ("Checking" inside
//       "Business Checking"), which two real accounts at one bank will satisfy;
//   0.8 sounds stronger and is not. The mask is 3-OR-4 trailing digits
//       (_TRAILING_DIGITS_RE = (\d{3,4})\s*$) tested as an UNANCHORED
//       substring, so — run against the real functions — "Savings 2204"
//       matches "Loan 2204-B Equipment", and a 3-digit mask "•204" matches
//       "Business Checking 12045". Pre-checking that offers a one-click merge
//       of a savings account into a loan account, and the merge is not
//       reversible (see applyRelink).
// A stricter default is affordable because every unmapped proposal is now
// reported on the way out (onRelinkHide) rather than passing silently.
const RELINK_AUTOCHECK_MIN = 1

const FREQUENCIES = [
  { label: 'Manual only', value: 'manual' },
  { label: 'Hourly', value: 'hourly' },
  { label: 'Every 4 hours', value: 'every_4h' },
  { label: 'Daily', value: 'daily' },
  { label: 'Weekly', value: 'weekly' },
]

const connected = computed(() => Boolean(status.value?.connected))
// `unhealthy` is the exact complement of the condition Bank Feeds uses to show
// its "Re-link in Settings" button (`!connected || auth_state !== 'healthy'`).
// It is deliberately open-world: an allowlist of the bad states would leave a
// 5th auth_state, added later, rendering that button while this card reported
// a green "Connected" with nothing to do — the dead end this screen exists to
// end. Anything not explicitly healthy needs the token form.
const unhealthy = computed(() =>
  !connected.value || status.value?.auth_state !== 'healthy')
// The known bad states, used only to pick the wording of the banner. A state
// outside this list still gets the form above — it just gets generic prose.
const needsReconnect = computed(() =>
  ['needs_reconnect', 'refresh_failed'].includes(status.value?.auth_state))
const quota = computed(() => status.value?.quota || { used: 0, cap: 20, remaining: 20 })
const capReached = computed(() => quota.value.remaining <= 0)
const capMax = computed(() => status.value?.schedule?.daily_fetch_cap_max ?? 20)
const backfillPending = computed(() =>
  (status.value?.accounts || []).filter((a) => a.sync_enabled && !a.is_inactive && !a.initial_backfill_done))
const tagValue = computed(() => {
  if (!connected.value) return 'Not Connected'
  // Same open-world rule as `unhealthy`: never label an unrecognised
  // auth_state "Connected", or the card contradicts the Bank Feeds row.
  if (unhealthy.value) return 'Reconnect required'
  if (status.value?.stale) return 'Stale'
  return 'Connected'
})
const tagSeverity = computed(() => {
  if (!connected.value) return 'warn'
  if (unhealthy.value) return 'danger'
  if (status.value?.stale) return 'warn'
  return 'success'
})

function applyStatusToForm() {
  const sched = status.value?.schedule || {}
  settings.value = {
    frequency: sched.frequency || 'manual',
    windowStart: sched.fetch_window_start || '',
    windowEnd: sched.fetch_window_end || '',
    cap: sched.daily_fetch_cap ?? 20,
  }
}

async function load() {
  loading.value = true
  loadError.value = null
  try {
    status.value = await api.get('/api/bank-feeds/simplefin/status')
    applyStatusToForm()
  } catch (err) {
    loadError.value = err?.message || 'Unable to load status'
    console.warn('simplefin card load failed:', err)
  } finally {
    loading.value = false
  }
}

async function connect() {
  if (!setupToken.value.trim()) return
  connecting.value = true
  try {
    const out = await api.post('/api/bank-feeds/simplefin/connect', {
      setup_token: setupToken.value.trim(),
    })
    setupToken.value = ''
    if (out?.warning) {
      toast.add({ severity: 'warn', summary: 'Connected with a warning', detail: out.warning, life: 8000 })
    } else {
      toast.add({ severity: 'success', summary: 'SimpleFIN connected', life: 4000 })
    }
    relinkOrphans.value = out?.preview?.orphaned || []
    relinkMappedIds.value = []
    if (out?.preview?.proposals?.length) {
      // A sub-exact proposal starts UNCHECKED. Applying one is irreversible on
      // bank data: /relink re-points external_account_id, and when the target
      // id is already held by another row, _absorb_duplicate_account folds that
      // row into this one — its transactions are RE-POINTED onto this account
      // (only ones whose external id this account already holds are deleted as
      // dupes), then the absorbed account row itself is hard-deleted. There is
      // no soft-delete and no undo.
      //
      // The two directions are not symmetric. Skipping a true match leaves the
      // old row orphaned: it keeps sync_enabled, the SimpleFIN sync never marks
      // a vanished account inactive, so the card reports "Stale" indefinitely
      // until someone switches it off on Bank Feeds → Accounts. Visible, and
      // nothing is lost. Confirming a WRONG match is worse than losing rows —
      // it grafts a DIFFERENT bank account's transactions onto this one's
      // ledger, where they look like history that was always there. So the
      // default favours the noisy failure.
      // A 1.0 is an exact NAME match and the server never checks the name is
      // unique, so it is only evidence when the name picks out ONE account on
      // BOTH sides. All three collisions were reproduced against the real
      // _propose_matches:
      //   two stored "Checking" + two incoming "Checking" -> two 1.0 proposals;
      //   one stored  "Checking" + two incoming "Checking" -> one 1.0 proposal;
      //   two stored  "Checking" + one incoming "Checking" -> one 1.0 proposal,
      //     because the `used` set lets only one through.
      // That last one is why the orphan side is counted from preview.orphaned
      // (every orphan) and not from preview.proposals (only the ones that
      // matched) — the filtered list would report a collision as unique.
      const countNames = (items, key) => (items || []).reduce((acc, it) => {
        const name = (it?.[key] || '').trim().toLowerCase()
        if (name) acc[name] = (acc[name] || 0) + 1
        return acc
      }, {})
      const orphanNames = countNames(out.preview.orphaned, 'name')
      const candidateNames = countNames(out.preview.new, 'name')
      // Candidate display names, so the dialog can show what an opaque bridge
      // id actually refers to.
      const candidateById = Object.fromEntries(
        (out.preview.new || []).map((c) => [c.id, c]),
      )
      relinkProposals.value = out.preview.proposals.map((p) => {
        const name = (p.account_name || '').trim().toLowerCase()
        const ambiguous = (orphanNames[name] || 0) > 1 || (candidateNames[name] || 0) > 1
        const candidate = candidateById[p.new_external_id]
        return {
          ...p,
          ambiguous,
          candidate_name: candidate?.name || '',
          candidate_balance: candidate?.balance ?? null,
          apply: !ambiguous && (p.confidence ?? 0) >= RELINK_AUTOCHECK_MIN,
        }
      })
      relinkVisible.value = true
    } else if (relinkOrphans.value.length) {
      // Re-issued accounts the server could not propose a match for. There is
      // no dialog in this case, so the notice has to fire here or it never does.
      reportUnmappedOrphans()
    }
    await load()
  } catch (err) {
    toast.add({
      severity: 'error', summary: 'Connect failed',
      detail: err?.message || 'The setup token was rejected', life: 9000,
    })
  } finally {
    connecting.value = false
  }
}

// Watching the flag rather than the Dialog's `@hide` on purpose: `@hide` fires
// off the leave transition, so it is both later than the state change and
// absent in jsdom. Every exit — Skip, the close-X, ESC, and Apply with nothing
// checked — writes this flag through v-model, so one watcher covers them all.
watch(relinkVisible, (visible, wasVisible) => {
  if (wasVisible && !visible) reportUnmappedOrphans()
})

// Reports every re-issued account that did NOT get a mapping. Called both when
// the dialog closes by any route and when there was no dialog to show, so the
// silence cannot hide behind "no proposals" or "some were mapped".
function reportUnmappedOrphans() {
  const mapped = new Set(relinkMappedIds.value)
  const skipped = relinkOrphans.value.filter((o) => !mapped.has(o.account_id))
  const hadMapping = mapped.size > 0
  relinkOrphans.value = []
  relinkMappedIds.value = []
  relinkProposals.value = []
  if (skipped.length === 0) return
  toast.add({
    severity: 'warn',
    summary: hadMapping
      ? `${skipped.length} account${skipped.length === 1 ? '' : 's'} left unmapped`
      : 'Nothing re-linked',
    // Deliberately no "re-link them later" instruction. Once a sync runs, the
    // new ids are stored as ordinary accounts, so _propose_matches has nothing
    // left to offer and this dialog cannot reappear — reconnecting to look for
    // it would burn a single-use bridge token for nothing.
    detail: 'The bridge re-issued those accounts under new ids. On the next sync they '
      + 'arrive as new accounts; the originals keep their history but stop receiving '
      + 'data and will report as stale — switch them off under Bank Feeds → Accounts. '
      + 'Once that sync has run this mapping step is no longer offered.',
    life: 12000,
  })
}

async function applyRelink() {
  relinkApplying.value = true
  try {
    const mappings = relinkProposals.value
      .filter((p) => p.apply)
      .map((p) => ({ account_id: p.account_id, new_external_id: p.new_external_id }))
    if (mappings.length) {
      await api.post('/api/bank-feeds/simplefin/relink', { mappings })
      relinkMappedIds.value = mappings.map((m) => m.account_id)
      toast.add({ severity: 'success', summary: 'Accounts re-linked', life: 4000 })
    }
    // Everything else — including Apply with nothing checked — is a dismissal,
    // and the visibility watcher reports it.
    relinkVisible.value = false
    await load()
  } catch (err) {
    toast.add({
      severity: 'error', summary: 'Re-link failed',
      detail: err?.message || 'Mapping rejected', life: 9000,
    })
  } finally {
    relinkApplying.value = false
  }
}

async function saveSettings() {
  settingsSaving.value = true
  try {
    const body = { frequency: settings.value.frequency, daily_fetch_cap: settings.value.cap }
    const start = settings.value.windowStart.trim()
    const end = settings.value.windowEnd.trim()
    if (start || end) {
      body.fetch_window_start = start
      body.fetch_window_end = end
    } else {
      body.clear_fetch_window = true
    }
    await api.put('/api/bank-feeds/simplefin/settings', body)
    toast.add({ severity: 'success', summary: 'Schedule saved', life: 4000 })
    await load()
  } catch (err) {
    toast.add({
      severity: 'error', summary: 'Save failed',
      detail: err?.message || 'Settings rejected', life: 9000,
    })
  } finally {
    settingsSaving.value = false
  }
}

async function syncNow() {
  syncQueuing.value = true
  try {
    await api.post('/api/bank-feeds/simplefin/sync', {})
    toast.add({ severity: 'info', summary: 'Sync queued', life: 4000 })
    await load()
  } catch (err) {
    toast.add({
      severity: 'warn', summary: 'Sync not queued',
      detail: err?.message || 'Daily fetch cap reached', life: 8000,
    })
  } finally {
    syncQueuing.value = false
  }
}

// Two-click disconnect on purpose — the global confirm dialog has a known
// silent auto-accept failure mode (#215), so no destructive action here
// rides it.
async function disconnect() {
  if (!disconnectArmed.value) {
    disconnectArmed.value = true
    setTimeout(() => { disconnectArmed.value = false }, 5000)
    return
  }
  disconnecting.value = true
  try {
    await api.post('/api/bank-feeds/simplefin/disconnect', {})
    toast.add({ severity: 'success', summary: 'SimpleFIN disconnected', life: 4000 })
    disconnectArmed.value = false
    await load()
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Disconnect failed', detail: err?.message, life: 9000 })
  } finally {
    disconnecting.value = false
  }
}

async function loadBankAccounts() {
  if (bankAccounts.value.length) return
  try {
    const out = await api.get('/api/bank-feeds/statements/accounts')
    bankAccounts.value = (out?.items || []).map((a) => ({
      label: `${a.name || a.institution} •${a.last4}`, value: a.id,
    }))
  } catch (err) {
    console.warn('simplefin tieout accounts load failed:', err)
  }
}

async function runTieout() {
  if (!tieout.value.month || !tieout.value.feedAccountId || !tieout.value.bankAccountId) return
  tieoutRunning.value = true
  tieoutResult.value = null
  try {
    const q = new URLSearchParams({
      feed_account_id: tieout.value.feedAccountId,
      bank_account_id: tieout.value.bankAccountId,
      month: tieout.value.month,
    })
    tieoutResult.value = await api.get(`/api/bank-feeds/simplefin/tieout?${q.toString()}`)
  } catch (err) {
    toast.add({
      severity: 'error', summary: 'Tie-out failed',
      detail: err?.message || 'Report unavailable', life: 8000,
    })
  } finally {
    tieoutRunning.value = false
  }
}

function money(cents) {
  if (cents == null) return '—'
  return (cents / 100).toLocaleString('en-US', { style: 'currency', currency: 'USD' })
}

onMounted(() => { load() })
</script>

<template>
  <div class="sfin-card" data-testid="simplefin-card">
    <div class="sfin-header">
      <div>
        <h3><i class="pi pi-building-columns" /> SimpleFIN Bank Feed</h3>
        <p class="muted">Daily balances and transactions from the bank, via the SimpleFIN Bridge.</p>
      </div>
      <Tag v-if="!loading" :value="tagValue" :severity="tagSeverity" data-testid="sfin-status-tag" />
    </div>

    <div v-if="loading" class="muted">Loading…</div>
    <small v-else-if="loadError" class="muted">Status unavailable — {{ loadError }}</small>

    <template v-else>
      <!-- Banner only when there is still a connection to describe; a
           disconnected card just shows the setup instructions below. -->
      <p v-if="connected && needsReconnect" class="status-error" data-testid="sfin-reconnect-banner">
        The bridge connection needs attention — sign in at bridge.simplefin.org, fix the
        bank connection there, then paste a fresh setup token below.
      </p>
      <p v-else-if="connected && unhealthy" class="status-error" data-testid="sfin-reconnect-banner">
        The bridge connection is not healthy ({{ status.auth_state }}) — check the
        connection at bridge.simplefin.org, then paste a fresh setup token below.
      </p>
      <p v-else-if="connected && status.stale" class="status-warnrow" data-testid="sfin-stale-banner">
        No successful fetch in over {{ status.stale_after_hours }} hours — data may be behind.
      </p>

      <!-- The setup-token form is both the way in and the way back. Re-claiming
           while still connected is the DESIGNED repair: POST /connect updates
           the connection in place, sets auth_state healthy, and returns
           id-mapping proposals. Without it the only route back was Disconnect
           first — which keeps every row (oauth.soft_disconnect, "Rows are
           KEPT"), so nothing was lost, but it stops the feed and makes fixing a
           broken connection look like tearing it down. This is where Bank
           Feeds' "Re-link in Settings" button lands, and `unhealthy` is the
           exact complement of the condition that renders it. -->
      <template v-if="unhealthy">
        <!-- Someone repairing a connection already has a bridge account;
             telling them to create one reads as the wrong screen. -->
        <small v-if="connected" class="muted" data-testid="sfin-relink-steps">
          1. Sign in at
          <a href="https://bridge.simplefin.org/" target="_blank" rel="noopener">bridge.simplefin.org</a>
          and fix the bank connection there (GDX never sees your bank login).
          2. Generate a new setup token and paste it here.
        </small>
        <small v-else class="muted" data-testid="sfin-connect-steps">
          1. Create an account at
          <a href="https://bridge.simplefin.org/" target="_blank" rel="noopener">bridge.simplefin.org</a>
          and connect your bank there (GDX never sees your bank login).
          2. Generate a setup token and paste it here.
        </small>
        <Textarea
          v-model="setupToken" rows="2" autoResize
          placeholder="Paste the SimpleFIN setup token"
          data-testid="sfin-token-input"
        />
        <div class="integration-actions">
          <Button
            :label="connected ? 'Re-link with new token' : 'Connect'"
            :loading="connecting" :disabled="!setupToken.trim()"
            data-testid="sfin-connect-btn" @click="connect"
          />
        </div>
      </template>

      <template v-if="connected">
      <div class="sfin-facts">
        <small class="muted">
          Last sync: {{ status.last_synced_at ? new Date(status.last_synced_at).toLocaleString() : 'never' }}
        </small>
        <small class="muted" data-testid="sfin-quota">
          Fetches today: {{ quota.used }}/{{ quota.cap }}
        </small>
        <small v-if="backfillPending.length" class="muted" data-testid="sfin-backfill">
          History backfill in progress:
          <template v-for="a in backfillPending" :key="a.id">
            {{ a.name || a.external_account_id }} (through {{ a.backfill_synced_through || 'start' }})
          </template>
        </small>
        <small
          v-if="status.schedule?.last_run_error"
          class="status-warnrow" data-testid="sfin-last-error"
        >
          Last sync reported: {{ status.schedule.last_run_error }}
        </small>
      </div>

      <div class="sfin-settings">
        <label class="field">
          <span>Fetch frequency</span>
          <Select
            v-model="settings.frequency" :options="FREQUENCIES"
            optionLabel="label" optionValue="value" data-testid="sfin-frequency"
          />
        </label>
        <label class="field">
          <span>Fetch hours ({{ status.timezone }})</span>
          <span class="window-inputs">
            <InputText v-model="settings.windowStart" placeholder="08:00" size="small" data-testid="sfin-window-start" />
            –
            <InputText v-model="settings.windowEnd" placeholder="18:00" size="small" data-testid="sfin-window-end" />
          </span>
          <small class="muted">Local time. Leave both empty for any hour.</small>
        </label>
        <label class="field">
          <span>Daily fetch cap (max {{ capMax }})</span>
          <InputNumber
            v-model="settings.cap" :min="1" :max="capMax" showButtons
            data-testid="sfin-cap"
          />
        </label>
        <div class="integration-actions">
          <Button label="Save schedule" size="small" :loading="settingsSaving"
                  data-testid="sfin-save-settings" @click="saveSettings" />
          <Button
            label="Sync Now" icon="pi pi-sync" size="small" severity="info"
            :loading="syncQueuing" :disabled="capReached"
            data-testid="sfin-sync-now" @click="syncNow"
          />
          <Button
            :label="disconnectArmed ? 'Really disconnect?' : 'Disconnect'"
            severity="danger" size="small" text :loading="disconnecting"
            data-testid="sfin-disconnect" @click="disconnect"
          />
        </div>
        <small v-if="capReached" class="muted">
          Daily fetch cap reached — resets at local midnight.
        </small>
      </div>

      <details class="sfin-tieout" @toggle="loadBankAccounts">
        <summary>Statement tie-out</summary>
        <small class="muted">
          Compare a month of feed data against the imported bank statement. The two
          should agree — a mismatch is an error or fraud signal.
        </small>
        <div class="tieout-controls">
          <InputText v-model="tieout.month" placeholder="YYYY-MM" size="small" data-testid="sfin-tieout-month" />
          <Select
            v-model="tieout.feedAccountId" placeholder="Feed account" size="small"
            :options="(status.accounts || []).map(a => ({ label: a.name || a.external_account_id, value: a.id }))"
            optionLabel="label" optionValue="value" data-testid="sfin-tieout-feed"
          />
          <Select
            v-model="tieout.bankAccountId" placeholder="Statement account" size="small"
            :options="bankAccounts" optionLabel="label" optionValue="value"
            data-testid="sfin-tieout-bank"
          />
          <Button label="Run" size="small" :loading="tieoutRunning"
                  :disabled="!tieout.month || !tieout.feedAccountId || !tieout.bankAccountId"
                  data-testid="sfin-tieout-run" @click="runTieout" />
        </div>
        <div v-if="tieoutResult" class="tieout-result" data-testid="sfin-tieout-result">
          <p>
            <strong>{{ tieoutResult.matched }}</strong> matched ·
            statement {{ tieoutResult.statement_lines }} lines ({{ money(tieoutResult.statement_sum_cents) }}) ·
            feed {{ tieoutResult.feed_transactions }} transactions ({{ money(tieoutResult.feed_sum_cents) }})
          </p>
          <p v-if="!tieoutResult.statement_only.length && !tieoutResult.feed_only.length" class="status-ok">
            Sources agree for {{ tieoutResult.month }}.
          </p>
          <template v-else>
            <div v-if="tieoutResult.statement_only.length">
              <strong>On the statement, missing from the feed:</strong>
              <ul>
                <li v-for="l in tieoutResult.statement_only" :key="l.id">
                  {{ l.date }} · {{ money(l.amount_cents) }} · {{ l.description }}
                </li>
              </ul>
            </div>
            <div v-if="tieoutResult.feed_only.length">
              <strong>In the feed, missing from the statement:</strong>
              <ul>
                <li v-for="f in tieoutResult.feed_only" :key="f.id">
                  {{ f.date }} · {{ money(f.amount_cents) }} · {{ f.description }}
                </li>
              </ul>
            </div>
          </template>
        </div>
      </details>
      </template>
    </template>

    <Dialog
      v-model:visible="relinkVisible" modal header="Re-link accounts"
      :style="{ width: '34rem' }" data-testid="sfin-relink-dialog"
    >
      <p class="muted">
        The bridge presented existing accounts under new ids. Confirm the mapping so
        history and watermarks are kept — unchecked rows will be treated as new accounts.
        Weak name matches start unchecked: confirming one merges the two accounts'
        transaction history into a single account and cannot be undone, so check it
        only if you recognise the pairing.
      </p>
      <!-- Confidence is shown because applying a mapping grafts an existing
           account's transaction history and sync watermarks onto a different
           external id. The server proposes on a name-similarity score, so a
           partial-name match from a DIFFERENT real bank account can be
           proposed; the reader needs to see how sure the match is before
           confirming it. An exact match reads "exact". -->
      <div v-for="p in relinkProposals" :key="p.account_id" class="relink-row">
        <input v-model="p.apply" type="checkbox" :id="`rl-${p.account_id}`" />
        <label :for="`rl-${p.account_id}`">
          <!-- The right-hand side is an opaque bridge id, and the user is being
               asked to recognise the pairing — so show the candidate's own name
               and balance, which /connect already returns in preview.new. -->
          {{ p.account_name }} →
          <template v-if="p.candidate_name">
            {{ p.candidate_name }}<template v-if="p.candidate_balance != null">
              ({{ p.candidate_balance }})</template>
          </template>
          <template v-else>{{ p.new_external_id }}</template>
          <span
            v-if="p.confidence != null"
            class="relink-confidence"
            :class="{ weak: p.ambiguous || p.confidence < RELINK_AUTOCHECK_MIN }"
            :data-testid="`sfin-relink-confidence-${p.account_id}`"
          >
            <template v-if="p.ambiguous">name matches more than one account</template>
            <template v-else-if="p.confidence >= 1">exact</template>
            <template v-else>{{ Math.round(p.confidence * 100) }}% match</template>
          </span>
        </label>
      </div>
      <template #footer>
        <Button
          label="Skip — add them as new" text
          data-testid="sfin-relink-skip" @click="relinkVisible = false"
        />
        <Button label="Apply mapping" :loading="relinkApplying"
                data-testid="sfin-relink-apply" @click="applyRelink" />
      </template>
    </Dialog>
  </div>
</template>

<style scoped>
.sfin-card {
  background: var(--surface-panel);
  color: var(--text-primary);
  border: 1px solid var(--border-subtle);
  border-radius: 8px;
  padding: 1.25rem;
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}
.sfin-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
}
.sfin-header h3 { margin: 0 0 0.25rem 0; font-size: 1rem; font-weight: 600; }
.muted { color: var(--text-muted); font-size: 0.875rem; margin: 0; }
.sfin-facts { display: flex; flex-wrap: wrap; gap: 0.35rem 1.25rem; }
.sfin-settings { display: flex; flex-direction: column; gap: 0.6rem; }
.field { display: flex; flex-direction: column; gap: 0.25rem; font-size: 0.875rem; }
.window-inputs { display: flex; align-items: center; gap: 0.4rem; }
.window-inputs :deep(input) { width: 5.5rem; }
.integration-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }
.status-error {
  color: var(--color-danger-500, #ef4444);
  font-size: 0.875rem;
  margin: 0;
}
.status-warnrow { color: var(--color-warning-500, #f59e0b); font-size: 0.875rem; margin: 0; }
.status-ok { color: var(--color-success-500, #22c55e); font-size: 0.875rem; }
.sfin-tieout summary { cursor: pointer; font-size: 0.9rem; font-weight: 600; }
.tieout-controls { display: flex; gap: 0.5rem; flex-wrap: wrap; margin: 0.6rem 0; align-items: center; }
.tieout-result { font-size: 0.85rem; }
.tieout-result ul { margin: 0.25rem 0 0.5rem 1.1rem; padding: 0; }
.relink-row { display: flex; gap: 0.5rem; align-items: center; margin: 0.35rem 0; }
.relink-confidence {
  margin-left: 0.4rem;
  font-size: 0.78rem;
  color: var(--text-muted);
}
.relink-confidence.weak { color: var(--color-warning-500, #f59e0b); font-weight: 600; }
</style>
