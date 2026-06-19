#!/usr/bin/env node
/**
 * UX consistency gate — grep-based lint catching the patterns the audit
 * (2026-05-05) flagged. Run with `node scripts/ux_gate.mjs` or via the
 * `lint:ux` npm script. Exits 1 on any violation.
 *
 * Banned patterns:
 *   1. severity="warning"  — invalid in PrimeVue v4 (use "warn")
 *   2. severity="danger" on <Toast> or <Message> — those use "error"
 *   3. severity="error" on <Tag> or <Button> — those use "danger"
 *   4. window.confirm( / window.alert( / bare confirm( / bare alert(
 *      in src/views/ + src/components/ — use useDestructiveConfirm or Toast
 *   5. <Button icon="..."> without `label`, `aria-label`, or `:aria-label`
 *      attribute — screen reader users hear "button" with no context
 *
 * Allowed exceptions:
 *   - test files (*.spec.js, *.test.js, tests/**)
 *   - games widgets (experimental subsystem, deferred)
 *
 * Add new bans here as the codebase tightens. Each ban prints a list of
 * (file:line) violations with the offending text.
 */
import { execSync } from 'node:child_process';
import { readFileSync, statSync } from 'node:fs';
import { resolve, join } from 'node:path';

// Scan root: env var override (UX_GATE_ROOT) for negative-tests + CI in
// alternate trees, otherwise the directory above this script (the
// frontend root). Without override the gate scans the canonical
// `src/views` and `src/components` subtrees.
const ROOT = resolve(process.env.UX_GATE_ROOT || new URL('..', import.meta.url).pathname);
const SCAN_DIRS = ['src/views', 'src/components'];
const SKIP_PATHS = [
  'src/components/games/widgets/BigButton.vue', // experimental, uses prompt+alert
];
const SKIP_TEST_GLOB = /(\.spec\.|\.test\.|__tests__\/|^tests\/)/;

function listVueFiles() {
  const all = [];
  for (const dir of SCAN_DIRS) {
    const out = execSync(`find ${join(ROOT, dir)} -name '*.vue'`, { encoding: 'utf8' });
    for (const line of out.split('\n')) {
      if (!line) continue;
      const rel = line.slice(ROOT.length + 1);
      if (SKIP_PATHS.includes(rel)) continue;
      if (SKIP_TEST_GLOB.test(rel)) continue;
      all.push({ abs: line, rel });
    }
  }
  return all;
}

function grepHits(content, regex) {
  const hits = [];
  const lines = content.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(regex);
    if (m) hits.push({ line: i + 1, text: lines[i].trim() });
  }
  return hits;
}

const violations = {
  'severity="warning" (use "warn" — v4 vocabulary)': [],
  'severity="error" on Tag/Button (those use "danger")': [],
  'severity="danger" on Toast/Message (those use "error")': [],
  'native confirm()/alert() (use useDestructiveConfirm or Toast)': [],
  'icon-only Button without aria-label': [],
  'unregistered PrimeVue component (template tag without import)': [],
};

// PrimeVue v4 component registry — every PascalCase component used in
// templates must have a matching `import Foo from "primevue/foo"`. Without
// it, Vue renders the tag as a raw HTML element (e.g., <DATATABLE>) with
// no functionality. Caught 32 sites in S110 — this rule prevents regression.
const PV_COMPONENTS = [
  'Calendar','DatePicker','Dropdown','Select','InputSwitch','ToggleSwitch',
  'OverlayPanel','Popover','InputMask','Sidebar','Drawer','TabView',
  'Listbox','MultiSelect','Tree','TreeTable','OrderList','PickList','Steps',
  'Stepper','InputNumber','InputText','Textarea','Password','RadioButton',
  'Checkbox','Slider','Rating','SelectButton','SplitButton','Toolbar',
  'Card','Panel','Fieldset','Splitter','SplitterPanel','ScrollPanel',
  'Skeleton','ProgressBar','ProgressSpinner','Avatar','Badge','Chip','Tag',
  'Divider','Knob','MeterGroup','Galleria','Carousel','Timeline','BlockUI',
  'Toast','Message','InlineMessage','ConfirmDialog','ConfirmPopup','Dialog',
  'DynamicDialog','OverlayBadge','Breadcrumb','MegaMenu','TabMenu',
  'PanelMenu','ContextMenu','TieredMenu','Dock','DataView','DataTable',
  'Column','ColumnGroup','VirtualScroller','Paginator','TreeSelect',
  'AutoComplete','CascadeSelect','InputOtp','InputGroup','InputGroupAddon',
  'FloatLabel','IftaLabel','IconField','InputIcon','FileUpload','Editor',
  'SpeedDial','Terminal','Tabs','TabList','Tab','TabPanels','TabPanel',
  'Accordion','AccordionPanel','AccordionHeader','AccordionContent',
];

