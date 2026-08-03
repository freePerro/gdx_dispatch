// PWA manifest contract — three files must agree for install + share-to-GDX
// to work, and nothing ties them together at build time:
//   public/manifest.webmanifest  (what Android reads)
//   AppBottomNav.vue             (what consumes the share/shortcut params)
//   router/index.js              (the routes those URLs land on)
// These tests pin the agreement so editing one side breaks loudly here
// instead of silently on a phone.
import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = join(__dirname, '..', '..');
const manifest = JSON.parse(
  readFileSync(join(ROOT, 'public', 'manifest.webmanifest'), 'utf8'),
);
const bottomNavSrc = readFileSync(
  join(ROOT, 'src', 'components', 'AppBottomNav.vue'),
  'utf8',
);
const routerSrc = readFileSync(join(ROOT, 'src', 'router', 'index.js'), 'utf8');
const indexHtml = readFileSync(join(ROOT, 'index.html'), 'utf8');

describe('manifest.webmanifest', () => {
  it('has the fields Chrome requires for installability', () => {
    expect(manifest.name).toBeTruthy();
    expect(manifest.short_name).toBe('GDX');
    expect(manifest.display).toBe('standalone');
    expect(manifest.start_url).toBeTruthy();
    const sizes = manifest.icons.map((i) => i.sizes);
    expect(sizes).toContain('192x192');
    expect(sizes).toContain('512x512');
    expect(manifest.icons.some((i) => i.purpose === 'maskable')).toBe(true);
  });

  it('every icon it declares exists in public/', () => {
    const declared = [
      ...manifest.icons.map((i) => i.src),
      ...manifest.shortcuts.flatMap((s) => (s.icons || []).map((i) => i.src)),
    ];
    for (const src of declared) {
      expect(existsSync(join(ROOT, 'public', src)), `${src} missing from public/`).toBe(true);
    }
  });

  it('start_url and shortcut/share URLs point at registered routes', () => {
    const paths = [
      manifest.start_url,
      manifest.share_target.action,
      ...manifest.shortcuts.map((s) => s.url.split('?')[0]),
    ];
    for (const p of paths) {
      expect(routerSrc.includes(`path: '${p}'`), `${p} not in router/index.js`).toBe(true);
    }
  });

  it('share_target params match what AppBottomNav consumes', () => {
    const { params } = manifest.share_target;
    for (const param of [params.title, params.text, params.url]) {
      expect(bottomNavSrc.includes(`'${param}'`), `${param} not consumed by AppBottomNav`).toBe(
        true,
      );
    }
    // GET is the only method that survives the login redirect for a
    // logged-out share (POST bodies don't).
    expect(manifest.share_target.method).toBe('GET');
  });

  it('the Quick note shortcut param is consumed by AppBottomNav', () => {
    const quickNote = manifest.shortcuts.find((s) => s.name === 'Quick note');
    expect(quickNote.url).toContain('capture=1');
    expect(bottomNavSrc).toMatch(/q\.capture === '1'/);
  });

  // Audit 2026-08-03: a share is REACHABLE, not just routable. The tech-role
  // remap off /mobile/planner must carry the query along (or the share text
  // dies in the redirect), and the gated branch must explain itself instead
  // of eating the share silently.
  it('tech-role planner remap preserves the query (share params survive)', () => {
    const remap = routerSrc.match(/'\/mobile\/planner'\)\s*return\s*\{([^}]*)\}/);
    expect(remap, 'tech remap for /mobile/planner not found').toBeTruthy();
    expect(remap[1]).toContain('query: to.query');
  });

  it('a share blocked by the office-only gate gets visible feedback, not silence', () => {
    const gated = bottomNavSrc.match(/if \(!showCapture\.value\) \{([\s\S]*?)\} else \{/);
    expect(gated, 'gated branch not found in AppBottomNav').toBeTruthy();
    expect(gated[1]).toContain('toast.add');
  });
});

describe('index.html PWA wiring', () => {
  it('links the manifest and both theme-colors', () => {
    expect(indexHtml).toContain('rel="manifest"');
    expect(indexHtml).toContain('/manifest.webmanifest');
    expect(indexHtml).toContain('apple-touch-icon');
    const themeMetas = indexHtml.match(/name="theme-color"/g) || [];
    expect(themeMetas.length).toBe(2);
  });
});

describe('sw.js', () => {
  it('stays fetch-handler-free (2026-04-11 stale-chunk incident guard)', () => {
    const sw = readFileSync(join(ROOT, 'public', 'sw.js'), 'utf8');
    expect(sw).not.toContain("addEventListener('fetch'");
    expect(sw).not.toContain('APP_SHELL');
  });

  it('default notification icon points at a file that ships in public/', () => {
    const sw = readFileSync(join(ROOT, 'public', 'sw.js'), 'utf8');
    const m = sw.match(/payload\.icon \|\| '([^']+)'/);
    expect(m).toBeTruthy();
    expect(existsSync(join(ROOT, 'public', m[1])), `${m[1]} missing from public/`).toBe(true);
  });
});
