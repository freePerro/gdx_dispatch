<script setup>
import { ref, onMounted, computed } from 'vue'
import Toolbar from 'primevue/toolbar'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import InputNumber from 'primevue/inputnumber'
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
  AUTO_EMAIL: 'auto_email',
}
const activeTab = ref(TAB_KEYS.CONNECTION)

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
      auto_email_triggers: settings.value.auto_email_triggers,
    })
    toast.add({ severity: 'success', summary: 'Saved', detail: 'Outlook settings updated.', life: 3000 })
  } catch (err) {
    toast.add({ severity: 'error', summary: 'Save failed', detail: err?.message || 'Unknown error', life: 5000 })
  }
}

onMounted(() => {
  load()
})

defineExpose({ load, saveCredentials, saveSettings, clearSecret })
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
        <Tab :value="TAB_KEYS.AUTO_EMAIL">Auto-Email</Tab>
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

        <!-- Auto-Email -->
        <TabPanel :value="TAB_KEYS.AUTO_EMAIL">
          <div class="flex flex-col gap-4 mt-4">
            <div class="ae-inactive-notice" data-test="auto-email-inactive">
              <strong>⚠ Not active yet.</strong> These auto-email triggers are
              <strong>not currently wired to any event</strong> — saving a template here
              does not send anything. Estimate/invoice emails are already sent by the
              existing send flows; whether to enable this separate per-user automation is
              still under review. Configure it only once it's turned on.
            </div>
            <p class="text-sm hint-text">
              Configure templates that auto-email users on domain events. Each user
              opts in per trigger from their Profile. Templates use Mustache-style
              <code>&#123;&#123;customer.name&#125;&#125;</code> placeholders.
            </p>
            <div v-for="trigger in ['invoice.created', 'job.completed', 'estimate.sent']" :key="trigger"
                 class="trigger-block">
              <h3 class="font-medium">{{ trigger }}</h3>
              <div class="mt-2">
                <label class="text-sm">Subject template</label>
                <InputText v-model="settings.auto_email_triggers[trigger].subject"
                           class="w-full" placeholder="e.g. Invoice {{invoice.number}}" />
              </div>
              <div class="mt-2">
                <label class="text-sm">Body template (HTML)</label>
                <textarea v-model="settings.auto_email_triggers[trigger].template"
                          class="w-full p-2 template-textarea text-sm"
                          rows="4" />
              </div>
            </div>
            <div>
              <Button label="Save Auto-Email Templates" @click="saveSettings" />
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
.trigger-block {
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
  padding: 0.75rem;
  background: var(--p-content-hover-background);
}
.ae-inactive-notice {
  border: 1px solid var(--p-content-border-color);
  border-left: 4px solid #d97706;
  border-radius: 6px;
  padding: 0.6rem 0.8rem;
  background: var(--p-content-hover-background);
  color: var(--p-text-color);
  font-size: 0.85rem;
  line-height: 1.45;
}
.template-textarea {
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: var(--p-content-background);
  color: var(--p-text-color);
}
</style>
