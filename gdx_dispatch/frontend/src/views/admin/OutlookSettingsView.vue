<script setup>
import { ref, onMounted, computed } from 'vue'
import Toolbar from 'primevue/toolbar'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
import AutoComplete from 'primevue/autocomplete'
import Password from 'primevue/password'
import Slider from 'primevue/slider'
import ToggleSwitch from 'primevue/toggleswitch'
import Tabs from 'primevue/tabs'
import TabList from 'primevue/tablist'
import Tab from 'primevue/tab'
import TabPanels from 'primevue/tabpanels'
import TabPanel from 'primevue/tabpanel'
import Select from 'primevue/select'
import Message from 'primevue/message'
import { useToast } from 'primevue/usetoast'
import { useApi } from '../../composables/useApi'
import { useDestructiveConfirm } from '../../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

// Sprint Outlook Integration — Phase 8 admin settings page.
// Mounted at /settings/integrations/outlook (router config in slice S40).

const api = useApi()
const toast = useToast()

const loading = ref(false)
const error = ref(null)
const credentials = ref(null)
const settings = ref(null)
const newSecret = ref('')

const TAB_KEYS = {
  CONNECTION: 'connection',
  TAGGING: 'tagging',
  VISIBILITY: 'visibility',
  VENDOR_BILLS: 'vendor_bills',
}
const activeTab = ref(TAB_KEYS.CONNECTION)

// Consumer mail providers. Allowlisting one of these matches every sender at
// that provider, not just the vendor — worth a warning, not a block, because
// plenty of small suppliers really do invoice from a gmail address.
const CONSUMER_DOMAINS = [
  'gmail.com', 'googlemail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
  'live.com', 'aol.com', 'icloud.com', 'me.com', 'msn.com', 'comcast.net',
]

const sweeping = ref(false)
const sweepDays = ref(120)

const broadEntries = computed(() =>
  (settings.value?.vendor_bill_sender_allowlist || [])
    .filter((e) => CONSUMER_DOMAINS.includes(String(e).trim().toLowerCase())),
)

const allowlistEmpty = computed(
  () => (settings.value?.vendor_bill_sender_allowlist || []).length === 0,
)

// PrimeVue's AutoComplete (multiple + no typeahead) commits a chip ONLY on
// Enter — onBlur drops focus without touching the model. So typing a sender and
// clicking Save straight after would send the OLD list, flash "Saved", and
// leave the typed text sitting in the box looking accepted. That is precisely
// the "why didn't it save?" this whole page exists to eliminate, so commit any
// pending text before it can be lost.
function commitPendingSender(event) {
  const el = event?.target
  const raw = (el?.value || '').trim()
  if (!raw) return
  if (!Array.isArray(settings.value.vendor_bill_sender_allowlist)) {
    settings.value.vendor_bill_sender_allowlist = []
  }
  const list = settings.value.vendor_bill_sender_allowlist
  if (!list.some((e) => String(e).toLowerCase() === raw.toLowerCase())) {
    list.push(raw)
  }
  if (el) el.value = ''
}

// What the server last confirmed it has. The sweep reads the SAVED list, so an
// edit sitting unsaved on screen would silently not apply — the confirm dialog
// says so, but a dialog is a bad place for a fact the user needs BEFORE
// clicking, and useDestructiveConfirm resolves its service lazily outside
// setup, so in practice the dialog doesn't render at all here. Surfacing it
// inline is both more visible and not dependent on that.
const savedAllowlist = ref([])

const allowlistDirty = computed(() => (
  JSON.stringify(settings.value?.vendor_bill_sender_allowlist || [])
  !== JSON.stringify(savedAllowlist.value)
))

const ROLE_OPTIONS = [
  { label: 'Tech and above (everyone)', value: 'tech' },
  { label: 'CSR/Dispatcher and above (default)', value: 'tech_plus_one' },
  { label: 'Admin/Owner only', value: 'admin_only' },
  { label: 'Mailbox owner only (fully private)', value: 'owner_only' },
]

