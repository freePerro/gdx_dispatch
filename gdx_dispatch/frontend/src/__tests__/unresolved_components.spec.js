/**
 * Architectural lint — every PascalCase component tag used in a .vue template
 * must be imported (or locally declared) in that file's script.
 *
 * 2026-08-18 (Doug): the PDF Template Editor's Save button "didn't show".
 * PdfTemplateEditorView.vue used <Button> and <ColorPicker> without importing
 * either. This app registers NO global components and has no auto-import
 * resolver, so Vue fell back to native elements: <Button> became a bare
 * <button> whose `label` prop is just an ignored attribute — a tiny, empty,
 * unstyled button — and <ColorPicker> became an HTMLUnknownElement that
 * renders nothing. Unit tests passed the whole time because the spec stubbed
 * Button globally; only the real app resolved (or failed to resolve) the
 * import. This gate closes that gap at the source level.
 *
 * Recovery when this fires: add the missing `import X from "primevue/x"`
 * (or wherever the component lives) to the flagged file.
 */
import { describe, expect, it } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { basename, join, relative } from 'node:path';
import { parse } from 'vue/compiler-sfc';

const SRC_DIR = join(__dirname, '..');
const REPO_ROOT = join(SRC_DIR, '..', '..', '..', '..');

// Resolved without an import: Vue built-ins, and vue-router's plugin-registered
// components. Nothing else is global in this app (see main.js) — if that ever
// changes, extend this list in the same commit that registers the global.
const GLOBALLY_RESOLVED = new Set([
  'Transition', 'TransitionGroup', 'KeepAlive', 'Teleport', 'Suspense',
  'RouterLink', 'RouterView',
]);

function walk(dir) {
  const out = [];
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry.startsWith('.')) continue;
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) out.push(...walk(full));
    else if (full.endsWith('.vue')) out.push(full);
  }
  return out;
}

/** Tags used in markup, imports/declarations available to resolve them. */
export function findUnresolvedTags(source, selfName = '') {
  // Vue's own SFC parser, not regexes: comments and <style> bodies never
  // reach the tag walk, and the template AST is the same ground truth the
  // compiler resolves against. (An earlier regex version of this drew
  // CodeQL "incomplete HTML sanitization" findings — pattern-matched as a
  // sanitizer, which this is not; the real parser moots the whole class.)
  const { descriptor } = parse(String(source));
  const script = [descriptor.script?.content, descriptor.scriptSetup?.content]
    .filter(Boolean)
    .join('\n');

  const used = new Set();
  const visit = (node) => {
    // NodeTypes.ELEMENT === 1; comment nodes are a different type and fall out.
    if (node?.type === 1 && /^[A-Z]/.test(node.tag)) used.add(node.tag);
    for (const child of node?.children || []) visit(child);
  };
  visit(descriptor.template?.ast);

  const resolved = new Set(GLOBALLY_RESOLVED);
  // A recursive SFC resolves itself by filename (FolderTreeNode.vue).
  if (selfName) resolved.add(selfName);
  // Default imports: `import Button from "primevue/button"`.
  for (const [, name] of script.matchAll(/import\s+([A-Za-z0-9_$]+)\s+from/g)) resolved.add(name);
  // Named imports: `import { FilterMatchMode as FMM, X } from "..."`.
  for (const [, group] of script.matchAll(/import\s*\{([^}]*)\}\s*from/g)) {
    for (const piece of group.split(',')) {
      const name = piece.split(/\bas\b/).pop().trim();
      if (name) resolved.add(name);
    }
  }
  // Local declarations: `const Editor = defineAsyncComponent(...)` etc.
  for (const [, name] of script.matchAll(/(?:const|let|var|function)\s+([A-Z][A-Za-z0-9_$]*)/g)) resolved.add(name);

  return [...used].filter((tag) => !resolved.has(tag)).sort();
}

describe('no unresolved component tags', () => {
  it('every PascalCase tag in a template resolves to an import or local declaration', () => {
    const violations = [];
    // .story.vue files mount inside Histoire, which provides Story/Variant
    // as globals of its own — they are not part of the app build.
    const files = walk(SRC_DIR).filter((f) => !f.endsWith('.story.vue'));
    for (const file of files) {
      const missing = findUnresolvedTags(readFileSync(file, 'utf8'), basename(file, '.vue'));
      if (missing.length) {
        violations.push(`${relative(REPO_ROOT, file)}: ${missing.join(', ')}`);
      }
    }
    if (violations.length) {
      throw new Error(
        `\n${violations.length} file(s) use component tags with no import — Vue will render\n` +
        'these as native/unknown elements (an empty <button>, an invisible box):\n\n' +
        `${violations.join('\n')}\n\n` +
        'Fix: import the component in that file\'s <script> block.',
      );
    }
  });

  /* The gate's own fixtures — prove it fires and prove each resolution path
   * is honored, so a refactor of the scanner can't silently blind it. */
  it('catches the reported bug: a tag with no import', () => {
    expect(findUnresolvedTags('<template><Button label="Save" /></template>\n<script setup></script>'))
      .toEqual(['Button']);
  });
  it('honors default imports', () => {
    expect(findUnresolvedTags('<template><Button /></template>\n<script setup>import Button from "primevue/button";</script>'))
      .toEqual([]);
  });
  it('honors named and aliased imports', () => {
    expect(findUnresolvedTags('<template><Chart /></template>\n<script setup>import { BaseChart as Chart } from "./charts";</script>'))
      .toEqual([]);
  });
  it('honors local declarations (defineAsyncComponent)', () => {
    expect(findUnresolvedTags('<template><Editor /></template>\n<script setup>const Editor = defineAsyncComponent(() => import("./Editor.vue"));</script>'))
      .toEqual([]);
  });
  it('honors self-reference by filename in a recursive component', () => {
    expect(findUnresolvedTags('<template><TreeNode /></template>\n<script setup></script>', 'TreeNode'))
      .toEqual([]);
  });
  it('ignores tags that only appear in comments', () => {
    expect(findUnresolvedTags('<template><!-- <Button> --><div /></template>\n<script setup></script>'))
      .toEqual([]);
  });
});
