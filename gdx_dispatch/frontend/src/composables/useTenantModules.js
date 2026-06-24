import { computed, onMounted, ref } from 'vue';
import { MODULE_CATEGORIES } from '../constants/modules';
import { useApi } from './useApi';
import { useAuthStore } from '../stores/auth';

// 2026-04-29 nav-cleanup: every module that ships in MODULE_CATEGORIES is
// enabled by default. Tenants opt OUT via /api/settings/modules grants; the
// previous opt-in shape silently hid 30+ modules from every fresh tenant.
// Any module gated on a separate feature grant (phone_com, email/inbox, llm)
// uses the `requires` field on its module entry, not this map.
const DEFAULT_ENABLED_FALLBACK = true;

function normalizeEnabledModules(payload) {
  const modules = {};

  const applyRecord = (record) => {
    Object.entries(record || {}).forEach(([key, value]) => {
      if (typeof value === 'boolean') {
        modules[key] = value;
        return;
      }

      if (value && typeof value === 'object' && 'enabled' in value) {
        modules[key] = Boolean(value.enabled);
      }
    });
  };

  // The /api/settings/modules endpoint shape is
  //   { tenant_tier: "...", modules: [ { key, enabled, ... }, ... ] }
  // Array (no envelope) and bare-record fallbacks remain for older callers.
  const fromArray = (entries) => {
    entries.forEach((entry) => {
      if (typeof entry === 'string') {
        modules[entry] = true;
        return;
      }

      if (entry && typeof entry === 'object' && typeof entry.key === 'string') {
        modules[entry.key] = entry.enabled !== false;
      }
    });
  };

  if (Array.isArray(payload)) {
    fromArray(payload);
    return modules;
  }

  if (payload && typeof payload === 'object') {
    if (Array.isArray(payload.modules)) {
      // D101 sidebar fix (an earlier session, 2026-04-25): pre-fix this branch fed
      // the array into applyRecord, which iterated Object.entries() over it
      // and produced { "0": bool, "1": bool, ... } keyed by array index.
      // The merge with DEFAULT_ENABLED then left every real module key
      // showing as enabled — disabled modules stayed in the sidebar.
      fromArray(payload.modules);
    } else if (payload.modules && typeof payload.modules === 'object') {
      applyRecord(payload.modules);
    } else {
      applyRecord(payload);
    }
  }

  return modules;
}

// Module-level singleton state. Pre-fix every component that called
// useTenantModules() got its own refs and fired /api/settings/modules on
// mount, so the sidebar + 2-3 settings cards triple-fetched the same
// payload on every navigation (audit F-022/F-063). Now state is shared
// across callers and the fetch fires at most once per session unless a
// caller explicitly invokes loadTenantModules() to refresh after a save.
const _loading = ref(false);
const _enabledModules = ref({});
// Installed third-party plugins, from the /api/plugins catalog (ADR-013). Each
// entry: { key, name, tier, ui }. Drives a synthetic "Plugins" nav category.
const _plugins = ref([]);
let _loadPromise = null;

