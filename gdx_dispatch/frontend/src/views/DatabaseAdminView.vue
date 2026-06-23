<template>
  <section class="view-card p-4 space-y-4">
    <div class="flex items-center justify-between">
      <h2 class="text-xl font-semibold">Database</h2>
      <button class="px-3 py-1.5 text-sm rounded border border-gray-300 hover:bg-gray-50"
              :disabled="loading" @click="refresh">
        {{ loading ? 'Loading…' : 'Refresh' }}
      </button>
    </div>

    <p v-if="error" class="text-sm text-red-600">{{ error }}</p>

    <template v-if="status">
      <!-- Verdict banner -->
      <div class="rounded-lg p-3 text-sm" :class="verdictClass">
        <strong>{{ verdictTitle }}</strong> — {{ verdictDetail }}
      </div>

      <!-- Migration state -->
      <div class="rounded-lg border border-gray-200 p-3">
        <h3 class="font-medium mb-2">Migrations (control plane)</h3>
        <dl class="grid grid-cols-2 gap-y-1 text-sm">
          <dt class="text-gray-500">Current revision</dt><dd class="font-mono">{{ status.alembic.current || '—' }}</dd>
          <dt class="text-gray-500">Head revision</dt><dd class="font-mono">{{ status.alembic.head || '—' }}</dd>
          <dt class="text-gray-500">Pending</dt>
          <dd>{{ status.alembic.orphaned ? 'n/a (orphaned)' : status.alembic.pending_count }}</dd>
        </dl>

        <!-- Orphaned: detect-and-recommend, no destructive button -->
        <div v-if="status.alembic.orphaned" class="mt-3 rounded bg-red-50 border border-red-200 p-3 text-sm text-red-800">
          This DB is at a revision the current code no longer knows (a pre-squash install).
          It cannot be migrated forward. Resolve via CLI — re-pave or hand-migrate:
          <pre class="mt-1 bg-white/70 rounded p-2 text-xs overflow-x-auto">python -m gdx_dispatch.tools.bootstrap_app   # after dropping/recreating the DB (re-pave)</pre>
        </div>

        <div v-else class="mt-3 flex gap-2">
          <button class="px-3 py-1.5 text-sm rounded border border-gray-300 hover:bg-gray-50"
                  :disabled="busy || status.alembic.at_head" @click="loadPreview">Preview SQL</button>
          <button class="px-3 py-1.5 text-sm rounded bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
                  :disabled="busy || status.alembic.at_head" @click="openMigrate">Migrate to head</button>
        </div>
      </div>

      <!-- Drift -->
      <div class="rounded-lg border border-gray-200 p-3">
        <h3 class="font-medium mb-2">
          Schema drift (ORM vs live DB)
          <span v-if="status.tenant_drift.missing_count"
                class="ml-2 text-xs px-2 py-0.5 rounded-full bg-red-100 text-red-700">
            {{ status.tenant_drift.missing_count }} missing
          </span>
        </h3>
        <p v-if="!status.tenant_drift.total" class="text-sm text-gray-500">No drift — ORM matches the database.</p>
        <ul v-else class="text-xs font-mono space-y-0.5 max-h-72 overflow-y-auto">
          <li v-for="(d, i) in status.tenant_drift.items" :key="i" :class="severityClass(d.severity)">
            {{ d.detail }}
          </li>
        </ul>
      </div>

      <!-- Backup -->
      <div class="rounded-lg border border-gray-200 p-3">
        <h3 class="font-medium mb-2">Backup</h3>
        <p class="text-sm text-gray-600">
          Last backup:
          <span v-if="status.latest_backup">{{ status.latest_backup.file }}
            ({{ fmtSize(status.latest_backup.size_bytes) }}, {{ status.latest_backup.mtime?.slice(0,19).replace('T',' ') }} UTC)</span>
          <span v-else>none yet</span>
        </p>
        <button class="mt-2 px-3 py-1.5 text-sm rounded border border-gray-300 hover:bg-gray-50"
                :disabled="busy" @click="doBackup">{{ busy === 'backup' ? 'Backing up…' : 'Back up now (pg_dump)' }}</button>
      </div>

      <!-- SQL preview -->
      <div v-if="previewSql !== null" class="rounded-lg border border-gray-200 p-3">
        <h3 class="font-medium mb-2">Pending DDL preview</h3>
        <pre class="bg-gray-900 text-gray-100 rounded p-3 text-xs overflow-x-auto max-h-80">{{ previewSql || '(nothing to apply)' }}</pre>
      </div>

      <!-- Migrate confirm -->
      <div v-if="migrateOpen" class="rounded-lg border border-amber-300 bg-amber-50 p-3">
        <p class="text-sm text-amber-900">
          This backs up first, then applies pending migrations under a single-migrator lock.
          Type <strong>MIGRATE</strong> to confirm.
        </p>
        <div class="mt-2 flex gap-2">
          <input v-model="confirmText" placeholder="MIGRATE"
                 class="px-2 py-1 text-sm border border-gray-300 rounded font-mono" />
          <button class="px-3 py-1.5 text-sm rounded bg-amber-600 text-white hover:bg-amber-700 disabled:opacity-50"
                  :disabled="busy || confirmText !== 'MIGRATE'" @click="doMigrate">
            {{ busy === 'migrate' ? 'Migrating…' : 'Run migration' }}
          </button>
          <button class="px-3 py-1.5 text-sm rounded border border-gray-300" @click="migrateOpen=false">Cancel</button>
        </div>
      </div>
    </template>
  </section>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue';
