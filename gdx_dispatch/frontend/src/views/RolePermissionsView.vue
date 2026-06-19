<template>
    <section class="role-permissions-view view-card">
      <Message
        v-if="bannerPending"
        severity="warn"
        :closable="false"
        class="mb-3"
      >
        <div class="banner-row">
          <span>Your role permissions have been reset to platform defaults — review and re-customize where needed.</span>
          <Button label="Got it" size="small" :loading="ackingBanner" @click="ackBanner" />
        </div>
      </Message>

      <Tabs value="roles">
        <TabList>
          <Tab value="roles">Roles</Tab>
          <Tab value="users">User Assignments</Tab>
        </TabList>
        <TabPanels>
          <TabPanel value="roles">
            <Toolbar>
              <template #start>
                <h2 class="page-title">Roles</h2>
              </template>
              <template #end>
                <Button label="+ New Role" icon="pi pi-plus" @click="openCreateRole" />
              </template>
            </Toolbar>

            <div v-if="loadingRoles" class="spinner-wrap"><ProgressSpinner /></div>

            <DataTable
              v-else
              :value="roles"
              striped-rows
              responsiveLayout="scroll"
              emptyMessage="No roles available"
            >
              <Column field="name" header="Name">
                <template #body="{ data }">
                  <span>{{ data.name }}</span>
                  <span v-if="data.is_system" class="muted ms-2">system</span>
                </template>
              </Column>
              <Column field="description" header="Description">
                <template #body="{ data }">
                  <p class="muted">{{ data.description || "—" }}</p>
                </template>
              </Column>
              <Column header="Permissions" style="width: 220px">
                <template #body="{ data }">
                  <Badge :value="permissionCountLabel(data)" severity="info" />
                  <span class="muted ms-2">{{ permissionCategorySummary(data) }}</span>
                </template>
              </Column>
              <Column header="Actions" style="width: 280px">
                <template #body="{ data }">
                  <Button
                    v-if="!data.is_platform_locked"
                    label="Edit"
                    text
                    size="small"
                    class="me-1"
                    @click.stop="openEditRole(data)"
                  />
                  <span
                    v-else
                    class="muted me-1"
                    :title="`${data.name} is a platform-contract role — clone to customize`"
                  >
                    Locked
                  </span>
                  <Button
                    v-if="data.is_system && !data.is_platform_locked"
                    label="Reset"
                    text
                    size="small"
                    class="me-1"
                    :loading="resetting[data.id]"
                    @click.stop="resetRole(data)"
                  />
                  <Button
                    v-if="!data.is_system"
                    label="Delete"
                    text
                    severity="danger"
                    size="small"
                    @click.stop="deleteRole(data)"
                  />
                </template>
              </Column>
            </DataTable>
          </TabPanel>

          <TabPanel value="users">
            <Toolbar>
              <template #start>
                <h2 class="page-title">User Assignments</h2>
              </template>
              <template #end>
                <InputText
                  v-model="userSearch"
                  placeholder="Search by name or email"
                  class="user-search"
                />
              </template>
            </Toolbar>

            <div v-if="usersLoading" class="spinner-wrap"><ProgressSpinner /></div>

            <DataTable
              v-else
              :value="filteredUsers"
              striped-rows
              responsiveLayout="scroll"
              emptyMessage="No users match"
            >
              <Column header="User">
                <template #body="{ data }">
                  <div>
                    <div>{{ data.name || data.username || data.email || data.id }}</div>
                    <div class="muted">{{ data.email }}</div>
                  </div>
                </template>
              </Column>
              <Column header="Current Roles">
                <template #body="{ data }">
                  <div class="role-chips">
                    <span
                      v-for="rid in (userRoles[data.id] || [])"
                      :key="rid"
                      class="role-chip"
                    >
                      {{ roleName(rid) }}
                    </span>
                    <span v-if="!(userRoles[data.id] || []).length" class="muted">none</span>
                  </div>
                </template>
              </Column>
              <Column header="Assign / Replace" style="width: 360px">
                <template #body="{ data }">
                  <div class="assign-row">
                    <Select
                      :modelValue="pendingRoleByUser[data.id]"
                      @update:modelValue="(v) => (pendingRoleByUser[data.id] = v)"
                      :options="roleOptions"
                      optionLabel="label"
                      optionValue="value"
                      placeholder="Pick a role"
                      filter
                      class="assign-select"
                    />
                    <Button
                      label="Save"
                      icon="pi pi-check"
                      size="small"
                      :loading="assigning[data.id]"
                      :disabled="!pendingRoleByUser[data.id]"
                      @click="assignRoleToUser(data)"
                    />
                  </div>
                </template>
              </Column>
            </DataTable>
          </TabPanel>
        </TabPanels>
      </Tabs>

      <Dialog
        v-model:visible="showDialog"
        :header="dialogTitle"
        modal
        :style="{ width: '720px' }"
      >
        <div class="form-field">
          <label for="role-name">Name</label>
          <InputText
            id="role-name"
            v-model="roleForm.name"
            class="w-full"
            :disabled="editingRole && editingRole.is_system"
          />
        </div>
        <div class="form-field">
          <label for="role-description">Description</label>
          <Textarea
            id="role-description"
            v-model="roleForm.description"
            rows="2"
            class="w-full"
          />
        </div>

        <div class="form-field">
          <label>Permissions</label>
          <div class="grid-toolbar muted">
            <span>{{ effectiveSummary }}</span>
            <span class="grid-toolbar-spacer"></span>
            <Button
              v-if="hasWildcard"
              label="Clear wildcard"
              text
              size="small"
              @click="clearWildcard"
            />
            <Button
              v-else
              label="Grant all (*)"
              text
              size="small"
              @click="grantWildcard"
            />
          </div>
          <div class="permission-grid">
            <div
              v-for="cat in catalogByCategory"
              :key="cat.category"
              class="permission-category"
            >
              <div class="category-header">
                <strong>{{ formatCategory(cat.category) }}</strong>
                <span class="muted">{{ countSelectedInCategory(cat) }} / {{ cat.items.length }}</span>
              </div>
              <div
                v-for="perm in cat.items"
                :key="perm.key"
                class="permission-row"
              >
                <Checkbox
                  :modelValue="isPermissionSelected(perm.key)"
                  :binary="true"
                  :disabled="hasWildcard"
                  @update:modelValue="(v) => togglePermission(perm.key, v)"
                  :inputId="`perm-${perm.key}`"
                />
                <label :for="`perm-${perm.key}`" class="permission-label">
                  <span>{{ perm.label }}</span>
                  <code class="muted">{{ perm.key }}</code>
                </label>
              </div>
            </div>
          </div>
        </div>

        <div v-if="roleError" class="inline-error">{{ roleError }}</div>
        <template #footer>
          <Button label="Cancel" text @click="showDialog = false" />
          <Button label="Save" icon="pi pi-check" :loading="savingRole" @click="saveRole" />
        </template>
      </Dialog>
    </section>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useApiWithToast } from "../composables/useApiWithToast";
