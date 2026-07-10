<template>
    <section class="users-view view-card">
      <!-- Summary Stats Bar -->
      <div class="stats-bar" data-testid="users-stats-bar">
        <div class="stat-card" data-testid="users-stat-owner">
          <span class="stat-count">{{ roleCounts.owner }}</span>
          <span class="stat-label">Owner</span>
        </div>
        <div class="stat-card" data-testid="users-stat-admin">
          <span class="stat-count">{{ roleCounts.admin }}</span>
          <span class="stat-label">Admin</span>
        </div>
        <div class="stat-card" data-testid="users-stat-dispatcher">
          <span class="stat-count">{{ roleCounts.dispatcher }}</span>
          <span class="stat-label">Dispatcher</span>
        </div>
        <div class="stat-card" data-testid="users-stat-technician">
          <span class="stat-count">{{ roleCounts.technician }}</span>
          <span class="stat-label">Technician</span>
        </div>
        <div class="stat-card" data-testid="users-stat-viewer">
          <span class="stat-count">{{ roleCounts.viewer }}</span>
          <span class="stat-label">Viewer</span>
        </div>
        <div class="stat-card" data-testid="users-stat-active">
          <span class="stat-count">{{ activeCount }}</span>
          <span class="stat-label">Active</span>
        </div>
        <div class="stat-card" data-testid="users-stat-total">
          <span class="stat-count">{{ users.length }}</span>
          <span class="stat-label">Total</span>
        </div>
      </div>

      <!-- Toolbar: filter buttons + add user -->
      <Toolbar>
        <template #start>
          <div class="filter-buttons" data-testid="users-filter-buttons">
            <Button
              v-for="f in filterOptions"
              :key="f.value"
              :label="f.label"
              :severity="activeFilter === f.value ? undefined : 'secondary'"
              :outlined="activeFilter !== f.value"
              size="small"
              :data-testid="`users-filter-${f.value}`"
              @click="activeFilter = f.value"
            />
          </div>
        </template>
        <template #end>
          <Button
            label="+ Add User"
            icon="pi pi-plus"
            data-testid="users-add-btn"
            @click="openCreateDialog"
          />
        </template>
      </Toolbar>

      <!-- Loading -->
      <div v-if="isLoading" class="spinner-wrap" data-testid="users-loading">
        <ProgressSpinner />
      </div>

      <!-- User Card Grid -->
      <div v-else class="user-grid" data-testid="users-grid">
        <div
          v-for="user in filteredUsers"
          :key="user.id"
          class="user-card"
          :data-testid="`users-card-${user.id}`"
        >
          <div class="card-header">
            <div
              class="avatar"
              :style="{ backgroundColor: avatarColor(user) }"
              :data-testid="`users-avatar-${user.id}`"
            >
              {{ initials(user) }}
            </div>
            <div class="user-info">
              <span class="user-name">{{ user.name || user.username || 'Unnamed' }}</span>
              <span class="user-email">{{ user.email || user.username || '' }}</span>
            </div>
            <button
              v-if="isLocked(user)"
              type="button"
              class="locked-badge"
              :data-testid="`users-locked-badge-${user.id}`"
              :aria-label="`View lockout reason for ${user.name || user.email}`"
              @click="openLockoutInfo(user)"
            >
              <i class="pi pi-lock" aria-hidden="true" />
              Locked
            </button>
          </div>

          <div class="card-controls">
            <div class="control-row">
              <label :for="`role-${user.id}`">Role</label>
              <Select
                :id="`role-${user.id}`"
                :modelValue="fromBackendRole((user.role || '').toLowerCase())"
                :options="roleOptions"
                optionLabel="label"
                optionValue="value"
                :data-testid="`users-role-select-${user.id}`"
                class="w-full"
                @update:modelValue="(val) => changeRole(user, val)"
              />
            </div>

            <div class="toggle-row">
              <label :for="`schedulable-${user.id}`">Schedulable</label>
              <ToggleSwitch
                :id="`schedulable-${user.id}`"
                :modelValue="Boolean(user.schedulable)"
                :data-testid="`users-schedulable-toggle-${user.id}`"
                @update:modelValue="() => toggleSchedulable(user)"
              />
            </div>
          </div>

          <div class="card-actions">
            <Button
              label="Reset Password"
              text
              size="small"
              :data-testid="`users-reset-pw-${user.id}`"
              @click="openResetPasswordDialog(user)"
            />
            <Button
              label="Send Reset Link"
              text
              size="small"
              :data-testid="`users-send-reset-${user.id}`"
              @click="sendResetLink(user)"
            />
            <Button
              label="Edit Profile"
              text
              size="small"
              :data-testid="`users-edit-${user.id}`"
              @click="openEditDialog(user)"
            />
            <Button
              v-if="canLockOut(user)"
              label="Lock Out"
              icon="pi pi-lock"
              severity="danger"
              size="small"
              :data-testid="`users-lockout-${user.id}`"
              @click="openLockoutDialog(user)"
            />
            <Button
              v-if="canUnlock(user)"
              label="Unlock"
              icon="pi pi-lock-open"
              severity="success"
              outlined
              size="small"
              :data-testid="`users-unlock-${user.id}`"
              @click="openUnlockDialog(user)"
            />
            <Button
              label="Delete"
              text
              severity="danger"
              size="small"
              :data-testid="`users-delete-${user.id}`"
              @click="promptDelete(user)"
            />
          </div>
        </div>

        <div v-if="filteredUsers.length === 0" class="empty-message">
          No users match this filter.
        </div>
      </div>

      <!-- Create User Dialog -->
      <Dialog
        v-model:visible="showCreateDialog"
        header="Add User"
        :style="{ width: '500px' }"
        modal
        data-testid="users-create-dialog"
      >
        <form class="dialog-form" @submit.prevent="submitCreate">
          <div class="form-field">
            <label for="create-name">Full Name *</label>
            <InputText
              id="create-name"
              v-model="createForm.name"
              data-testid="users-create-name"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label for="create-email">Email *</label>
            <InputText
              id="create-email"
              v-model="createForm.email"
              type="email"
              data-testid="users-create-email"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label for="create-password">Password *</label>
            <Password
              id="create-password"
              v-model="createForm.password"
              toggleMask
              data-testid="users-create-password"
              class="w-full"
              :pt="{ root: { class: 'w-full' }, pcInput: { root: { class: 'w-full' } } }"
            />
            <small class="form-hint">Min 8 characters with uppercase, lowercase, and a number</small>
          </div>
          <div class="form-field">
            <label for="create-role">Role</label>
            <Select
              id="create-role"
              v-model="createForm.role"
              :options="roleOptions"
              optionLabel="label"
              optionValue="value"
              data-testid="users-create-role"
              class="w-full"
            />
          </div>
          <div class="toggle-row">
            <label for="create-invite">Send invite</label>
            <ToggleSwitch
              id="create-invite"
              v-model="createForm.send_invite"
              data-testid="users-create-invite-toggle"
            />
          </div>
          <div v-if="createError" class="inline-error" data-testid="users-create-error">
            {{ createError }}
          </div>
          <div class="form-actions">
            <Button type="button" label="Cancel" text @click="showCreateDialog = false" />
            <Button
              type="submit"
              label="Create User"
              :loading="isCreating"
              data-testid="users-create-submit"
            />
          </div>
        </form>
      </Dialog>

      <!-- Edit Profile Dialog -->
      <Dialog
        v-model:visible="showEditDialog"
        header="Edit Profile"
        :style="{ width: '500px' }"
        modal
        data-testid="users-edit-dialog"
      >
        <form class="dialog-form" @submit.prevent="submitEdit">
          <div class="form-field">
            <label for="edit-name">Name</label>
            <InputText
              id="edit-name"
              v-model="editForm.name"
              data-testid="users-edit-name"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label for="edit-email">Email</label>
            <InputText
              id="edit-email"
              v-model="editForm.email"
              type="email"
              data-testid="users-edit-email"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label for="edit-phone">Phone</label>
            <PhoneInput
              id="edit-phone"
              v-model="editForm.phone"
              data-testid="users-edit-phone"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label for="edit-address">Route Start Address</label>
            <InputText
              id="edit-address"
              v-model="editForm.route_start_address"
              data-testid="users-edit-address"
              class="w-full"
            />
          </div>
          <div class="form-field">
            <label for="edit-certs">Certifications</label>
            <Textarea
              id="edit-certs"
              v-model="editForm.certifications"
              rows="3"
              data-testid="users-edit-certifications"
              class="w-full"
            />
          </div>
          <fieldset class="form-field" style="border:1px solid var(--surface-border); padding:0.75rem 1rem; border-radius:6px;">
            <legend style="padding:0 0.5rem;">Shift override</legend>
            <p class="form-hint" style="margin-top:0;">
              Blank = inherit the shop default. Set any field to override only that one.
            </p>
            <div style="display:flex; gap:0.75rem; flex-wrap:wrap;">
              <div style="flex:1; min-width:140px;">
                <label for="edit-shift-start" style="display:block; font-size:0.85rem;">Start</label>
                <InputText
                  id="edit-shift-start"
                  v-model="editForm.shift_start"
                  type="time"
                  class="w-full"
                  data-testid="users-edit-shift-start"
                />
              </div>
              <div style="flex:1; min-width:140px;">
                <label for="edit-shift-end" style="display:block; font-size:0.85rem;">End</label>
                <InputText
                  id="edit-shift-end"
                  v-model="editForm.shift_end"
                  type="time"
                  class="w-full"
                  data-testid="users-edit-shift-end"
                />
              </div>
            </div>
            <div style="margin-top:0.75rem;">
              <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:0.35rem;">
                <label style="font-size:0.85rem;">Working days</label>
                <Button
                  v-if="editForm.workdays != null"
                  label="Inherit"
                  text
                  size="small"
                  data-testid="users-edit-workdays-clear"
                  @click="clearWorkdayOverride"
                />
              </div>
              <div style="display:flex; gap:0.35rem; flex-wrap:wrap;">
                <button
                  v-for="day in USER_WORKDAY_BITS"
                  :key="day.bit"
                  type="button"
                  class="p-button p-component"
                  :class="isUserWorkday(day.bit) ? 'p-button-primary' : 'p-button-secondary p-button-outlined'"
                  style="padding:0.3rem 0.6rem; min-width:48px; font-size:0.8rem;"
                  :data-testid="`users-edit-day-${day.label.toLowerCase()}`"
                  @click="toggleUserWorkdayBit(day.bit)"
                >{{ day.label }}</button>
              </div>
            </div>
          </fieldset>
          <div v-if="editError" class="inline-error" data-testid="users-edit-error">
            {{ editError }}
          </div>
          <div class="form-actions">
            <Button type="button" label="Cancel" text @click="showEditDialog = false" />
            <Button
              type="submit"
              label="Save Changes"
              :loading="isSavingEdit"
              data-testid="users-edit-submit"
            />
          </div>
        </form>
      </Dialog>

      <!-- Reset Password Dialog -->
      <Dialog
        v-model:visible="showResetDialog"
        header="Reset Password"
        :style="{ width: '420px' }"
        modal
        data-testid="users-reset-dialog"
      >
        <form class="dialog-form" @submit.prevent="submitResetPassword">
          <div class="form-field">
            <label for="reset-pw">New Password</label>
            <Password
              id="reset-pw"
              v-model="resetForm.password"
              toggleMask
              data-testid="users-reset-password-input"
              class="w-full"
              :pt="{ root: { class: 'w-full' }, pcInput: { root: { class: 'w-full' } } }"
            />
          </div>
          <div class="form-field">
            <label for="reset-pw-confirm">Confirm Password</label>
            <Password
              id="reset-pw-confirm"
              v-model="resetForm.confirm"
              toggleMask
              :feedback="false"
              data-testid="users-reset-password-confirm"
              class="w-full"
              :pt="{ root: { class: 'w-full' }, pcInput: { root: { class: 'w-full' } } }"
            />
          </div>
          <div v-if="resetError" class="inline-error" data-testid="users-reset-error">
            {{ resetError }}
          </div>
          <div class="form-actions">
            <Button type="button" label="Cancel" text @click="showResetDialog = false" />
            <Button
              type="submit"
              label="Reset Password"
              :loading="isResetting"
              data-testid="users-reset-submit"
            />
          </div>
        </form>
      </Dialog>

      <!-- Lock Out Confirmation Dialog -->
      <Dialog
        v-model:visible="showLockoutDialog"
        :style="{ width: '480px' }"
        modal
        data-testid="users-lockout-dialog"
        :pt="{ header: { class: 'lockout-dialog-header' } }"
      >
        <template #header>
          <div class="lockout-header">
            <i class="pi pi-lock" aria-hidden="true" />
            <span>Lock out {{ lockoutTarget?.name || lockoutTarget?.email }}?</span>
          </div>
        </template>
        <p class="lockout-warning">
          This will immediately revoke <strong>{{ lockoutTarget?.name || 'this user' }}'s</strong>
          sessions and prevent them from signing in. They will not be deleted — you
          can unlock them later. This action is audit-logged.
        </p>
        <div class="form-field">
          <label for="lockout-reason">Reason *</label>
          <Select
            id="lockout-reason"
            v-model="lockoutForm.reason"
            :options="lockoutReasons"
            optionLabel="label"
            optionValue="value"
            placeholder="Select a reason"
            data-testid="users-lockout-reason"
            class="w-full"
          />
        </div>
        <div class="form-field">
          <label for="lockout-notes">Notes (optional)</label>
          <Textarea
            id="lockout-notes"
            v-model="lockoutForm.notes"
            rows="3"
            placeholder="Additional context for the audit trail"
            data-testid="users-lockout-notes"
            class="w-full"
            maxlength="2000"
          />
        </div>
        <div class="form-field">
          <label for="lockout-confirm">Type <strong>LOCK</strong> to confirm</label>
          <InputText
            id="lockout-confirm"
            v-model="lockoutForm.confirm"
            placeholder="LOCK"
            autocomplete="off"
            data-testid="users-lockout-confirm-input"
            class="w-full"
          />
        </div>
        <div v-if="lockoutError" class="inline-error" data-testid="users-lockout-error">
          {{ lockoutError }}
        </div>
        <div class="form-actions">
          <Button label="Cancel" text @click="showLockoutDialog = false" />
          <Button
            label="Lock Out"
            icon="pi pi-lock"
            severity="danger"
            :disabled="!canSubmitLockout"
            :loading="isLockingOut"
            data-testid="users-lockout-submit"
            @click="submitLockout"
          />
        </div>
      </Dialog>

      <!-- Unlock Confirmation Dialog -->
      <Dialog
        v-model:visible="showUnlockDialog"
        header="Unlock user"
        :style="{ width: '420px' }"
        modal
        data-testid="users-unlock-dialog"
      >
        <p>
          Unlock <strong>{{ unlockTarget?.name || unlockTarget?.email }}</strong>?
          They will be able to sign in again immediately.
        </p>
        <div class="form-actions">
          <Button label="Cancel" text @click="showUnlockDialog = false" />
          <Button
            label="Unlock"
            icon="pi pi-lock-open"
            severity="success"
            :loading="isUnlocking"
            data-testid="users-unlock-submit"
            @click="submitUnlock"
          />
        </div>
      </Dialog>

      <!-- Lockout Info popover dialog (click-to-reveal reason) -->
      <Dialog
        v-model:visible="showLockoutInfoDialog"
        header="Lockout details"
        :style="{ width: '420px' }"
        modal
        data-testid="users-lockout-info-dialog"
      >
        <div v-if="lockoutInfoLoading" class="spinner-wrap">
          <ProgressSpinner />
        </div>
        <div v-else-if="lockoutInfo" class="lockout-info">
          <div class="lockout-info-row">
            <span class="lockout-info-label">Reason</span>
            <span class="lockout-info-value" data-testid="users-lockout-info-reason">
              {{ formatReason(lockoutInfo.reason) }}
            </span>
          </div>
          <div v-if="lockoutInfo.notes" class="lockout-info-row">
            <span class="lockout-info-label">Notes</span>
            <span class="lockout-info-value" data-testid="users-lockout-info-notes">
              {{ lockoutInfo.notes }}
            </span>
          </div>
          <div v-if="lockoutInfo.locked_at" class="lockout-info-row">
            <span class="lockout-info-label">When</span>
            <span class="lockout-info-value">{{ formatDateTime(lockoutInfo.locked_at) }}</span>
          </div>
        </div>
        <div v-else class="lockout-info-empty" data-testid="users-lockout-info-empty">
          No lockout history found for this user.
        </div>
        <div class="form-actions">
          <Button label="Close" @click="showLockoutInfoDialog = false" />
        </div>
      </Dialog>

      <!-- Delete Confirmation Dialog -->
      <Dialog
        v-model:visible="showDeleteDialog"
        header="Confirm Delete"
        :style="{ width: '400px' }"
        modal
        data-testid="users-delete-dialog"
      >
        <p>Are you sure you want to delete <strong>{{ deleteTarget?.name || deleteTarget?.username }}</strong>?</p>
        <p style="color: var(--p-text-muted-color)">This action cannot be undone.</p>
        <div class="form-actions">
          <Button label="Cancel" text @click="showDeleteDialog = false" />
          <Button
            label="Delete"
            severity="danger"
            :loading="isDeleting"
            data-testid="users-confirm-delete-btn"
            @click="confirmDelete"
          />
        </div>
      </Dialog>

      <Toast data-testid="users-toast" />
    </section>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue';
