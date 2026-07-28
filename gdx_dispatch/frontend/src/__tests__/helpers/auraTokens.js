/**
 * Resolve a CSS colour value to the hex it actually renders as, per theme.
 *
 * Exists because the 2026-07-27 dark-mode sweep found that judging colours by
 * NAME is guesswork: `--p-surface-100` sounds theme-aware and is not (zinc.100
 * = #f4f4f5 in dark), and an asserted "surface-400+ is mid-grey and fine"
 * cutoff was wrong too (#a1a1aa = 2.56:1 against dark-mode white text).
 *
 * Two adversarial reviews shaped what this covers. The first killed
 * name-matching. The second found that resolving only `--p-*` tokens left the
 * same bug wide open in every other spelling — a literal `background: #f4f4f5`
 * was invisible to the gate, and there were 118 raw-hex backgrounds in the
 * tree. So this resolves VALUES, not tokens: hex, rgb()/rgba(), color-mix(),
 * PrimeVue tokens, and the app's own base.css tokens, in either theme.
 *
 * Anything genuinely unjudgeable still returns null, and every caller treats
 * null as "don't flag" — the gate must never guess.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import Aura from '@primeuix/themes/aura';
import { Theme } from '@primeuix/styled';

const ASSETS = join(__dirname, '..', '..', 'assets');

function readDeclarations(source) {
  const out = {};
  for (const m of source.matchAll(/(--[\w-]+)\s*:\s*([^;{}]+)/g)) out[m[1]] = m[2].trim();
  return out;
}

/** Slice a CSS source into the body of the first rule matching `selector`. */
function ruleBody(source, selector) {
  const at = source.indexOf(selector);
  if (at === -1) return '';
  const open = source.indexOf('{', at);
  const close = source.indexOf('}', open);
  return open === -1 || close === -1 ? '' : source.slice(open + 1, close);
}

function buildTables() {
  Theme.setTheme({
    preset: Aura,
    // Must mirror main.js — the dark block is keyed off this selector.
    options: { darkModeSelector: '[data-theme="dark"]', prefix: 'p', cssLayer: false },
  });
  const primevue = Theme.getCommonStyleSheet();
  const darkAt = primevue.indexOf('[data-theme="dark"]');
  const pvLight = readDeclarations(darkAt > 0 ? primevue.slice(0, darkAt) : primevue);
  // PrimeVue's dark block only re-declares what changes; the rest inherits.
  const pvDark = darkAt > 0 ? { ...pvLight, ...readDeclarations(primevue.slice(darkAt)) } : pvLight;

  // The app's own design tokens. base.css declares the DARK values on :root
  // and overrides for light under [data-theme="light"] — dark is the default
  // here, which is the opposite of PrimeVue's arrangement.
  const base = readFileSync(join(ASSETS, 'base.css'), 'utf8');
  const baseDark = readDeclarations(ruleBody(base, ':root'));
  const baseLight = { ...baseDark, ...readDeclarations(ruleBody(base, '[data-theme="light"]')) };

  // App-level overrides of PrimeVue tokens must win, or the measurement is of
  // a stylesheet the app does not actually ship.
  const overrides = readDeclarations(readFileSync(join(ASSETS, 'primevue-cta-contrast.css'), 'utf8'));

  return {
    light: { ...pvLight, ...baseLight, ...overrides },
    dark: { ...pvDark, ...baseDark, ...overrides },
  };
}

const TABLES = buildTables();

// The handful of CSS named colours that actually appear as backgrounds in
// real stylesheets. `background: white` is the same defect as `#ffffff` and
// was slipping through as unjudgeable.
const NAMED_COLORS = {
  white: '#ffffff', black: '#000000', silver: '#c0c0c0', gray: '#808080',
  grey: '#808080', whitesmoke: '#f5f5f5', ghostwhite: '#f8f8ff', snow: '#fffafa',
  ivory: '#fffff0', beige: '#f5f5dc', linen: '#faf0e6', lightgray: '#d3d3d3',
  lightgrey: '#d3d3d3', gainsboro: '#dcdcdc', azure: '#f0ffff', mintcream: '#f5fffa',
  seashell: '#fff5ee', oldlace: '#fdf5e6', floralwhite: '#fffaf0', aliceblue: '#f0f8ff',
  lavender: '#e6e6fa', cornsilk: '#fff8dc', lemonchiffon: '#fffacd', honeydew: '#f0fff0',
};

