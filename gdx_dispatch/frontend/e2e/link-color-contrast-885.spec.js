/**
 * GDXA-885 — text links clear WCAG AA in light mode, in a real browser.
 *
 * Views colour their links `var(--p-primary-color)`, Aura's light emerald
 * #10b981: 2.54:1 on white. base.css re-points that token at `--link-color`
 * (#047857) inside a, button and label. jsdom cannot resolve that cascade,
 * so this reads the painted colour:
 *   - a real link: the customer-name link on /customers (global-setup always
 *     seeds one customer);
 *   - probes of each element kind, styled the way the views style them;
 *   - the global token itself, which must stay emerald for fills and rings;
 *   - a page scan: no visible a/button/label paints #10b981 text.
 * Dark mode must be unchanged (#34d399).
 *
 * Scope: the shell rule reaches a, button and label (plus PrimeVue's
 * <Button link>, through its own token), and so does the scan. A <span> or <div> styled as a link (JobsView .job-number,
 * CustomerDetailView .job-link, EstimatesView .link-text) or an <a> that
 * inherits colour from an emerald parent stays #10b981 and is NOT checked
 * here; those need the owning view to use var(--link-color).
 */
import { test, expect } from './_fixtures.js';

const EMERALD_500 = 'rgb(16, 185, 129)';
const LINK_LIGHT = 'rgb(4, 120, 87)';
const LINK_DARK = 'rgb(52, 211, 153)';

function luminance(rgb) {
  const [r, g, b] = rgb
    .match(/[\d.]+/g)
    .slice(0, 3)
    .map((c) => Number(c) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a, b) {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

async function open(page, theme, path) {
  await page.addInitScript((t) => {
    if (window === window.top) localStorage.setItem('gdx_theme', t);
  }, theme);
  await page.goto(path);
  expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(theme);
}

/** Probes styled like the views: a scoped `color: var(--p-primary-color)`. */
const PROBE = () => {
  const host = document.createElement('div');
  host.style.background = '#ffffff';
  host.innerHTML =
    '<a href="#" data-probe="a" style="color: var(--p-primary-color)">link</a>' +
    '<button type="button" data-probe="button" style="color: var(--p-primary-color); background: none; border: 0">more</button>' +
    '<label data-probe="label" style="color: var(--p-primary-color)">add photo</label>' +
    '<span data-probe="span" style="color: var(--p-primary-color)">amount</span>' +
    // PrimeVue <Button link> paints from --p-button-link-color, resolved at :root.
    '<span data-probe="buttonLink" style="color: var(--p-button-link-color)">cancel</span>';
  document.body.appendChild(host);
  const out = {};
  for (const el of host.querySelectorAll('[data-probe]')) {
    out[el.dataset.probe] = getComputedStyle(el).color;
  }
  host.remove();
  return out;
};

test('light mode: text links paint #047857 (>= 4.5:1) and the global primary stays emerald', async ({ page }) => {
  await open(page, 'light', '/customers');
  const link = page.locator('.customer-name-link').first();
  await expect(link).toBeVisible({ timeout: 20000 });

  const real = await link.evaluate((el) => getComputedStyle(el).color);
  console.log(`customer-name-link light: ${real} ratio=${contrast(real, 'rgb(255,255,255)').toFixed(2)}`);
  expect(real).toBe(LINK_LIGHT);

  const probes = await page.evaluate(PROBE);
  console.log('probes light:', JSON.stringify(probes));
  for (const kind of ['a', 'button', 'label']) {
    expect(probes[kind], `${kind} probe`).toBe(LINK_LIGHT);
    expect(contrast(probes[kind], 'rgb(255,255,255)')).toBeGreaterThanOrEqual(4.5);
    expect(contrast(probes[kind], 'rgb(241,245,249)')).toBeGreaterThanOrEqual(4.5);
  }
  expect(probes.buttonLink, 'PrimeVue <Button link> token').toBe(LINK_LIGHT);
  // Not a link: untouched, so fills, borders and focus rings keep the brand.
  expect(probes.span).toBe(EMERALD_500);

  await page.screenshot({ path: 'test-results/link-color-885-light.png' });
});

for (const path of ['/customers', '/jobs', '/']) {
  test(`light mode: no visible link, button or label on ${path} paints emerald-500 text`, async ({ page }) => {
    await open(page, 'light', path);
    await page.waitForLoadState('networkidle');
    const offenders = await page.evaluate((emerald) => {
      return [...document.querySelectorAll('a, button, label')]
        .filter((el) => el.offsetParent !== null && el.textContent.trim())
        .filter((el) => getComputedStyle(el).color === emerald)
        .map((el) => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')} "${el.textContent.trim().slice(0, 30)}"`);
    }, EMERALD_500);
    expect(offenders).toEqual([]);
  });
}

test('dark mode: links keep #34d399', async ({ page }) => {
  await open(page, 'dark', '/customers');
  const link = page.locator('.customer-name-link').first();
  await expect(link).toBeVisible({ timeout: 20000 });
  expect(await link.evaluate((el) => getComputedStyle(el).color)).toBe(LINK_DARK);
  const probes = await page.evaluate(PROBE);
  console.log('probes dark:', JSON.stringify(probes));
  for (const kind of ['a', 'button', 'label', 'span', 'buttonLink']) expect(probes[kind], `${kind} probe`).toBe(LINK_DARK);
  await page.screenshot({ path: 'test-results/link-color-885-dark.png' });
});
