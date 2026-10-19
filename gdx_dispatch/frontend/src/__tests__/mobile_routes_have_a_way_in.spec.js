/**
 * GDXA-1726 — every /mobile route has a visible way in.
 *
 * #396 deleted the Today wrap card, the only link to /mobile/summary, and
 * the day-summary screen sat URL-only for two months. A tech on a phone
 * cannot type a URL, so a route nothing links to is dead UI.
 *
 * Static net, not proof: for each /mobile route, some source file other
 * than the router, the route's own view and the tests must name its path
 * (in quotes or a template string) or its route name. A device walk still
 * proves the link is visible.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

const SRC_DIR = join(__dirname, '..');
const ROUTER = readFileSync(join(SRC_DIR, 'router/index.js'), 'utf8');

function sourceFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      if (name === '__tests__') continue;
      out.push(...sourceFiles(full));
    } else if (/\.(vue|js)$/.test(name) && !/\.spec\.js$/.test(name)) {
      out.push(full);
    }
  }
  return out;
}

// const MobileFooView = () => import('../views/MobileFooView.vue');
const VIEW_FILES = Object.fromEntries(
  [...ROUTER.matchAll(/const (\w+) = \(\) => import\('\.\.\/(views\/[^']+)'\)/g)]
    .map(([, ident, file]) => [ident, file]),
);

const MOBILE_ROUTES = [
  ...ROUTER.matchAll(/\{ path: '(\/mobile[^']*)', name: '([^']+)', component: (\w+)/g),
].map(([, path, name, component]) => ({
  path,
  name,
  // /mobile/jobs/:id is linked as `/mobile/jobs/${id}`
  linkPrefix: path.split('/:')[0],
  viewFile: VIEW_FILES[component],
}));

const FILES = sourceFiles(SRC_DIR).map((full) => ({
  rel: relative(SRC_DIR, full).replaceAll('\\', '/'),
  text: readFileSync(full, 'utf8'),
}));

function inboundFiles(route) {
  const prefix = route.linkPrefix;
  const needles = [`'${prefix}`, `"${prefix}`, `\`${prefix}`, `name: '${route.name}'`];
  return FILES
    .filter((f) => f.rel !== 'router/index.js' && f.rel !== route.viewFile)
    .filter((f) => needles.some((n) => f.text.includes(n)))
    .map((f) => f.rel);
}

describe('every /mobile route has a way in', () => {
  it('finds the /mobile routes in the router', () => {
    // A router refactor that breaks the parse must not pass as "no orphans".
    expect(MOBILE_ROUTES.length).toBeGreaterThan(10);
    expect(MOBILE_ROUTES.map((r) => r.path)).toContain('/mobile/summary');
  });

  it.each(MOBILE_ROUTES.map((r) => [r.path, r]))('%s is linked from somewhere', (_path, route) => {
    expect(inboundFiles(route)).not.toEqual([]);
  });

  it('reaches the day summary from Today', () => {
    const summary = MOBILE_ROUTES.find((r) => r.path === '/mobile/summary');
    expect(inboundFiles(summary)).toContain('views/MobileTodayView.vue');
  });
});