function toHex6(value) {
  const v = String(value).trim().replace(/\s*!important\s*$/, '');
  const named = NAMED_COLORS[v.toLowerCase()];
  if (named) return named;
  // 8- and 4-digit hex carry alpha; treat a fully-opaque one as its RGB.
  const long = /^#([0-9a-fA-F]{6})([0-9a-fA-F]{2})$/.exec(v);
  if (long) return parseInt(long[2], 16) === 255 ? `#${long[1].toLowerCase()}` : null;
  const short = /^#([0-9a-fA-F]{3})([0-9a-fA-F])$/.exec(v);
  if (short) {
    if (parseInt(short[2] + short[2], 16) !== 255) return null;
    const [r, g, b] = short[1];
    return `#${r}${r}${g}${g}${b}${b}`.toLowerCase();
  }
  if (!/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/.test(v)) return null;
  if (v.length === 4) return `#${v[1]}${v[1]}${v[2]}${v[2]}${v[3]}${v[3]}`;
  return v.toLowerCase();
}

const hex = (r, g, b) =>
  `#${[r, g, b].map((c) => Math.max(0, Math.min(255, Math.round(c))).toString(16).padStart(2, '0')).join('')}`;
const channels = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));

/** Composite `over` (with alpha) onto `base`, both #rrggbb. */
function composite(over, alpha, base) {
  const a = channels(over);
  const b = channels(base);
  return hex(...[0, 1, 2].map((i) => a[i] * alpha + b[i] * (1 - alpha)));
}

/** Split a function's argument list on top-level commas. */
function splitArgs(inner) {
  const parts = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < inner.length; i++) {
    const ch = inner[i];
    if (ch === '(') depth += 1;
    else if (ch === ')') depth -= 1;
    else if (ch === ',' && depth === 0) { parts.push(inner.slice(start, i)); start = i + 1; }
  }
  parts.push(inner.slice(start));
  return parts.map((p) => p.trim()).filter(Boolean);
}

/** Body of the first `name(...)` call in `value`, paren-balanced. */
function callBody(value, name) {
  const at = value.indexOf(`${name}(`);
  if (at === -1) return null;
  const open = at + name.length;
  let depth = 0;
  for (let i = open; i < value.length; i++) {
    if (value[i] === '(') depth += 1;
    else if (value[i] === ')') {
      depth -= 1;
      if (depth === 0) return value.slice(open + 1, i);
    }
  }
  return null;
}

/**
 * Resolve a CSS colour value to #rrggbb for the given mode ('light'|'dark').
 *
 * `backdrop` is what translucent values composite onto; it defaults to the
 * theme's content background, which is what a background actually sits on.
 * Returns null when the value is not a colour we can pin down.
 */