const _categories = computed(() => {
  // Permission filtering: opt-in. Module entries WITHOUT a `permission`
  // field stay visible; entries that name one are hidden when the user
  // lacks it. Auth store handles admin/owner escape hatch + wildcard.
  let _hasPerm = () => true;
  let _isOwner = false;
  try {
    const auth = useAuthStore();
    _hasPerm = (k) => auth.hasPermission(k);
    // Owner/superadmin get the "Manage plugins" install link (ADR-013 step 5);
    // matches the backend gate on /api/admin/plugins.
    _isOwner = ['owner', 'superadmin'].includes(auth.role);
  } catch (_e) {
    // useAuthStore() requires an active Pinia instance — during unit tests
    // without Pinia, fall through to the no-op so module filtering still works.
  }

  const base = MODULE_CATEGORIES.map((category) => {
    const modules = category.modules.filter((module) => {
      // `requires` (optional): the entry is gated on a different module
      // grant than its own key. Used when one backend module powers
      // multiple sidebar entries (e.g. phone_com → Calls + SMS views).
      const gateKey = module.requires || module.key;
      const explicit = _enabledModules.value[gateKey];
      // 2026-04-29 nav-cleanup: undefined → enabled. Tenants opt out, not in.
      const moduleEnabled = explicit === undefined ? DEFAULT_ENABLED_FALLBACK : Boolean(explicit);
      if (!moduleEnabled) return false;
      // Sprint role-permissions 2.2 — optional per-entry permission gate.
      if (module.permission && !_hasPerm(module.permission)) return false;
      return true;
    });
    return {
      ...category,
      modules,
    };
  }).filter((category) => category.modules.length > 0);

  // ADR-013: append a "Plugins" category for installed third-party plugins,
  // plus an owner-only "Manage plugins" install link. Per-request enablement is
  // enforced server-side by the proxy + each plugin's require_module; this is
  // just nav visibility (every installed plugin shows).
  const pluginModules = _plugins.value.map((p) => ({
    key: `plugin:${p.key}`,
    label: p.name || p.key,
    icon: 'pi pi-box',
    to: `/plugins/${p.key}`,
    type: 'Plugin',
  }));
  if (_isOwner) {
    pluginModules.push({
      key: 'plugins:manage',
      label: 'Manage plugins',
      icon: 'pi pi-cog',
      to: '/admin/plugins',
      type: 'Plugin',
    });
  }
  if (pluginModules.length) {
    base.push({ key: 'plugins', label: 'Plugins', icon: 'pi pi-box', modules: pluginModules });
  }
  return base;
});

const _allEnabledModules = computed(() => _categories.value.flatMap((category) => category.modules));

async function _doLoad(api) {
  // Skip the fetch pre-auth — the endpoint is behind login and would 403
  // on the /login page, cluttering DevTools and the R&D error feed.
  try {
    const { useAuthStore } = await import('../stores/auth');
    const auth = useAuthStore();
    if (!auth.isAuthenticated) {
      _enabledModules.value = {};
      return;
    }
  } catch (_e) {
    // If we can't resolve the store for any reason, still attempt the fetch.
  }

  _loading.value = true;
  try {
    const response = await api.get('/api/settings/modules');
    _enabledModules.value = normalizeEnabledModules(response);
  } catch (_error) {
    _enabledModules.value = {};
  } finally {
    _loading.value = false;
  }

  // Plugin catalog is best-effort: if plugin-host isn't deployed the proxy
  // errors, so a failure just means "no plugins" — never block module nav.
  try {
    const plugins = await api.get('/api/plugins');
    _plugins.value = Array.isArray(plugins) ? plugins : [];
  } catch (_error) {
    _plugins.value = [];
  }
}

export function useTenantModules({ refresh = false } = {}) {
  const api = useApi();

  async function loadTenantModules({ force = false } = {}) {
    if (force) _loadPromise = null;
    if (_loadPromise) return _loadPromise;
    _loadPromise = _doLoad(api).finally(() => {
      // Keep the resolved promise cached so repeat calls dedupe; a caller
      // who needs fresh data passes { force: true }.
    });
    return _loadPromise;
  }

  function isEnabled(moduleKey) {
    const explicit = _enabledModules.value[moduleKey];
    return explicit === undefined ? DEFAULT_ENABLED_FALLBACK : Boolean(explicit);
  }

  onMounted(() => {
    // First caller wins. Subsequent mounts share the same in-flight or
    // resolved promise, so /api/settings/modules fires once per session.
    loadTenantModules({ force: refresh });
  });

  return {
    loading: _loading,
    categories: _categories,
    allEnabledModules: _allEnabledModules,
    enabledModules: _enabledModules,
    isEnabled,
    loadTenantModules,
  };
}