import { useToast } from 'primevue/usetoast';
import { useApiWithToast } from '../composables/useApiWithToast';
import { formatDateTime } from '../composables/useFormatters';
import { useAuthStore } from '../stores/auth';
import Button from 'primevue/button';
import Dialog from 'primevue/dialog';
import InputText from 'primevue/inputtext';
import ProgressSpinner from 'primevue/progressspinner';
import Select from 'primevue/select';
import Textarea from 'primevue/textarea';
import ToggleSwitch from 'primevue/toggleswitch';
import Toast from 'primevue/toast';
import Toolbar from 'primevue/toolbar';
import Password from 'primevue/password';
import PhoneInput from '../components/PhoneInput.vue';

const api = useApiWithToast();
const toast = useToast();
const auth = useAuthStore();

const AVATAR_COLORS = [
  '#1976D2', '#388E3C', '#D32F2F', '#7B1FA2', '#00796B',
  '#F57C00', '#1A237E', '#00695C', '#6A1B9A', '#C2185B',
];

// 2026-04-29 nav-cleanup: align with the canonical 5-role taxonomy from
// /role-permissions ({admin, dispatcher, owner, technician, viewer}). The
// previous UsersView used {Admin, Dispatch, Tech, Sales, Owner} — which both
// renamed dispatcher/technician AND introduced a "sales" role that doesn't
// exist in the system roles table. KPI counts beneath the chips were always
// reading 0/0 for Sales because no row could ever match.
const roleOptions = [
  { label: 'Owner', value: 'owner' },
  { label: 'Admin', value: 'admin' },
  { label: 'Dispatcher', value: 'dispatcher' },
  { label: 'Technician', value: 'technician' },
  { label: 'Viewer', value: 'viewer' },
];

