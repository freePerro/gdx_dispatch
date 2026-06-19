// Default favorites seeded into the sidebar on first load. Onboarding is here
// so a brand-new tenant's admin lands on a useful checklist without having to
// hunt through the Admin category.
export const DEFAULT_FAVORITES = [
  { to: '/onboarding', label: 'Onboarding', icon: 'pi pi-flag' },
  { to: '/estimates', label: 'Estimates', icon: 'pi pi-file-edit' },
  { to: '/planner', label: 'Planner', icon: 'pi pi-calendar-plus' },
  { to: '/customers', label: 'Customers', icon: 'pi pi-users' },
  { to: '/dispatch', label: 'Dispatch', icon: 'pi pi-map' },
  { to: '/billing', label: 'Billing', icon: 'pi pi-dollar' },
  { to: '/leads', label: 'Leads', icon: 'pi pi-user-plus' },
];

// Returns { favorites, shouldPersist }.
//   raw === null  → user has never set favorites; seed defaults and persist
//   raw === '[]'  → user explicitly cleared their favorites; respect that
//   raw === '...' → user has favorites; parse and use
//   parse fails   → fall back to empty list, don't persist (avoid clobbering)
export function resolveInitialFavorites(raw, defaults = DEFAULT_FAVORITES) {
  if (raw === null || raw === undefined) {
    return { favorites: [...defaults], shouldPersist: true };
  }
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return { favorites: [], shouldPersist: false };
    return { favorites: parsed, shouldPersist: false };
  } catch {
    return { favorites: [], shouldPersist: false };
  }
}