import Badge from "primevue/badge";
import Button from "primevue/button";
import Checkbox from "primevue/checkbox";
import Column from "primevue/column";
import DataTable from "primevue/datatable";
import Dialog from "primevue/dialog";
import InputText from "primevue/inputtext";
import Message from "primevue/message";
import ProgressSpinner from "primevue/progressspinner";
import Select from "primevue/select";
import Tab from "primevue/tab";
import TabList from "primevue/tablist";
import TabPanel from "primevue/tabpanel";
import TabPanels from "primevue/tabpanels";
import Tabs from "primevue/tabs";
import Textarea from "primevue/textarea";
import Toolbar from "primevue/toolbar";
import { useDestructiveConfirm } from '../composables/useDestructiveConfirm';
const { confirmAsync } = useDestructiveConfirm();

const api = useApiWithToast();

const WILDCARD = "*";

const roles = ref([]);
const catalog = ref([]); // [{key, label, category}]
const loadingRoles = ref(false);
const showDialog = ref(false);
const savingRole = ref(false);
const editingRole = ref(null);
const roleForm = reactive({ name: "", description: "", permissions: [] });
const roleError = ref("");
const resetting = reactive({});

const users = ref([]);
const usersLoading = ref(false);
const userRoles = reactive({}); // userId -> [roleId, ...]
const pendingRoleByUser = reactive({});
const assigning = reactive({});
const userSearch = ref("");

const bannerPending = ref(false);
const ackingBanner = ref(false);

const roleOptions = computed(() => roles.value.map((role) => ({ label: role.name, value: role.id })));

const dialogTitle = computed(() => (editingRole.value ? `Edit ${editingRole.value.name}` : "New role"));