const filterOptions = [
  { label: 'All', value: 'all' },
  { label: 'Owner', value: 'owner' },
  { label: 'Admin', value: 'admin' },
  { label: 'Dispatcher', value: 'dispatcher' },
  { label: 'Technician', value: 'technician' },
  { label: 'Viewer', value: 'viewer' },
  { label: 'Inactive', value: 'inactive' },
];

const users = ref([]);
const isLoading = ref(false);
const activeFilter = ref('all');

// Create dialog state
const showCreateDialog = ref(false);
const isCreating = ref(false);
const createError = ref('');
const createForm = ref(emptyCreateForm());

// Edit dialog state
const showEditDialog = ref(false);
const isSavingEdit = ref(false);
const editError = ref('');
const editForm = ref(emptyEditForm());
const editTarget = ref(null);

// Reset password dialog state
const showResetDialog = ref(false);
const isResetting = ref(false);
const resetError = ref('');
const resetForm = ref({ password: '', confirm: '' });
const resetTarget = ref(null);

// Delete dialog state
const showDeleteDialog = ref(false);
const isDeleting = ref(false);
const deleteTarget = ref(null);

// Lockout dialog state
const lockoutReasons = [
  { label: 'Terminated', value: 'terminated' },
  { label: 'Security incident', value: 'security_incident' },
  { label: 'Policy violation', value: 'policy_violation' },
  { label: 'Suspicious activity', value: 'suspicious_activity' },
  { label: 'Other', value: 'other' },
];
const showLockoutDialog = ref(false);
const isLockingOut = ref(false);
const lockoutError = ref('');
const lockoutTarget = ref(null);
const lockoutForm = ref({ reason: null, notes: '', confirm: '' });

