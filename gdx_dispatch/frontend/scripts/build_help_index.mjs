#!/usr/bin/env node
// Build the help index consumed by the in-app drawer.
//
// Reads every markdown file under gdx/frontend/src/help/articles/, parses
// frontmatter (--- yaml-ish key/value, one per line), validates required
// fields, and emits gdx/frontend/public/help-index.json.
//
// Run via `npm run build:help` (also wired as `predev` and `prebuild` in
// package.json so the index is always fresh).
//
// Frontmatter contract:
//   title:       required, string
//   role:        required, one of owner|admin|dispatcher|technician|all
//   tags:        optional, comma-separated string
//   related:     optional, comma-separated list of other article slugs
//   module:      optional, module key (jobs, dispatch, etc.) — used by
//                step-level module skip in tours
//   video_url:   optional
//   lang:        optional, default "en"

import { readFileSync, readdirSync, writeFileSync, statSync, mkdirSync } from 'node:fs';
import { dirname, join, basename } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const ARTICLES_DIR = join(ROOT, 'src/help/articles');
const OUT_PATH = join(ROOT, 'public/help-index.json');

const ROLES = new Set(['owner', 'admin', 'dispatcher', 'technician', 'tech', 'all']);

function parseFrontmatter(text) {
  if (!text.startsWith('---')) {
    return { frontmatter: {}, body: text };
  }
  const end = text.indexOf('\n---', 3);
  if (end === -1) return { frontmatter: {}, body: text };
  const fmText = text.slice(3, end).trim();
  const body = text.slice(end + 4).replace(/^\s*\n/, '');
  const fm = {};
  for (const line of fmText.split('\n')) {
    const m = line.match(/^([a-z_][a-z0-9_]*)\s*:\s*(.*)$/i);
    if (!m) continue;
    const key = m[1].trim();
    let value = m[2].trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    fm[key] = value;
  }
  return { frontmatter: fm, body };
}

function splitList(s) {
  if (!s) return [];
  return s.split(',').map((x) => x.trim()).filter(Boolean);
}

function listArticles() {
  let entries;
  try {
    entries = readdirSync(ARTICLES_DIR);
  } catch (e) {
    if (e.code === 'ENOENT') return [];
    throw e;
  }
  return entries
    .filter((f) => f.endsWith('.md'))
    .filter((f) => {
      try { return statSync(join(ARTICLES_DIR, f)).isFile(); } catch (_e) { return false; }
    });
}

function build() {
  const files = listArticles();
  const articles = [];
  const errors = [];
  const seenSlugs = new Set();

  for (const file of files) {
    const path = join(ARTICLES_DIR, file);
    const slug = basename(file, '.md');
    const raw = readFileSync(path, 'utf8');
    const { frontmatter: fm, body } = parseFrontmatter(raw);

    if (!fm.title) errors.push(`${file}: missing required 'title'`);
    if (!fm.role) errors.push(`${file}: missing required 'role'`);
    if (fm.role && !ROLES.has(fm.role.toLowerCase())) {
      errors.push(`${file}: invalid role '${fm.role}' (allowed: ${[...ROLES].join(', ')})`);
    }
    if (seenSlugs.has(slug)) errors.push(`${file}: duplicate slug '${slug}'`);
    seenSlugs.add(slug);

    articles.push({
      slug,
      title: fm.title || slug,
      role: (fm.role || 'all').toLowerCase(),
      tags: splitList(fm.tags),
      related: splitList(fm.related),
      module: fm.module || null,
      video_url: fm.video_url || null,
      lang: fm.lang || 'en',
      body,
    });
  }

  // Warn (not fail) on dangling related slugs.
  const slugSet = new Set(articles.map((a) => a.slug));
  for (const a of articles) {
    for (const ref of a.related) {
      if (!slugSet.has(ref)) {
        console.warn(`[help-index] WARN: ${a.slug}.md references unknown related slug '${ref}'`);
      }
    }
  }

  if (errors.length) {
    console.error('[help-index] BUILD FAILED — frontmatter errors:');
    for (const e of errors) console.error('  - ' + e);
    process.exit(1);
  }

  mkdirSync(dirname(OUT_PATH), { recursive: true });
  writeFileSync(OUT_PATH, JSON.stringify({ generated_at: new Date().toISOString(), articles }, null, 2));
  console.log(`[help-index] wrote ${articles.length} article${articles.length === 1 ? '' : 's'} → ${OUT_PATH}`);
}

build();