const TECH_OUTBOUND_OPTIONS = [
  { label: 'Only the sender', value: 'only_sender' },
  { label: 'All techs', value: 'all_techs' },
  { label: 'Above-tech roles only', value: 'above_tech' },
]

const TECH_TO_TECH_OPTIONS = [
  { label: 'Only the participants', value: 'only_participants' },
  { label: 'All techs', value: 'all_techs' },
  { label: 'Above-tech roles only', value: 'above_tech' },
]

const ABOVE_TECH_SCOPE_OPTIONS = [
  { label: 'All tagged emails', value: 'all_tagged' },
  { label: 'Only customers/jobs they have access to', value: 'only_assigned_rows' },
]

const UNTAGGED_OPTIONS = [
  { label: 'Only the mailbox owner', value: 'only_owner' },
  { label: 'Admins and owners (above-tech roles)', value: 'above_tech' },
  { label: 'Hidden from everyone', value: 'none' },
]


async function load() {
  loading.value = true
  error.value = null
  try {
    credentials.value = await api.get('/api/admin/outlook/credentials')
    settings.value = await api.get('/api/admin/outlook/settings')
    savedAllowlist.value = [...(settings.value.vendor_bill_sender_allowlist || [])]
  } catch (err) {
    error.value = err?.message || 'Failed to load Outlook settings'
  } finally {
    loading.value = false
  }
}

async function saveCredentials() {
  const payload = {
    microsoft_tenant_id: credentials.value.microsoft_tenant_id || null,
    client_id: credentials.value.client_id || null,
  }
  if (newSecret.value) {
    payload.client_secret = newSecret.value
  }
  try {
    credentials.value = await api.patch('/api/admin/outlook/credentials', payload)
    newSecret.value = ''
    toast.add({ severity: 'success', summary: 'Saved', detail: 'Outlook credentials updated.', life: 3000 })
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Save failed', detail: err?.message || 'Unknown error', life: 5000 })
  }
}

async function clearSecret() {
  if (!(await confirmAsync({ header: 'Confirm', message: 'Clear the stored client secret? Users will not be able to connect until you paste a new one.' }))) return
  try {
    await api.del('/api/admin/outlook/credentials')
    credentials.value.secret_set = false
    credentials.value.secret_set_at = null
    toast.add({ severity: 'success', summary: 'Cleared', detail: 'Client secret removed.', life: 3000 })
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Clear failed', detail: err?.message || 'Unknown error', life: 5000 })
  }
}

async function saveSettings() {
  try {
    settings.value = await api.patch('/api/admin/outlook/settings', {
      backfill_days: settings.value.backfill_days,
      tag_strategy_order: settings.value.tag_strategy_order,
      tag_strategy_enabled: settings.value.tag_strategy_enabled,
      ai_tag_threshold: settings.value.ai_tag_threshold,
      visibility_rules: settings.value.visibility_rules,
      // Only when it changed. PATCH treats an omitted key as "leave alone", and
      // every tab's Save posts this same payload — so blindly resending the
      // allowlist would make ONE malformed stored entry (this column was
      // hand-written SQL before today) 422 the Tagging and Visibility tabs too,
      // over a value nobody on those tabs touched.
      ...(allowlistDirty.value
        ? { vendor_bill_sender_allowlist: settings.value.vendor_bill_sender_allowlist || [] }
        : {}),
    })
    savedAllowlist.value = [...(settings.value.vendor_bill_sender_allowlist || [])]
    toast.add({ severity: 'success', summary: 'Saved', detail: 'Outlook settings updated.', life: 3000 })
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Save failed', detail: err?.message || 'Unknown error', life: 5000 })
  }
}