export function resolveColor(mode, value, backdrop = null, depth = 0) {
  if (depth > 12 || value == null) return null;
  const raw = String(value).trim().replace(/\s*!important\s*$/, '');
  if (!raw || raw === 'transparent' || raw === 'inherit' || raw === 'currentColor') return null;

  const direct = toHex6(raw);
  if (direct) return direct;

  const table = mode === 'dark' ? TABLES.dark : TABLES.light;
  const surface = backdrop
    || (depth === 0 ? resolveColor(mode, 'var(--p-content-background)', '#000000', 1) : null)
    || '#000000';

  // var(--tok) / var(--tok, fallback) — the fallback is dead whenever the
  // token is defined, which for these tokens is always.
  if (raw.startsWith('var(')) {
    const args = splitArgs(callBody(raw, 'var') || '');
    const named = table[args[0]];
    if (named !== undefined) return resolveColor(mode, named, surface, depth + 1);
    // Not in the table. Whether the fallback is live depends on WHY.
    // `--p-*` is a PrimeVue token that exists at runtime — per-component ones
    // like --p-inputtext-background are injected when the component mounts and
    // never appear in the common stylesheet — so its fallback is dead and the
    // real value is unjudgeable from here. Say so rather than measuring the
    // fallback and reporting a bug that does not exist.
    if (args[0].startsWith('--p-')) return null;
    // Anything else really is undefined — `var(--surface-card, #ffffff)` names
    // a token base.css does not declare, so that white is what ships.
    return args.length > 1 ? resolveColor(mode, args.slice(1).join(','), surface, depth + 1) : null;
  }

  // color-mix(in <space>, A [p%], B [p%]) — actually compute it, rather than
  // treating any color-mix as adaptive. A 95%-light mix laundered through an
  // adaptive base is still a light background.
  const mix = callBody(raw, 'color-mix');
  if (mix !== null) {
    const args = splitArgs(mix);
    if (args.length < 3) return null;
    const parse = (arg) => {
      const pct = /(-?[\d.]+)%\s*$/.exec(arg);
      return {
        color: arg.replace(/\s*(-?[\d.]+)%\s*$/, '').trim(),
        weight: pct ? parseFloat(pct[1]) / 100 : null,
      };
    };
    const a = parse(args[1]);
    const b = parse(args[2]);
    const ah = resolveColor(mode, a.color, surface, depth + 1);
    const bh = resolveColor(mode, b.color, surface, depth + 1);
    if (!ah || !bh) return null;
    let wa = a.weight;
    let wb = b.weight;
    if (wa == null && wb == null) { wa = 0.5; wb = 0.5; }
    else if (wa == null) wa = 1 - wb;
    else if (wb == null) wb = 1 - wa;
    const total = wa + wb;
    if (!total) return null;
    return composite(ah, wa / total, bh);
  }

  const rgbBody = callBody(raw, 'rgba') ?? callBody(raw, 'rgb');
  if (rgbBody !== null) {
    const nums = rgbBody.split(/[,/\s]+/).map((n) => n.trim()).filter(Boolean);
    if (nums.length < 3) return null;
    const [r, g, b] = nums.slice(0, 3).map(Number);
    if ([r, g, b].some(Number.isNaN)) return null;
    const alpha = nums.length > 3 ? Number(nums[3]) : 1;
    if (Number.isNaN(alpha)) return null;
    return alpha >= 1 ? hex(r, g, b) : composite(hex(r, g, b), alpha, surface);
  }

  return null;
}

/**
 * The colour layers a background declaration paints. A gradient contributes
 * every stop, so a light stop cannot hide behind an adaptive one.
 */
export function backgroundLayers(declaration) {
  const value = String(declaration).split(':').slice(1).join(':');
  // `background: linear-gradient(...), #f4f4f5` is TWO layers. Judging only
  // the first let a transparent gradient hide a flat light one behind it.
  const layers = splitArgs(value);
  if (layers.length > 1) return layers.flatMap((layer) => backgroundLayers(`background:${layer}`));
  const gradient =
    callBody(value, 'linear-gradient')
    ?? callBody(value, 'radial-gradient')
    ?? callBody(value, 'conic-gradient');
  if (gradient !== null) {
    return splitArgs(gradient)
      .map((stop) => stop.replace(/\s+(-?[\d.]+(px|%|rem|em|deg|turn)|to\s+\w+.*)$/i, '').trim())
      .filter((s) => s && !/^(to\s|\d+deg|in\s|circle|ellipse|at\s)/i.test(s));
  }
  // A single function call is ONE colour — splitting it yields its arguments,
  // which measures the wrong thing entirely (a color-mix reported as its
  // 22% emerald arm rather than the muted tint it actually paints).
  const trimmed = value.trim().replace(/\s*!important\s*$/, '');
  if (/^[a-z-]+\(/i.test(trimmed) && callBody(trimmed, trimmed.slice(0, trimmed.indexOf('('))) !== null) {
    return [trimmed];
  }
  // Shorthand may carry position/size/repeat; the colour is what resolves.
  return splitArgs(value).flatMap((part) => part.split(/\s+(?![^(]*\))/));
}

function relativeLuminance(h) {
  const [r, g, b] = channels(h)
    .map((c) => c / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG 2.x contrast ratio between two #rrggbb colours. */
export function contrastRatio(fgHex, bgHex) {
  const a = relativeLuminance(fgHex);
  const b = relativeLuminance(bgHex);
  const [hi, lo] = a > b ? [a, b] : [b, a];
  return (hi + 0.05) / (lo + 0.05);
}
