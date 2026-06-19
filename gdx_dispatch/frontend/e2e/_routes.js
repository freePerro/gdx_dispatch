// Parses gdx/frontend/src/router/index.js and exports a normalized route list.
// Hand-rolled regex parser (avoids importing Vue/router at test time).
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROUTER_FILE = path.resolve(__dirname, '../src/router/index.js');

export function loadRoutes() {
  const src = fs.readFileSync(ROUTER_FILE, 'utf8');

  // Match every `{ path: '...', name: '...', ... }` route object inside the routes array.
  // We intentionally skip redirects (presence of `redirect:`) since they have no view to render.
  const routeRe = /\{\s*path:\s*['"`]([^'"`]+)['"`]([^}]*)\}/g;
  const out = [];
  let m;
  const seen = new Set();
  while ((m = routeRe.exec(src)) !== null) {
    const [, p, rest] = m;
    if (/redirect:/.test(rest)) continue;
    if (!/component:/.test(rest)) continue; // skip non-route matches (e.g. router.push targets)
    if (seen.has(p)) continue;
    seen.add(p);
    const nameMatch = rest.match(/name:\s*['"`]([^'"`]+)['"`]/);
    const isPublic = /meta:\s*\{[^}]*public:\s*true/.test(rest);
    out.push({
      path: p,
      name: nameMatch ? nameMatch[1] : null,
      public: isPublic,
      hasParam: /:[A-Za-z_]+/.test(p),
    });
  }
  return out;
}
