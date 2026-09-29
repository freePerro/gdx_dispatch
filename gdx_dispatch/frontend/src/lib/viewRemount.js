/**
 * Remount a view when navigation moves it from one record to another.
 *
 * Every detail view (/estimates/:id, /jobs/:id, /customers/:id, ...) loads its
 * record once, in onMounted. vue-router reuses the mounted component when only
 * the params change, so /estimates/A -> /estimates/B left A's data on screen
 * while every button (PDF, send, autosave) acted on B. Duplicate Estimate hit
 * it every time; the command palette and any record-to-record link hit it too.
 *
 * The blunt fix — `:key="$route.fullPath"` on <router-view> — was removed on
 * purpose (see App.vue): several views strip query params with
 * router.replace, and EstimateView flips /estimates/new -> /estimates/:id
 * after creating a draft and must NOT unmount there. So, within the same
 * view component:
 *   - params -> different params (/estimates/A -> /estimates/B): remount
 *   - params -> no params (/estimates/A -> /estimates/new): remount
 *   - no params -> params (/estimates/new -> /estimates/B): remount, because
 *     the command palette can push an existing estimate from the blank form
 *   - ...UNLESS the view itself announced the move with
 *     keepMountedThroughNextNavigation() — the draft getting its own id
 *   - query-only changes: keep mounted
 */
import { onScopeDispose, ref } from 'vue';
import { sameViewComponent } from './scrollRestore';

function paramsKey(route) {
  const p = route?.params || {};
  return JSON.stringify(
    Object.keys(p).sort()
      .filter((k) => p[k] !== undefined && p[k] !== null && p[k] !== '')
      .map((k) => [k, p[k]]),
  );
}

/** True when `to` shows a different record (or none) in the same view as `from`. */
export function isRecordSwitch(to, from) {
  if (!sameViewComponent(to, from)) return false;
  return paramsKey(to) !== paramsKey(from);
}

let keepFor = null;
let lastRemountTo = null;

/**
 * Call immediately before a router.replace/push to `path` that gives the
 * CURRENT screen its own id (a draft that was just created). That navigation
 * — and only a navigation landing exactly on `path` — will not remount the
 * view, so in-progress input and queued uploads survive. The mark is spent by
 * its own navigation (even an aborted one) or by the next SUCCESSFUL
 * navigation anywhere, so a stale mark cannot exempt a later record switch —
 * and an unrelated navigation that is cancelled by the marked one does not
 * spend it first.
 */
export function keepMountedThroughNextNavigation(path) {
  keepFor = path;
}

/**
 * True when the navigation that landed on `to` remounted the view. The
 * router's scrollBehavior asks this (it runs after afterEach) so scrolling and
 * remounting can never disagree: a remount is a new screen and goes to the
 * top; a kept-mounted flip (the draft getting its id) keeps the user's place.
 */
export function navigationRemounted(to) {
  return lastRemountTo !== null && lastRemountTo === to?.fullPath;
}

/**
 * Returns a ref that increments on every record switch. Bind it as the key
 * of the routed component so the view remounts and reloads. Must be called
 * inside a component's setup; the hook is removed when that scope ends.
 */
export function installViewRemount(router) {
  const epoch = ref(0);
  const remove = router.afterEach((to, from, failure) => {
    const keep = keepFor !== null && keepFor === to.fullPath;
    if (!failure || keep) keepFor = null;
    const remount = !failure && !keep && isRecordSwitch(to, from);
    lastRemountTo = remount ? to.fullPath : null;
    if (remount) epoch.value += 1;
  });
  onScopeDispose(remove);
  return epoch;
}
