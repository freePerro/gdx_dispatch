/**
 * Issue #100, display half — the Plugins admin page must not claim nothing is
 * installed while plugins are running.
 *
 * What happened (prod, 2026-07): `plugin_registry` was empty because every
 * plugin on this deployment is installed from an UPLOADED WHEEL, not from a
 * package index. The "Installed packages" table reads only that registry, so it
 * rendered "No plugin packages installed yet." while three plugins were loaded
 * and serving. An operator read that as "the plugin fell off", re-added it by
 * pasting a wheel filename into the Install field, and wedged plugin-host into
 * a permanent /ready 503. The backend half was fixed in #105; the heading and
 * the empty state — the part that actually misled a human — were not.
 *
 * These are ABSENCE and STRUCTURAL assertions on the template. They cannot
 * prove the page reads correctly to a person; only the browser walk can, and
 * one is owed on prod where `plugin_registry` is genuinely empty and three
 * plugins are genuinely running. What they CAN do is fail if the flat claim
 * comes back.
 */
import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const view = fs.readFileSync(
  path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'PluginsAdminView.vue'),
  'utf8',
);

describe('Plugins admin never claims nothing is installed while plugins run', () => {
  it('the flat "no packages installed" claim is gone', () => {
    expect(view).not.toContain('No plugin packages installed yet.');
  });

  it('the REGISTRY table\'s own empty state is conditional and reassuring', () => {
    // An adversarial audit broke the first version of this assertion: it
    // scanned the file for ANY empty block containing v-if/v-else, so reverting
    // this table to a flat "Nothing here." while a v-if lived in the unrelated
    // "Running now" block still passed. It could not fail for its own defect.
    //
    // Anchor to THIS table by its test ids instead. Both branches must exist:
    // the reassuring one (something is installed by another path) and the
    // genuinely-empty one.
    expect(view).toContain('data-testid="registry-empty-reassure"');
    expect(view).toContain('data-testid="registry-empty-none"');

    // ...and the reassuring branch must be guarded by the counts, not shown
    // unconditionally, or it becomes the opposite lie.
    const reassure = view.match(
      /<span v-if="([^"]*)" data-testid="registry-empty-reassure">/,
    );
    expect(reassure, 'reassuring branch is not conditional').not.toBeNull();
    expect(reassure[1]).toMatch(/artifacts\.length/);
    expect(reassure[1]).toMatch(/running\.length/);
  });

  it('the empty state points at headings that actually exist', () => {
    // It previously said "see Uploaded plugins above" — a heading this same
    // change had renamed. A reassurance that sends you to a section that isn't
    // there is just a quieter lie.
    const referenced = [...view.matchAll(/<em>([^<]+)<\/em>/g)].map((m) => m[1].trim());
    for (const name of referenced) {
      expect(
        view.includes(`>${name}<`) || view.includes(`${name}</h3>`),
        `empty state references a section heading that does not exist: "${name}"`,
      ).toBe(true);
    }
  });

  it('no heading claims to list every installed plugin', () => {
    // "Installed packages" was the lie in three words: the table holds registry
    // rows only. Headings must say which install PATH they describe.
    expect(view).not.toMatch(/<h3[^>]*>\s*Installed packages\s*<\/h3>/);
  });
});