// Unlock dialog state
const showUnlockDialog = ref(false);
const isUnlocking = ref(false);
const unlockTarget = ref(null);

// Lockout-info popover state
const showLockoutInfoDialog = ref(false);
const lockoutInfoLoading = ref(false);
const lockoutInfo = ref(null);

const currentUserId = computed(() => auth.user?.user_id || auth.user?.sub || auth.user?.id || null);

// Only admin + owner actors may lock/unlock. require_permission gate
// alone is insufficient because tenants can grant users.write to
// non-platform-locked roles — see backend `_require_lockout_actor`.
const ACTOR_LOCK_ROLES = new Set(['admin', 'owner']);
const currentUserRole = computed(() => (auth.user?.role || auth.user?.user_role || '').toLowerCase());
const canActOnLockouts = computed(() => ACTOR_LOCK_ROLES.has(currentUserRole.value));

function isLocked(user) {
  return !(user.active ?? user.is_active ?? true);
}

function isOwner(user) {
  return (user.role || '').toLowerCase() === 'owner';
}

function isSelf(user) {
  const me = currentUserId.value;
  return me != null && String(user.id) === String(me);
}

function canLockOut(user) {
  return canActOnLockouts.value && !isLocked(user) && !isOwner(user) && !isSelf(user);
}