async function runSweep() {
  // No confirm dialog here on purpose. The sweep is additive and idempotent
  // (checkpointed per message), so it doesn't warrant one — and the unsaved-edit
  // hazard it would have warned about is handled structurally instead, by
  // disabling this button while `allowlistDirty`. See [[useDestructiveConfirm]]:
  // its service resolves outside setup and the fallback silently auto-accepts,
  // so a dialog here would be decoration that never renders.
  sweeping.value = true
  try {
    const res = await api.post('/api/admin/outlook/vendor-bills/sweep', { days: sweepDays.value })
    const n = res?.queued?.length || 0
    toast.add({
      severity: 'success',
      summary: 'Sweep queued',
      detail: `Queued for ${n} mailbox${n === 1 ? '' : 'es'}. It runs in the background — `
        + 'new bills and statements appear on their pages as it works.',
      life: 6000,
    })
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Sweep failed', detail: err?.message || 'Unknown error', life: 6000 })
  } finally {
    sweeping.value = false
  }
}

onMounted(() => {
  load()
})

defineExpose({ load, saveCredentials, saveSettings, clearSecret, runSweep, sweepDays })
</script>

<template>
    <section class="outlook-settings view-card">
      <Toolbar>
        <template #start>
          <h1 class="view-heading">Outlook / Microsoft 365 Integration</h1>
        </template>
      </Toolbar>

      <p class="view-description text-muted">
        Configure Microsoft Entra ID credentials, tagging strategy, visibility rules,
        and automation triggers. Each employee then connects their own mailbox via
        Profile → Integrations.
      </p>

      <Message v-if="error" severity="error" :closable="false">{{ error }}</Message>
      <div v-if="loading" class="text-muted">Loading…</div>

    <Tabs v-else-if="settings && credentials" v-model:value="activeTab">
      <TabList>
        <Tab :value="TAB_KEYS.CONNECTION">Connection</Tab>
        <Tab :value="TAB_KEYS.TAGGING">Tagging</Tab>
        <Tab :value="TAB_KEYS.VISIBILITY">Visibility</Tab>
        <Tab :value="TAB_KEYS.VENDOR_BILLS">Vendor Bills</Tab>
      </TabList>
      <TabPanels>

        <!-- Connection -->
        <TabPanel :value="TAB_KEYS.CONNECTION">
          <div class="flex flex-col gap-4 mt-4">
            <div>
              <label class="font-medium">Microsoft Tenant ID</label>
              <p class="text-xs hint-text mb-1">
                The Directory (tenant) ID from Azure Portal → App Registrations →
                your GDX app → Overview.
              </p>
              <InputText v-model="credentials.microsoft_tenant_id" class="w-full" />
            </div>
            <div>
              <label class="font-medium">Client ID</label>
              <p class="text-xs hint-text mb-1">
                The Application (client) ID from the same Overview page.
              </p>
              <InputText v-model="credentials.client_id" class="w-full" />
            </div>
            <div>
              <label class="font-medium">
                Client Secret
                <span v-if="credentials.secret_set" class="text-xs success-text ml-2">
                  ✓ set {{ credentials.secret_set_at?.slice(0,10) }}
                </span>
              </label>
              <p class="text-xs hint-text mb-1">
                Paste a new secret to rotate. Stored Fernet-encrypted; never returned by the API.
              </p>
              <Password v-model="newSecret" :feedback="false" toggleMask class="w-full" placeholder="Paste new client secret" />
            </div>
            <div class="flex gap-2">
              <Button label="Save Credentials" @click="saveCredentials" />
              <Button v-if="credentials.secret_set" label="Clear Secret" severity="danger" outlined @click="clearSecret" />
            </div>
          </div>
        </TabPanel>

        <!-- Tagging -->
        <TabPanel :value="TAB_KEYS.TAGGING">
          <div class="flex flex-col gap-4 mt-4">
            <div>
              <label class="font-medium">Backfill window (days)</label>
              <p class="text-xs hint-text mb-1">
                On first connect, pull mail received this many days back.
              </p>
              <InputNumber v-model="settings.backfill_days" :min="1" :max="3650" />
            </div>
            <div>
              <h3 class="font-medium mb-1">Strategy enabled</h3>
              <div class="flex flex-col gap-2">
                <div class="flex items-center gap-2">
                  <ToggleSwitch v-model="settings.tag_strategy_enabled.auto_match" />
                  <span>Auto-match by email address (sender/recipient)</span>
                </div>
                <div class="flex items-center gap-2">
                  <ToggleSwitch v-model="settings.tag_strategy_enabled.job_thread" />
                  <span>Subject regex (e.g. <code>[Job #123]</code>)</span>
                </div>
                <div class="flex items-center gap-2">
                  <ToggleSwitch v-model="settings.tag_strategy_enabled.ai" />
                  <span>AI-assisted tagging (uses tenant Anthropic key)</span>
                </div>
              </div>
            </div>
            <div>
              <label class="font-medium">AI confidence threshold</label>
              <p class="text-xs hint-text mb-1">
                AI tags below this score are dropped. Default 0.85.
              </p>
              <Slider v-model="settings.ai_tag_threshold" :min="0" :max="1" :step="0.05" class="w-64" />
              <span class="ml-3 text-sm">{{ settings.ai_tag_threshold?.toFixed(2) }}</span>
            </div>
            <div>
              <Button label="Save Tagging Settings" @click="saveSettings" />
            </div>
          </div>
        </TabPanel>

        <!-- Visibility -->
        <TabPanel :value="TAB_KEYS.VISIBILITY">
          <div class="flex flex-col gap-4 mt-4">
            <div>
              <label class="font-medium">Tagged emails — minimum role to view</label>
              <p class="text-xs hint-text mb-1">
                Who can see emails that are linked to a customer or job?
              </p>
              <Select
                v-model="settings.visibility_rules.tagged_visibility_above_role"
                :options="ROLE_OPTIONS"
                optionLabel="label"
                optionValue="value"
                class="w-full"
              />
            </div>
            <div class="flex items-center gap-2">
              <ToggleSwitch v-model="settings.visibility_rules.tech_recipient_visible_to_all_techs" />
              <span>If a tech is in to/cc, all techs can see it</span>
            </div>
            <div>
              <label class="font-medium">Tech outbound (no tag) — visibility</label>
              <Select
                v-model="settings.visibility_rules.tech_outbound_no_tag_visibility"
                :options="TECH_OUTBOUND_OPTIONS"
                optionLabel="label" optionValue="value" class="w-full"
              />
            </div>
            <div>
              <label class="font-medium">Tech-to-tech internal (no tag) — visibility</label>
              <Select
                v-model="settings.visibility_rules.tech_to_tech_internal_visibility"
                :options="TECH_TO_TECH_OPTIONS"
                optionLabel="label" optionValue="value" class="w-full"
              />
            </div>
            <div>
              <label class="font-medium">Above-tech scope</label>
              <Select
                v-model="settings.visibility_rules.above_tech_scope"
                :options="ABOVE_TECH_SCOPE_OPTIONS"
                optionLabel="label" optionValue="value" class="w-full"
              />
            </div>
            <div>
              <label class="font-medium">Untagged emails — visibility</label>
              <Select
                v-model="settings.visibility_rules.untagged_visibility"
                :options="UNTAGGED_OPTIONS"
                optionLabel="label" optionValue="value" class="w-full"
              />
            </div>
            <div>
              <Button label="Save Visibility Rules" @click="saveSettings" />
            </div>
          </div>
        </TabPanel>

        <!-- Vendor Bills -->
        <TabPanel :value="TAB_KEYS.VENDOR_BILLS">
          <div class="flex flex-col gap-4 mt-4">
            <p class="text-sm hint-text">
              When mail arrives from one of these senders, GDX downloads its PDF
              attachments and files them automatically — supplier invoices land in
              <strong>Vendor Bills</strong>, statements of account land in
              <strong>Vendor Statements</strong>. Nothing else is touched, and
              anything it can't read is left alone rather than guessed at.
            </p>

            <div>
              <label class="font-medium" for="vb-allowlist">Allowlisted senders</label>
              <p class="text-xs hint-text mb-1">
                A full address (<code>ar@vendor.com</code>) or a whole domain
                (<code>vendor.com</code>, which also covers its subdomains).
                Type one and press Enter.
              </p>
              <!-- AutoComplete in multiple+no-typeahead mode is PrimeVue 4's
                   chips input (InputChips is deprecated). No suggestion source:
                   the value is whatever the admin types, one chip per Enter. -->
              <AutoComplete
                id="vb-allowlist"
                v-model="settings.vendor_bill_sender_allowlist"
                multiple
                :typeahead="false"
                class="w-full"
                data-test="vendor-bill-allowlist"
                placeholder="vendor.com"
                @blur="commitPendingSender"
              />
            </div>

            <Message v-if="allowlistEmpty" severity="warn" :closable="false">
              <strong>Vendor bill intake is off.</strong> With no senders listed,
              nothing is ingested and the sweep does nothing. Add the address or
              domain your supplier emails from — note that's the domain in their
              <em>From</em> line, which is often not the one in their company name.
            </Message>

            <Message v-if="broadEntries.length" severity="warn" :closable="false">
              <strong>{{ broadEntries.join(', ') }}</strong>
              {{ broadEntries.length === 1 ? 'is a consumer mail domain' : 'are consumer mail domains' }} —
              this allowlists <em>every</em> sender there, not just your vendor.
              Prefer their full address if you can.
            </Message>

            <div>
              <Button label="Save Allowlist" @click="saveSettings" />
            </div>

            <div class="sweep-block">
              <h3 class="font-medium">Import past email</h3>
              <p class="text-xs hint-text mb-2">
                New mail is picked up automatically, and a sweep runs nightly.
                Use this to reach further back — after adding a sender, for
                instance. It only looks at mail already synced to GDX, skips
                anything it has handled before, and is safe to run more than
                once; large windows may need a second run.
              </p>
              <div class="flex items-center gap-2 flex-wrap">
                <label class="text-sm" for="vb-sweep-days">Look back</label>
                <InputNumber
                  id="vb-sweep-days"
                  v-model="sweepDays"
                  :min="1"
                  :max="3650"
                  showButtons
                  class="sweep-days"
                />
                <span class="text-sm">days</span>
                <!-- outlined: plain `secondary` rendered as bare icon+text
                     against this panel, reading as a label rather than a
                     control. The border is what makes it look clickable. -->
                <Button
                  label="Run Sweep Now"
                  icon="pi pi-download"
                  severity="secondary"
                  outlined
                  :loading="sweeping"
                  :disabled="allowlistEmpty || allowlistDirty"
                  data-test="vendor-bill-sweep"
                  @click="runSweep"
                />
              </div>
              <p
                v-if="allowlistDirty && !allowlistEmpty"
                class="text-xs dirty-note mt-2"
                data-test="vendor-bill-dirty"
              >
                You've changed the sender list but haven't saved it. The sweep
                uses the saved list — save first, then run it.
              </p>
            </div>
          </div>
        </TabPanel>

      </TabPanels>
      </Tabs>
    </section>
</template>

<style scoped>
.outlook-settings {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}
.view-heading {
  margin: 0;
  font-size: 1.25rem;
  font-weight: 600;
}
.view-description {
  margin: 0;
  font-size: 0.9rem;
}
.text-muted, .hint-text {
  color: var(--p-text-muted-color);
}
.success-text {
  color: var(--p-green-500, #22c55e);
}
.sweep-block {
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
  padding: 0.75rem;
  /* Theme tokens only — this panel has to stay legible in dark mode too. */
  background: var(--p-content-hover-background);
}
.dirty-note {
  color: var(--p-orange-500, #f59e0b);
  margin: 0;
}
.sweep-days :deep(input) {
  /* Wide enough for a 4-digit window plus the stacked spinner arrows — at 5rem
     the default 120 rendered clipped as "12". */
  width: 7rem;
}
</style>