const hasWildcard = computed(() => roleForm.permissions.includes(WILDCARD));

const catalogByCategory = computed(() => {
  const buckets = new Map();
  catalog.value.forEach((perm) => {
    if (!buckets.has(perm.category)) buckets.set(perm.category, []);
    buckets.get(perm.category).push(perm);
  });
  return Array.from(buckets.entries()).map(([category, items]) => ({ category, items }));
});

const effectiveSummary = computed(() => {
  if (hasWildcard.value) return "Wildcard granted — full access to every permission.";
  const total = catalog.value.length;
  const selected = roleForm.permissions.filter((p) => p !== WILDCARD).length;
  return `${selected} of ${total} permissions selected.`;
});

const filteredUsers = computed(() => {
  const q = userSearch.value.trim().toLowerCase();
  if (!q) return users.value;
  return users.value.filter((u) => {
    const haystack = `${u.name || ""} ${u.username || ""} ${u.email || ""}`.toLowerCase();
    return haystack.includes(q);
  });
});

function permissionCountLabel(role) {
  if ((role.permissions || []).includes(WILDCARD)) return "all (*)";
  return String((role.permissions || []).length);
}

function permissionCategorySummary(role) {
  if ((role.permissions || []).includes(WILDCARD)) return "every category";
  const cats = new Set();
  (role.permissions || []).forEach((p) => {
    const found = catalog.value.find((c) => c.key === p);
    if (found) cats.add(found.category);
  });
  if (!cats.size) return "—";
  return Array.from(cats).map(formatCategory).join(", ");
}

function formatCategory(category) {
  if (!category) return "";
  return category.charAt(0).toUpperCase() + category.slice(1).replace(/_/g, " ");
}

function isPermissionSelected(key) {
  if (hasWildcard.value) return true;
  return roleForm.permissions.includes(key);
}

function togglePermission(key, value) {
  if (value) {
    if (!roleForm.permissions.includes(key)) roleForm.permissions.push(key);
  } else {
    roleForm.permissions = roleForm.permissions.filter((p) => p !== key);
  }
}

function grantWildcard() {
  roleForm.permissions = [WILDCARD];
}

function clearWildcard() {
  roleForm.permissions = [];
}

function countSelectedInCategory(cat) {
  if (hasWildcard.value) return cat.items.length;
  return cat.items.filter((p) => roleForm.permissions.includes(p.key)).length;
}

function roleName(roleId) {
  const r = roles.value.find((x) => x.id === roleId);
  return r ? r.name : roleId;
}

async function loadRoles() {
  loadingRoles.value = true;
  try {
    const data = await api.get("/api/role-permissions/roles");
    roles.value = Array.isArray(data) ? data : data?.items || [];
  } finally {
    loadingRoles.value = false;
  }
}

async function loadCatalog() {
  try {
    const data = await api.get("/api/role-permissions/permissions/catalog");
    catalog.value = Array.isArray(data) ? data : [];
  } catch {
    catalog.value = [];
  }
}

async function loadUsers() {
  usersLoading.value = true;
  try {
    const data = await api.get("/api/admin/users");
    users.value = Array.isArray(data) ? data : data?.items || [];
    await Promise.all(users.value.map((u) => loadUserRoles(u.id)));
  } finally {
    usersLoading.value = false;
  }
}

async function loadUserRoles(userId) {
  try {
    const data = await api.get(`/api/role-permissions/users/${encodeURIComponent(userId)}/roles`);
    const list = Array.isArray(data) ? data : data?.items || [];
    userRoles[userId] = list.map((r) => r.role_id);
  } catch {
    userRoles[userId] = [];
  }
}

async function loadBanner() {
  try {
    const data = await api.get("/api/role-permissions/migration-banner");
    bannerPending.value = Boolean(data?.pending);
  } catch {
    bannerPending.value = false;
  }
}

async function ackBanner() {
  ackingBanner.value = true;
  try {
    await api.post("/api/role-permissions/migration-banner/ack", {}, { successMessage: "Acknowledged" });
    bannerPending.value = false;
  } catch {
    // useApi toasted; leave banner visible so user can retry.
  } finally {
    ackingBanner.value = false;
  }
}

function openCreateRole() {
  editingRole.value = null;
  roleForm.name = "";
  roleForm.description = "";
  roleForm.permissions = [];
  roleError.value = "";
  showDialog.value = true;
}