function canUnlock(user) {
  return canActOnLockouts.value && isLocked(user);
}

const canSubmitLockout = computed(() =>
  Boolean(lockoutForm.value.reason) && lockoutForm.value.confirm.trim() === 'LOCK'
);

function formatReason(reasonKey) {
  const found = lockoutReasons.find((r) => r.value === reasonKey);
  return found ? found.label : (reasonKey || 'Unknown');
}

function emptyCreateForm() {
  return { name: '', email: '', password: '', role: 'technician', send_invite: false };
}

function emptyEditForm() {
  // shift_start/end as strings ("" = inherit tenant default); workdays as
  // bitmask integer or null (null = inherit). Sprint dispatch-capacity.
  return {
    name: '', email: '', phone: '', route_start_address: '', certifications: '',
    shift_start: '', shift_end: '', workdays: null,
  };
}

// Sprint dispatch-capacity (2026-05-20) — per-user shift override editor.
const USER_WORKDAY_BITS = [
  { label: 'Mon', bit: 1 },
  { label: 'Tue', bit: 2 },
  { label: 'Wed', bit: 4 },
  { label: 'Thu', bit: 8 },
  { label: 'Fri', bit: 16 },
  { label: 'Sat', bit: 32 },
  { label: 'Sun', bit: 64 },
];
function isUserWorkday(bit) {
  return editForm.value.workdays != null && ((editForm.value.workdays & bit) === bit);
}
function toggleUserWorkdayBit(bit) {
  // Null (inherit) → start from 0 and set this bit; allows building a
  // custom override one click at a time. Toggling the only bit off goes
  // back to null (inherit) — there's no such thing as "works zero days".
  if (editForm.value.workdays == null) {
    editForm.value.workdays = bit;
    return;
  }
  const next = editForm.value.workdays ^ bit;
  editForm.value.workdays = next === 0 ? null : next;
}
function clearWorkdayOverride() {
  editForm.value.workdays = null;
}