import { useApiWithToast } from '../composables/useApiWithToast';

const api = useApiWithToast();
const status = ref(null);
const loading = ref(false);
const busy = ref('');            // '', 'backup', 'migrate'
const error = ref('');
const previewSql = ref(null);
const migrateOpen = ref(false);
const confirmText = ref('');

const verdict = computed(() => status.value?.verdict);
const verdictClass = computed(() => ({
  ok: 'bg-green-50 text-green-800 border border-green-200',
  pending: 'bg-blue-50 text-blue-800 border border-blue-200',
  drift: 'bg-amber-50 text-amber-900 border border-amber-200',
  orphaned: 'bg-red-50 text-red-800 border border-red-200',
}[verdict.value] || 'bg-gray-50 text-gray-700 border border-gray-200'));
const verdictTitle = computed(() => ({
  ok: 'Healthy', pending: 'Migrations pending', drift: 'Schema drift', orphaned: 'Orphaned revision',
}[verdict.value] || 'Unknown'));
const verdictDetail = computed(() => ({
  ok: 'At head, ORM matches the database.',
  pending: 'Control-plane migrations are available to apply.',
  drift: 'The ORM expects columns/tables the database is missing — some features will 500.',
  orphaned: 'DB revision is not in the migration tree; forward migration is blocked.',
}[verdict.value] || ''));

function severityClass(s) {
  return { missing: 'text-red-600', stale: 'text-gray-400', review: 'text-amber-600' }[s] || '';
}
function fmtSize(b) { return b > 1e6 ? (b / 1e6).toFixed(1) + ' MB' : Math.round(b / 1e3) + ' KB'; }

async function refresh() {
  loading.value = true; error.value = '';
  try { status.value = await api.get('/api/admin/db/status'); }
  catch (e) { error.value = e?.message || 'Failed to load status'; }
  finally { loading.value = false; }
}
async function loadPreview() {
  busy.value = 'preview';
  try { previewSql.value = (await api.get('/api/admin/db/preview'))?.sql ?? ''; }
  finally { busy.value = ''; }
}
async function doBackup() {
  busy.value = 'backup';
  try { await api.post('/api/admin/db/backup', {}, { successMessage: 'Backup created' }); await refresh(); }
  finally { busy.value = ''; }
}
function openMigrate() { migrateOpen.value = true; confirmText.value = ''; }
async function doMigrate() {
  busy.value = 'migrate';
  try {
    await api.post('/api/admin/db/migrate', { confirm: confirmText.value }, { successMessage: 'Migration applied' });
    migrateOpen.value = false; await refresh();
  } finally { busy.value = ''; }
}

onMounted(refresh);
</script>
