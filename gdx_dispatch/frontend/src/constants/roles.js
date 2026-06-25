/**
 * Canonical role names + normalization — single source of truth (frontend).
 *
 * Mirror of backend core/roles.py. The same role is spelled multiple ways:
 *   - users.role (DB / JWT claim) uses SHORT legacy forms: 'tech', 'dispatch'
 *   - the RBAC catalog uses LONG forms: 'technician', 'dispatcher'
 *   - superadmin has appeared as super_admin / superadmin / super-admin
 *
 * Always normalizeRole() before comparing, so UI code never special-cases
 * variants. Canonical = the LONG form. See the wiki "Role naming conventions".
 */

export const OWNER = 'owner';
export const ADMIN = 'admin';
export const DISPATCHER = 'dispatcher';
export const TECHNICIAN = 'technician';
export const SALES = 'sales';
export const ACCOUNTING = 'accounting';
export const VIEWER = 'viewer';
export const MANAGER = 'manager';
export const SUPER_ADMIN = 'super_admin';

const ALIASES = {
  tech: TECHNICIAN,
  technician: TECHNICIAN,
  dispatch: DISPATCHER,
  dispatcher: DISPATCHER,
  superadmin: SUPER_ADMIN,
  super_admin: SUPER_ADMIN,
  'super-admin': SUPER_ADMIN,
};

/** Collapse any known spelling of a role to its canonical (long) form. */
export function normalizeRole(raw) {
  const r = String(raw ?? '').trim().toLowerCase();
  return ALIASES[r] || r;
}

/** True for the field technician role (either 'tech' or 'technician'). */
export function isTechnician(role) {
  return normalizeRole(role) === TECHNICIAN;
}

/** True for owner / admin / superadmin (full-access tier). */
export function isAdminTier(role) {
  const r = normalizeRole(role);
  return r === OWNER || r === ADMIN || r === SUPER_ADMIN;
}

const HUMAN = {
  owner: 'Owner',
  admin: 'Admin',
  dispatcher: 'Dispatcher',
  technician: 'Technician',
  sales: 'Sales',
  accounting: 'Accounting',
  viewer: 'Viewer',
  manager: 'Manager',
  super_admin: 'Super admin',
};

/** Human-readable label for a role, variant-aware. */
export function humanizeRole(role) {
  const r = normalizeRole(role);
  if (!r) return '';
  return HUMAN[r] || (r.charAt(0).toUpperCase() + r.slice(1));
}