function initials(user) {
  const name = user.name || user.username || user.email || '?';
  const parts = name.trim().split(/\s+/);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
  }
  return name.slice(0, 2).toUpperCase();
}

function avatarColor(user) {
  const str = user.id || user.email || user.name || '';
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash);
  }
  return AVATAR_COLORS[Math.abs(hash) % AVATAR_COLORS.length];
}

// Aliases for legacy short forms ("dispatch" → "dispatcher", "tech" → "technician").
// Keeps the count correct if any user row still has the old short value pending
// a backend taxonomy migration.
const ROLE_ALIASES = { dispatch: 'dispatcher', tech: 'technician' };

// Inverse: UI long form → backend short form. Backend regex still accepts only
// `admin|dispatch|tech|sales|owner`; sending 'technician'/'dispatcher' returns 422.
// Until the taxonomy is unified server-side, translate at the UI boundary.
const ROLE_TO_BACKEND = { technician: 'tech', dispatcher: 'dispatch' };
const toBackendRole = (r) => ROLE_TO_BACKEND[r] || r;
const fromBackendRole = (r) => ROLE_ALIASES[r] || r;

const roleCounts = computed(() => {
  const counts = { owner: 0, admin: 0, dispatcher: 0, technician: 0, viewer: 0 };
  for (const u of users.value) {
    let r = (u.role || '').toLowerCase();
    r = ROLE_ALIASES[r] || r;
    if (r in counts) counts[r]++;
  }
  return counts;
});

const activeCount = computed(() => users.value.filter((u) => (u.active ?? u.is_active ?? true)).length);

const filteredUsers = computed(() => {
  if (activeFilter.value === 'all') return users.value;
  if (activeFilter.value === 'inactive') return users.value.filter((u) => !(u.active ?? u.is_active ?? true));
  return users.value.filter((u) => fromBackendRole((u.role || '').toLowerCase()) === activeFilter.value);
});

async function fetchUsers() {
  isLoading.value = true;
  try {
    const result = await api.get('/api/users');
    users.value = Array.isArray(result) ? result : result?.items || result?.data || [];
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Load Error', detail: error?.message || 'Failed to load users.', life: 5000 });
  } finally {
    isLoading.value = false;
  }
}

async function changeRole(user, newRole) {
  try {
    const backendRole = toBackendRole(newRole);
    await api.post(`/api/users/${encodeURIComponent(user.id)}/role`, { role: backendRole }, { successMessage: 'Role updated' });
    user.role = backendRole;
  } catch {
    // useApiWithToast handles error display
  }
}

function openLockoutDialog(user) {
  lockoutTarget.value = user;
  lockoutForm.value = { reason: null, notes: '', confirm: '' };
  lockoutError.value = '';
  showLockoutDialog.value = true;
}

async function submitLockout() {
  if (!lockoutTarget.value?.id || !canSubmitLockout.value) return;
  lockoutError.value = '';
  isLockingOut.value = true;
  try {
    const result = await api.post(
      `/api/users/${encodeURIComponent(lockoutTarget.value.id)}/lockout`,
      {
        reason: lockoutForm.value.reason,
        notes: lockoutForm.value.notes.trim() || null,
      },
      { successMessage: 'User locked out' },
    );
    const next = result?.active ?? false;
    lockoutTarget.value.active = next;
    lockoutTarget.value.is_active = next;
    showLockoutDialog.value = false;
  } catch (error) {
    lockoutError.value = error?.message || 'Failed to lock out user.';
  } finally {
    isLockingOut.value = false;
  }
}

function openUnlockDialog(user) {
  unlockTarget.value = user;
  showUnlockDialog.value = true;
}

