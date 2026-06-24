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

      <!-- Add a plugin package -->
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
        <Column field="tier" header="Tier" />
      </DataTable>
    </template>
  </section>
</template>

<script setup>
// Owner-only in-app plugin install UI (ADR-013 step 5). Writes the desired
// state to plugin_registry via /api/admin/plugins; plugin-host materializes it
// on restart. "Running now" reads the live catalog so the operator can see the
// gap between what's registered and what's actually loaded.
import { computed, onMounted, reactive, ref } from 'vue';
import Toolbar from 'primevue/toolbar';
import Message from 'primevue/message';
import InputText from 'primevue/inputtext';
import Button from 'primevue/button';
import DataTable from 'primevue/datatable';
import Column from 'primevue/column';
import { useToast } from 'primevue/usetoast';
import { useApiWithToast } from '../composables/useApiWithToast';
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
import { useAuthStore } from '../stores/auth';

const api = useApiWithToast();
const toast = useToast();
const { confirmAsync } = useDestructiveConfirm();
const auth = useAuthStore();

// Match the backend gate exactly (_OWNER_ROLES = {owner, superadmin}); `admin`
// is intentionally excluded — an admin would see the form but 403 on submit.
const isOwner = computed(() => ['owner', 'superadmin'].includes(auth.role));

const registry = ref([]);
const running = ref([]);
const loading = ref(false);
const saving = ref(false);
const restarting = ref(false);
const form = reactive({ package: '', version: '' });

async function loadRegistry() {
  registry.value = (await api.get('/api/admin/plugins')) || [];
}

async function load() {
  if (!isOwner.value) return;
  loading.value = true;
  try {
    await loadRegistry();
    // Best-effort: plugin-host may not be up yet. A failure just means none.
    try { running.value = (await api.get('/api/plugins')) || []; }
    catch (_e) { running.value = []; }
  } finally {
    loading.value = false;
  }
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
</style>