function extractTemplateBlock(src) {
  // Outer-most <template> ... </template> with proper depth handling so
  // nested <template #slot> blocks are included.
  const open = /<template[^>]*>/.exec(src);
  if (!open) return '';
  let depth = 1;
  let i = open.index + open[0].length;
  while (i < src.length && depth > 0) {
    const o = src.indexOf('<template', i);
    const c = src.indexOf('</template', i);
    if (c === -1) return src.slice(open.index + open[0].length);
    if (o !== -1 && o < c) { depth++; i = o + 9; }
    else { depth--; i = c + 10; if (depth === 0) return src.slice(open.index + open[0].length, c); }
  }
  return '';
}

const files = listVueFiles();

for (const { abs, rel } of files) {
  const src = readFileSync(abs, 'utf8');

  // 1. severity="warning"
  for (const h of grepHits(src, /severity="warning"/)) {
    violations['severity="warning" (use "warn" — v4 vocabulary)'].push(`${rel}:${h.line}  ${h.text}`);
  }

  // 2. severity="error" on Tag/Button — heuristic: grep `severity="error"` and see if the
  //    component on that line is <Tag or <Button.
  for (const h of grepHits(src, /<(Tag|Button)\b[^>]*severity="error"/)) {
    violations['severity="error" on Tag/Button (those use "danger")'].push(`${rel}:${h.line}  ${h.text}`);
  }

  // 3. severity="danger" on Toast/Message
  for (const h of grepHits(src, /<(Toast|Message)\b[^>]*severity="danger"/)) {
    violations['severity="danger" on Toast/Message (those use "error")'].push(`${rel}:${h.line}  ${h.text}`);
  }

  // 4. native confirm()/alert() — exclude useConfirm, confirmAsync, ConfirmDialog
  for (const h of grepHits(src, /\b(window\.)?(confirm|alert)\(/)) {
    const txt = h.text;
    if (/useConfirm|confirmAsync|confirmDestructive|ConfirmDialog|ConfirmationService/.test(txt)) continue;
    violations['native confirm()/alert() (use useDestructiveConfirm or Toast)'].push(`${rel}:${h.line}  ${txt}`);
  }

  // 6. Unregistered PrimeVue component — template uses <Foo> but no
  //    `import Foo from 'primevue/...'`. Caught the S110 32-site bug.
  const tplBlock = extractTemplateBlock(src);
  if (tplBlock) {
    for (const comp of PV_COMPONENTS) {
      const tagRe = new RegExp(`<${comp}\\b`);
      if (!tagRe.test(tplBlock)) continue;
      const importRe = new RegExp(`\\bimport\\s+${comp}\\b`);
      if (importRe.test(src)) continue;
      // Find the line number of the first offending tag
      const lines = src.split('\n');
      let lineNum = 1;
      for (let i = 0; i < lines.length; i++) {
        if (tagRe.test(lines[i])) { lineNum = i + 1; break; }
      }
      violations['unregistered PrimeVue component (template tag without import)'].push(
        `${rel}:${lineNum}  <${comp}>  (no `+`import ${comp} from "primevue/${comp.toLowerCase()}")`
      );
    }
  }

  // 5. icon-only Button without aria-label
  // Scan for `<Button ... icon="..." ... />` (or with content) where the attribute set lacks label/aria-label.
  const buttonRe = /<Button\b([^>]*?)\/?>/gs;
  for (const m of src.matchAll(buttonRe)) {
    const attrs = m[1];
    const hasIcon = /\bicon\s*=/.test(attrs) || /\b:icon\s*=/.test(attrs);
    const hasLabel = /\blabel\s*=/.test(attrs) || /\b:label\s*=/.test(attrs)
      || /\baria-label\s*=/.test(attrs) || /\b:aria-label\s*=/.test(attrs);
    if (hasIcon && !hasLabel) {
      const lineNum = src.slice(0, m.index).split('\n').length;
      const lineText = src.split('\n')[lineNum - 1]?.trim() || '';
      violations['icon-only Button without aria-label'].push(`${rel}:${lineNum}  ${lineText}`);
    }
  }
}

let total = 0;
let exitCode = 0;
for (const [rule, hits] of Object.entries(violations)) {
  if (hits.length === 0) continue;
  total += hits.length;
  // aria-label rule is currently warn-only (long tail of remaining sites)
  const isWarn = rule.startsWith('icon-only Button');
  console.log(`\n${isWarn ? 'WARN' : 'FAIL'}  ${rule}  [${hits.length}]`);
  for (const h of hits.slice(0, 30)) console.log(`  ${h}`);
  if (hits.length > 30) console.log(`  ... and ${hits.length - 30} more`);
  if (!isWarn) exitCode = 1;
}

if (total === 0) {
  console.log('\n✓ ux gate clean — no banned patterns found');
} else if (exitCode === 0) {
  console.log(`\n✓ ux gate passed (${total} warnings, 0 fails)`);
} else {
  console.log(`\n✗ ux gate FAILED — fix the FAIL rules above`);
}
process.exit(exitCode);