async function submitUnlock() {
  if (!unlockTarget.value?.id) return;
  isUnlocking.value = true;
  try {
    const result = await api.post(
      `/api/users/${encodeURIComponent(unlockTarget.value.id)}/unlock`,
      {},
      { successMessage: 'User unlocked' },
    );
    const next = result?.active ?? true;
    unlockTarget.value.active = next;
    unlockTarget.value.is_active = next;
    showUnlockDialog.value = false;
  } catch {
    // useApiWithToast handles error display
  } finally {
    isUnlocking.value = false;
  }
}

async function openLockoutInfo(user) {
  lockoutInfo.value = null;
  lockoutInfoLoading.value = true;
  showLockoutInfoDialog.value = true;
  try {
    const result = await api.get(`/api/users/${encodeURIComponent(user.id)}/lockout-info`);
    lockoutInfo.value = result || null;
  } catch {
    lockoutInfo.value = null;
  } finally {
    lockoutInfoLoading.value = false;
  }
}

async function toggleSchedulable(user) {
  try {
    const result = await api.post(`/api/users/${encodeURIComponent(user.id)}/toggle-schedulable`, {}, { successMessage: 'Schedulable updated' });
    user.schedulable = result?.schedulable ?? !user.schedulable;
  } catch {
    // useApiWithToast handles error display
  }
}

async function sendResetLink(user) {
  try {
    await api.post(`/api/users/${encodeURIComponent(user.id)}/send-reset-link`, {}, { successMessage: 'Reset link sent' });
  } catch {
    // useApiWithToast handles error display
  }
}

// Create dialog
function openCreateDialog() {
  createForm.value = emptyCreateForm();
  createError.value = '';
  showCreateDialog.value = true;
}

async function submitCreate() {
  createError.value = '';
  if (!createForm.value.name.trim()) { createError.value = 'Name is required.'; return; }
  if (!createForm.value.email.trim()) { createError.value = 'Email is required.'; return; }
  if (!createForm.value.send_invite && !createForm.value.password.trim()) {
    createError.value = 'Password is required when not sending an invite.';
    return;
  }

  isCreating.value = true;
  try {
    const endpoint = createForm.value.send_invite ? '/api/users/invite' : '/api/users';
    const payload = {
      name: createForm.value.name.trim(),
      email: createForm.value.email.trim(),
      role: toBackendRole(createForm.value.role),
    };
    if (!createForm.value.send_invite) {
      payload.password = createForm.value.password;
    }
    await api.post(endpoint, payload, { successMessage: 'User created' });
    showCreateDialog.value = false;
    await fetchUsers();
  } catch (error) {
    createError.value = error?.message || 'Failed to create user.';
  } finally {
    isCreating.value = false;
  }
}

// Edit dialog
function openEditDialog(user) {
  editTarget.value = user;
  editForm.value = {
    name: user.name || '',
    email: user.email || '',
    phone: user.phone || '',
    route_start_address: user.route_start_address || '',
    certifications: user.certifications || '',
    shift_start: user.shift_start || '',
    shift_end: user.shift_end || '',
    workdays: Number.isInteger(user.workdays) ? user.workdays : null,
  };
  editError.value = '';
  showEditDialog.value = true;
}

async function submitEdit() {
  editError.value = '';
  if (!editTarget.value) return;

  isSavingEdit.value = true;
  try {
    const payload = {
      name: editForm.value.name.trim(),
      email: editForm.value.email.trim(),
      phone: editForm.value.phone.trim() || null,
      route_start_address: editForm.value.route_start_address.trim() || null,
      certifications: editForm.value.certifications.trim() || null,
      // Sprint dispatch-capacity — empty string = "inherit tenant default"
      // (send explicit null to clear the override). workdays null = inherit.
      shift_start: editForm.value.shift_start ? editForm.value.shift_start : null,
      shift_end: editForm.value.shift_end ? editForm.value.shift_end : null,
      workdays: editForm.value.workdays,
    };
    if (payload.shift_start && payload.shift_end && payload.shift_end <= payload.shift_start) {
      throw new Error('Shift end must be after shift start.');
    }
    await api.patch(`/api/users/${encodeURIComponent(editTarget.value.id)}`, payload, { successMessage: 'Profile updated' });
    showEditDialog.value = false;
    await fetchUsers();
  } catch (error) {
    editError.value = error?.message || 'Failed to save profile.';
  } finally {
    isSavingEdit.value = false;
  }
}

// Reset password dialog
function openResetPasswordDialog(user) {
  resetTarget.value = user;
  resetForm.value = { password: '', confirm: '' };
  resetError.value = '';
  showResetDialog.value = true;
}

