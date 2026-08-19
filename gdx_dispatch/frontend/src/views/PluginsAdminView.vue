<template>
  <section class="plugins-admin-view view-card" data-testid="plugins-admin">
    <Toolbar>
      <template #start>
        <h2 class="page-title">Plugins</h2>
      </template>
      <template v-if="isOwner" #end>
        <Button label="Restart plugin-host" icon="pi pi-refresh" severity="secondary"
                :loading="restarting" @click="restartHost" />
      </template>
    </Toolbar>

    <Message v-if="!isOwner" severity="warn" :closable="false">
      Installing plugins is owner-only.
    </Message>

    <template v-else>
      <Message severity="info" :closable="false" class="restart-note">
        Installing or removing a plugin records intent. Hit
        <strong>Restart plugin-host</strong> to apply: it pip-installs the
        package and mounts its routes. The core app keeps serving — only
        plugin-host cycles (~10s).
      </Message>

      <!-- Browse the curated catalog -->
      <h3 class="section-title">Browse plugins</h3>
      <Message v-if="store.error" severity="warn" :closable="false" data-testid="store-error">
        The plugin catalog is unreachable, so there's nothing to browse right now.
        Installing by package name or file below still works.
        <br><small>{{ store.error }}</small>
      </Message>
      <div v-else-if="store.loading" class="store-empty">Loading the catalog…</div>
      <div v-else-if="!store.plugins.length" class="store-empty">
        The catalog has no plugins listed yet.
      </div>
      <div v-else class="store-grid" data-testid="store-grid">
        <div v-for="p in store.plugins" :key="p.key" class="store-card" :data-plugin="p.key">
          <div class="store-card-head">
            <span class="store-name">{{ p.name }}</span>
            <Tag :value="p.tier" severity="secondary" rounded />
          </div>
          <p class="store-desc">{{ p.description }}</p>
          <!-- Permissions are shown BEFORE install, not after: an owner should
               know what a plugin will be able to do before its code is on the
               box, not at first use. -->
          <div class="store-perms">
            <template v-if="p.permissions?.length">
              <Tag v-for="perm in p.permissions" :key="perm" :value="perm"
                   severity="warn" rounded class="perm-chip" />
              <small class="perm-note">asks for elevated access</small>
            </template>
            <small v-else class="perm-note">no elevated permissions</small>
          </div>
          <div class="store-card-foot">
            <span class="store-state">
              <template v-if="p.state === 'running'">
                <i class="pi pi-check-circle" /> Running v{{ p.running_version }}
              </template>
              <template v-else-if="p.state === 'pending_restart'">
                <i class="pi pi-clock" /> v{{ p.installed_version }} — restart to load
              </template>
              <template v-else>v{{ p.version }}</template>
            </span>
            <span class="store-actions">
              <!-- No dead end: once it's loaded, there's a way into it. -->
              <Button v-if="p.state === 'running'" label="Open" icon="pi pi-arrow-right"
                      size="small" text @click="openPlugin(p)" />
              <Button v-if="p.state === 'pending_restart'" label="Restart now"
                      icon="pi pi-refresh" size="small" severity="secondary"
                      :loading="restarting" @click="restartHost" />
              <Button v-if="p.update_available" :label="`Update to ${p.version}`"
                      icon="pi pi-arrow-up" size="small" @click="askInstall(p)" />
              <Button v-else-if="p.state === 'available'" label="Install"
                      icon="pi pi-download" size="small" @click="askInstall(p)" />
            </span>
          </div>
        </div>
      </div>

      <!-- Install confirmation. A real modal on purpose: useDestructiveConfirm
           auto-accepts silently (issue #215), which here would turn Install into
           unconfirmed code execution AND skip the permission review. -->
      <Dialog v-model:visible="storeConfirm.show" modal :style="{ width: '34rem' }"
              :header="`Install ${storeConfirm.plugin?.name || ''}?`">
        <p>
          This installs <strong>{{ storeConfirm.plugin?.distribution }}</strong>
          v{{ storeConfirm.plugin?.version }} from the GDX plugin catalog.
        </p>
        <p v-if="storeConfirm.plugin?.permissions?.length">
          It asks for:
          <Tag v-for="perm in storeConfirm.plugin.permissions" :key="perm" :value="perm"
               severity="warn" rounded class="perm-chip" />
          You'll be asked to consent before those can be used.
        </p>
        <Message severity="warn" :closable="false">
          Installing runs the plugin's code on your plugin-host. Only install
          plugins you trust.
        </Message>
        <template #footer>
          <Button label="Cancel" text @click="storeConfirm.show = false" />
          <Button :label="storeConfirm.plugin?.installed_version ? 'Update' : 'Install'"
                  icon="pi pi-download" :loading="storeConfirm.saving"
                  data-testid="confirm-install" @click="confirmInstall" />
        </template>
      </Dialog>

      <!-- Add a plugin package -->
      <h3 class="section-title">Install by package name or file</h3>
      <form class="install-form" @submit.prevent="install">
        <div class="form-field">
          <label for="pkg" class="form-label">Package *</label>
          <InputText id="pkg" v-model.trim="form.package" class="w-full"
                     placeholder="gdx-plugin-example" maxlength="200" />
        </div>
        <div class="form-field">
          <label for="ver" class="form-label">Version</label>
          <InputText id="ver" v-model.trim="form.version" class="w-full"
                     placeholder="(latest)" maxlength="50" />
        </div>
        <Button type="submit" label="Install" icon="pi pi-download"
                :loading="saving" :disabled="!form.package" />
      </form>

      <!-- Upload a private plugin file (not on a package index) -->
      <form class="install-form" @submit.prevent="uploadFile">
        <div class="form-field" style="flex: 1 1 20rem;">
          <label for="pkgfile" class="form-label">Upload plugin file (.whl / .tar.gz)</label>
          <input id="pkgfile" ref="fileInput" type="file" accept=".whl,.tar.gz"
                 class="w-full" @change="onPick" />
        </div>
        <Button type="submit" label="Upload" icon="pi pi-upload"
                :loading="uploading" :disabled="!picked" />
      </form>

      <!-- Uploaded artifacts -->
      <h3 class="section-title">Uploaded plugins</h3>
      <DataTable :value="artifacts" :loading="loading" dataKey="filename" responsiveLayout="scroll">
        <template #empty>No uploaded plugin files.</template>
        <Column field="filename" header="File" />
        <Column field="sha256" header="SHA-256">
          <template #body="{ data }">{{ (data.sha256 || '').slice(0, 12) }}…</template>
        </Column>
        <Column header="" :style="{ width: '80px' }">
          <template #body="{ data }">
            <Button icon="pi pi-trash" severity="danger" text size="small"
                    v-tooltip="`Remove ${data.filename}`"
                    :aria-label="`Remove ${data.filename}`" @click="removeArtifact(data)" />
          </template>
        </Column>
      </DataTable>

      <!-- Installed (desired-state registry) -->
      <h3 class="section-title">Installed packages</h3>
      <DataTable :value="registry" :loading="loading" dataKey="package" responsiveLayout="scroll">
        <template #empty>No plugin packages installed yet.</template>
        <Column field="package" header="Package" />
        <Column field="version" header="Version">
          <template #body="{ data }">{{ data.version || 'latest' }}</template>
        </Column>
        <Column header="" :style="{ width: '80px' }">
          <template #body="{ data }">
            <Button icon="pi pi-trash" severity="danger" text size="small"
                    v-tooltip="`Remove ${data.package}`"
                    :aria-label="`Remove ${data.package}`" @click="remove(data)" />
          </template>
        </Column>
      </DataTable>

      <!-- Running (live catalog from plugin-host) -->
      <h3 class="section-title">Running now</h3>
      <DataTable :value="running" :loading="loading" dataKey="key" responsiveLayout="scroll">
        <template #empty>No plugins loaded by plugin-host.</template>
        <Column field="key" header="Key" />
        <Column field="name" header="Name" />
        <!-- The version actually loaded in the plugin-host process. Deliberately
             NOT the version the install tables say should be there: those can
             disagree, and this column is the one that reports what is really
             running. -->
        <Column header="Version">
          <template #body="{ data }">
            <span v-if="data.version">{{ data.version }}</span>
            <span v-else class="muted" title="This plugin was not installed from a package">unknown</span>
          </template>
        </Column>
        <Column field="tier" header="Tier" />
        <Column header="Permissions">
          <template #body="{ data }">
            <span v-if="!data.permissions || !data.permissions.length">—</span>
            <Button v-else label="Review & consent" size="small" text icon="pi pi-shield"
                    @click="openConsent(data)" />
          </template>
        </Column>
      </DataTable>

      <!-- ADR-014 consent dialog: a plugin's elevated permissions must be
           consented to before they can be used (e.g. the browser stream). -->
      <Dialog v-model:visible="consent.show" :header="`Permissions — ${consent.name}`"
              modal :style="{ width: '34rem' }">
        <p>
          This plugin requests elevated capabilities. An approved plugin runs with
          backend access — only consent to plugins you trust.
        </p>
        <ul class="consent-list">
          <li v-for="p in consent.items" :key="p.name">
            <strong>{{ p.name }}</strong>
            <span :class="['consent-badge', p.consented ? 'is-on' : 'is-off']">
              {{ p.consented ? 'consented' : 'not consented' }}
            </span>
            <p class="consent-risk">{{ p.risk }}</p>
          </li>
        </ul>
        <template #footer>
          <Button label="Close" text @click="consent.show = false" />
          <Button :label="consent.allConsented ? 'Re-consent' : 'Grant consent'"
                  icon="pi pi-check" :loading="consent.saving"
                  :disabled="!consent.items.length" @click="grantConsent" />
        </template>
      </Dialog>
    </template>
  </section>
</template>

<script setup>
// Owner-only in-app plugin install UI (ADR-013 step 5). Writes the desired
// state to plugin_registry via /api/admin/plugins; plugin-host materializes it
// on restart. "Running now" reads the live catalog so the operator can see the
// gap between what's registered and what's actually loaded.
import { computed, onMounted, reactive, ref } from 'vue';
import { useRouter } from 'vue-router';
import Toolbar from 'primevue/toolbar';
import Message from 'primevue/message';
import InputText from 'primevue/inputtext';
import Button from 'primevue/button';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import Dialog from 'primevue/dialog';
import Tag from 'primevue/tag';
import { useToast } from 'primevue/usetoast';
import { useApiWithToast } from '../composables/useApiWithToast';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
import { useAuthStore } from '../stores/auth';
import { isOwner as isOwnerRole } from '../constants/roles';

const api = useApiWithToast();
const toast = useToast();
const { confirmAsync } = useDestructiveConfirm();
const auth = useAuthStore();
const router = useRouter();

// Match the backend gate exactly (_OWNER_ROLES = {owner, superadmin}); `admin`
// is intentionally excluded — an admin would see the form but 403 on submit.
const isOwner = computed(() => isOwnerRole(auth.role));

const registry = ref([]);
const artifacts = ref([]);
const running = ref([]);
const loading = ref(false);
const saving = ref(false);
const uploading = ref(false);
const restarting = ref(false);
const form = reactive({ package: '', version: '' });
const fileInput = ref(null);
const picked = ref(null);
const consent = reactive({ show: false, key: '', name: '', items: [], allConsented: false, saving: false });
const store = reactive({ plugins: [], error: null, loading: false });
const storeConfirm = reactive({ show: false, plugin: null, saving: false });

async function loadStore() {
  store.loading = true;
  try {
    const r = await api.get('/api/admin/plugins/storefront');
    store.plugins = r?.plugins || [];
    store.error = r?.error || null;
  } catch (e) {
    // Say the catalog is unreachable rather than rendering an empty store —
    // "no plugins listed" and "we couldn't ask" are different statements.
    store.plugins = [];
    store.error = e?.message || 'the catalog could not be read';
  } finally {
    store.loading = false;
  }
}

function askInstall(plugin) {
  storeConfirm.plugin = plugin;
  storeConfirm.show = true;
}

async function confirmInstall() {
  const p = storeConfirm.plugin;
  if (!p) return;
  storeConfirm.saving = true;
  try {
    await api.post('/api/admin/plugins/storefront/install',
      { key: p.key, version: p.version },
      { successMessage: `${p.name} recorded — restart plugin-host to load it` });
    storeConfirm.show = false;
    await Promise.all([loadStore(), loadArtifacts()]);
  } finally {
    storeConfirm.saving = false;
  }
}

function openPlugin(plugin) {
  router.push(`/plugins/${encodeURIComponent(plugin.key)}`);
}

async function loadRegistry() {
  registry.value = (await api.get('/api/admin/plugins')) || [];
}

async function _loadPermissions(key) {
  const r = await api.get(`/api/admin/plugins/${encodeURIComponent(key)}/permissions`);
  consent.items = r?.permissions || [];
  consent.allConsented = !!r?.all_consented;
}

async function openConsent(plugin) {
  consent.key = plugin.key;
  consent.name = plugin.name;
  consent.items = [];
  consent.allConsented = false;
  await _loadPermissions(plugin.key);
  consent.show = true;
}

async function grantConsent() {
  consent.saving = true;
  try {
    await api.post(`/api/admin/plugins/${encodeURIComponent(consent.key)}/consent`, {},
      { successMessage: `Consent recorded for ${consent.name}` });
    await _loadPermissions(consent.key);
  } finally {
    consent.saving = false;
  }
}

async function loadArtifacts() {
  artifacts.value = (await api.get('/api/admin/plugins/artifacts')) || [];
}

async function load() {
  if (!isOwner.value) return;
  loading.value = true;
  try {
    await loadRegistry();
    await loadArtifacts();
    await loadStore();
    // Best-effort: plugin-host may not be up yet. A failure just means none.
    try { running.value = (await api.get('/api/plugins')) || []; }
    catch (_e) { running.value = []; }
  } finally {
    loading.value = false;
  }
}

function onPick(e) {
  picked.value = e.target.files?.[0] || null;
}

async function uploadFile() {
  if (!picked.value) return;
  uploading.value = true;
  try {
    const fd = new FormData();
    fd.append('file', picked.value);
    await api.post('/api/admin/plugins/upload', fd,
      { successMessage: `Uploaded ${picked.value.name} — restart plugin-host to install` });
    picked.value = null;
    if (fileInput.value) fileInput.value.value = '';
    await loadArtifacts();
  } finally {
    uploading.value = false;
  }
}

async function removeArtifact(row) {
  const ok = await confirmAsync({
    message: `Remove ${row.filename}? It stays installed until plugin-host restarts.`,
    header: 'Remove uploaded plugin',
  });
  if (!ok) return;
  await api.del(`/api/admin/plugins/artifacts/${encodeURIComponent(row.filename)}`,
    { successMessage: `Removed ${row.filename}` });
  await loadArtifacts();
}

async function install() {
  if (!form.package) return;
  saving.value = true;
  try {
    await api.post('/api/admin/plugins',
      { package: form.package, version: form.version || null },
      { successMessage: `Registered ${form.package} — restart plugin-host to apply` });
    form.package = '';
    form.version = '';
    await loadRegistry();
  } finally {
    saving.value = false;
  }
}

async function remove(row) {
  const ok = await confirmAsync({
    message: `Remove ${row.package}? It stays running until plugin-host restarts.`,
    header: 'Remove plugin',
  });
  if (!ok) return;
  await api.del(`/api/admin/plugins/${encodeURIComponent(row.package)}`,
    { successMessage: `Unregistered ${row.package}` });
  await loadRegistry();
}

async function restartHost() {
  restarting.value = true;
  try {
    await api.post('/api/admin/plugins/restart', {},
      { successMessage: 'Restarting plugin-host — applying changes…' });
    const back = await waitForHost();
    await load();
    // Don't leave the operator with a false "applying…" sense of success: if the
    // host never answered, it likely crash-looped on a bad plugin — tell them to
    // check the logs rather than silently stopping.
    if (!back) {
      toast.add({
        severity: 'warn',
        summary: 'plugin-host not responding',
        detail: 'It did not come back within 30s — it may be crash-looping on a plugin. Check container logs.',
        life: 8000,
      });
    }
  } finally {
    restarting.value = false;
  }
}

// plugin-host drops for a few seconds while Docker recreates it. Poll the
// catalog silently (suppressErrorToast) until it answers again, ~30s max.
async function waitForHost(tries = 15, delayMs = 2000) {
  for (let i = 0; i < tries; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    try {
      await api.get('/api/plugins', { suppressErrorToast: true });
      return true;
    } catch (_e) { /* still cycling — keep waiting */ }
  }
  return false;
}

onMounted(load);
</script>

<style scoped>
.install-form {
  display: flex;
  gap: 1rem;
  align-items: flex-end;
  flex-wrap: wrap;
  margin: 1rem 0;
}
.install-form .form-field { flex: 1 1 12rem; }
.form-label { display: block; margin-bottom: 0.25rem; font-weight: 600; }
.section-title { margin: 1.5rem 0 0.5rem; }
.restart-note { margin-bottom: 0.5rem; }
/* PrimeVue v4 token, not the v3 `--text-color-secondary` (undefined here, so
   the text would render un-muted). Guarded by no_legacy_css_tokens.spec.js. */
.muted { color: var(--p-text-muted-color); }

/* Storefront. All colours are theme tokens — this grid is read in dark mode
   as often as light, and a hardcoded card background is the classic way that
   breaks. */
.store-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(17rem, 1fr));
  gap: 1rem;
  margin-bottom: 1rem;
}
.store-card {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding: 1rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: var(--p-content-border-radius, 6px);
  background: var(--p-content-background);
}
.store-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}
.store-name { font-weight: 600; }
.store-desc {
  margin: 0;
  color: var(--p-text-muted-color);
  font-size: 0.9rem;
  /* Descriptions vary in length; the cards should still line up. */
  min-height: 2.6rem;
}
.store-perms { display: flex; flex-wrap: wrap; align-items: center; gap: 0.35rem; }
.perm-chip { margin-right: 0.25rem; }
.perm-note { color: var(--p-text-muted-color); }
.store-card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin-top: auto;
}
.store-state { color: var(--p-text-muted-color); font-size: 0.9rem; }
.store-actions { display: flex; gap: 0.25rem; flex-wrap: wrap; }
.store-empty { color: var(--p-text-muted-color); margin-bottom: 1rem; }
</style>