function openEditRole(role) {
  editingRole.value = role;
  roleForm.name = role.name;
  roleForm.description = role.description || "";
  roleForm.permissions = Array.isArray(role.permissions) ? [...role.permissions] : [];
  roleError.value = "";
  showDialog.value = true;
}

async function saveRole() {
  if (!roleForm.name.trim()) {
    roleError.value = "Name is required.";
    return;
  }
  savingRole.value = true;
  roleError.value = "";
  try {
    const payload = {
      name: roleForm.name.trim(),
      description: roleForm.description.trim() || null,
      permissions: roleForm.permissions || [],
    };
    if (editingRole.value) {
      // Seeded roles keep their canonical name — backend ignores name changes
      // on is_system rows; we strip client-side to keep audit diffs clean.
      if (editingRole.value.is_system) {
        delete payload.name;
      }
      await api.patch(`/api/role-permissions/roles/${editingRole.value.id}`, payload, {
        successMessage: "Role updated",
      });
    } else {
      await api.post("/api/role-permissions/roles", payload, { successMessage: "Role created" });
    }
    await loadRoles();
    showDialog.value = false;
  } catch (err) {
    // useApi already toasted. Keep the dialog open so the user can correct
    // and retry instead of crashing the whole view to ErrorBoundary.
    roleError.value = err?.message || "Could not save role.";
  } finally {
    savingRole.value = false;
  }
}

async function deleteRole(role) {
  if (role.is_system) return;
  if (!(await confirmAsync({ header: 'Confirm', message: `Delete role ${role.name}?` }))) return;
  try {
    await api.del(`/api/role-permissions/roles/${role.id}`, { successMessage: "Role deleted" });
    await loadRoles();
  } catch {
    // useApi toasted; leave list as-is.
  }
}

async function resetRole(role) {
  if (!role.is_system) return;
  if (!(await confirmAsync({ header: 'Confirm', message: `Reset ${role.name} to platform defaults? Custom permissions on this role will be lost.` }))) return;
  resetting[role.id] = true;
  try {
    await api.post(`/api/role-permissions/roles/${role.id}/reset`, {}, { successMessage: `Reset ${role.name}` });
    await loadRoles();
  } catch {
    // useApi toasted.
  } finally {
    resetting[role.id] = false;
  }
}

async function assignRoleToUser(user) {
  const roleId = pendingRoleByUser[user.id];
  if (!roleId) return;
  assigning[user.id] = true;
  try {
    await api.post(
      `/api/role-permissions/users/${encodeURIComponent(user.id)}/roles`,
      { role_id: roleId },
      { successMessage: `Assigned ${roleName(roleId)} to ${user.name || user.email || user.id}` }
    );
    pendingRoleByUser[user.id] = null;
    await loadUserRoles(user.id);
  } catch {
    // useApi toasted.
  } finally {
    assigning[user.id] = false;
  }
}

watch(
  () => showDialog.value,
  (open) => {
    if (open && !catalog.value.length) loadCatalog();
  }
);

onMounted(async () => {
  await Promise.all([loadRoles(), loadCatalog(), loadUsers(), loadBanner()]);
});
</script>

<style scoped>
.role-permissions-view :deep(.p-datatable-table) td {
  vertical-align: top;
}

.banner-row {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  width: 100%;
}

.user-search {
  width: 280px;
}

.role-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
}

.role-chip {
  background: var(--p-content-hover-background);
  color: var(--text-color, #333);
  border-radius: 999px;
  padding: 0.1rem 0.55rem;
  font-size: 0.85em;
}

.assign-row {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}

.assign-select {
  flex: 1;
}

.permission-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 1rem;
  border: 1px solid var(--p-content-border-color);
  border-radius: 6px;
  padding: 0.75rem;
  max-height: 420px;
  overflow-y: auto;
}

.permission-category {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.category-header {
  display: flex;
  justify-content: space-between;
  border-bottom: 1px solid var(--p-content-border-color);
  padding-bottom: 0.25rem;
}

.permission-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.permission-label {
  display: flex;
  flex-direction: column;
  font-size: 0.92em;
  cursor: pointer;
}

.permission-label code {
  font-size: 0.78em;
}

.grid-toolbar {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.4rem;
}

.grid-toolbar-spacer {
  flex: 1;
}

.muted {
  color: var(--p-text-muted-color);
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  padding: 2rem 0;
}

.inline-error {
  color: var(--p-red-500);
  margin: 0.5rem 0;
}
</style>