async function submitResetPassword() {
  resetError.value = '';
  if (!resetForm.value.password) { resetError.value = 'Password is required.'; return; }
  if (resetForm.value.password !== resetForm.value.confirm) {
    resetError.value = 'Passwords do not match.';
    return;
  }
  if (!resetTarget.value) return;

  isResetting.value = true;
  try {
    await api.post(
      `/api/users/${encodeURIComponent(resetTarget.value.id)}/reset-password`,
      { new_password: resetForm.value.password },
      { successMessage: 'Password reset successfully' }
    );
    showResetDialog.value = false;
  } catch (error) {
    resetError.value = error?.message || 'Failed to reset password.';
  } finally {
    isResetting.value = false;
  }
}

// Delete dialog
function promptDelete(user) {
  deleteTarget.value = user;
  showDeleteDialog.value = true;
}

async function confirmDelete() {
  if (!deleteTarget.value?.id) return;
  isDeleting.value = true;
  try {
    await api.del(`/api/users/${encodeURIComponent(deleteTarget.value.id)}`, { successMessage: 'User deleted' });
    showDeleteDialog.value = false;
    deleteTarget.value = null;
    await fetchUsers();
  } catch (error) {
    toast.add({ severity: 'error', summary: 'Error', detail: error?.message || 'Failed to delete user.', life: 5000 });
  } finally {
    isDeleting.value = false;
  }
}

onMounted(() => {
  fetchUsers();
});
</script>

<style scoped>
.users-view {
  max-width: 1400px;
}

.stats-bar {
  display: flex;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin-bottom: 1rem;
}

.stat-card {
  background: var(--surface-card, #1e1e2e);
  border: 1px solid var(--surface-border, #333);
  border-radius: 8px;
  padding: 0.75rem 1.25rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 90px;
}

.stat-count {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--p-primary-color);
}

.stat-label {
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
  margin-top: 0.15rem;
}

.filter-buttons {
  display: flex;
  gap: 0.35rem;
  flex-wrap: wrap;
}

.spinner-wrap {
  display: flex;
  justify-content: center;
  margin: 2rem 0;
}

.user-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 1rem;
  margin-top: 1rem;
}

.user-card {
  background: var(--surface-card, #1e1e2e);
  border: 1px solid var(--surface-border, #333);
  border-radius: 10px;
  padding: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.card-header {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.avatar {
  width: 44px;
  height: 44px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-weight: 700;
  font-size: 0.95rem;
  flex-shrink: 0;
}

.user-info {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.user-name {
  font-weight: 600;
  font-size: 1rem;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.user-email {
  font-size: 0.82rem;
  color: var(--p-text-muted-color);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.card-controls {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.control-row {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.control-row label,
.toggle-row label {
  font-size: 0.8rem;
  color: var(--p-text-muted-color);
}

.toggle-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.card-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
  border-top: 1px solid var(--surface-border, #333);
  padding-top: 0.5rem;
}

.dialog-form {
  display: grid;
  gap: 0.75rem;
}

.form-field {
  display: grid;
  gap: 0.25rem;
}

.form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 0.75rem;
}

.inline-error {
  color: #b42318;
  margin: 0.5rem 0;
  font-size: 0.9rem;
}

.empty-message {
  text-align: center;
  padding: 2rem;
  color: var(--p-text-muted-color);
  grid-column: 1 / -1;
}

.w-full {
  width: 100%;
}

.form-hint {
  color: var(--text-muted, #94a3b8);
  font-size: 0.8rem;
  margin-top: 0.25rem;
}

/* Lockout — red badge in the card header for locked users. Click reveals
   reason in the lockout-info dialog (privacy: not displayed by default). */
.locked-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  margin-left: auto;
  padding: 0.2rem 0.55rem;
  background: rgba(220, 38, 38, 0.12);
  color: #dc2626;
  border: 1px solid rgba(220, 38, 38, 0.45);
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
  cursor: pointer;
  letter-spacing: 0.02em;
}

.locked-badge:hover {
  background: rgba(220, 38, 38, 0.18);
}

.locked-badge:focus-visible {
  outline: 2px solid #dc2626;
  outline-offset: 1px;
}

.lockout-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  color: #dc2626;
  font-weight: 600;
}

.lockout-warning {
  margin: 0 0 0.85rem 0;
  font-size: 0.9rem;
  color: var(--p-text-color);
  line-height: 1.4;
}

.lockout-info {
  display: grid;
  gap: 0.65rem;
  margin-bottom: 0.5rem;
}

.lockout-info-row {
  display: grid;
  grid-template-columns: 5rem 1fr;
  gap: 0.5rem;
  align-items: start;
}

.lockout-info-label {
  font-size: 0.78rem;
  color: var(--p-text-muted-color);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.lockout-info-value {
  font-size: 0.9rem;
  color: var(--p-text-color);
  word-break: break-word;
}

.lockout-info-empty {
  text-align: center;
  padding: 1rem 0;
  color: var(--p-text-muted-color);
}
</style>
