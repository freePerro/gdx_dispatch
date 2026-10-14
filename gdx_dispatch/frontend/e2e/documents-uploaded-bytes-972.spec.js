// GDXA-972: uploaded html/svg never renders as a page of the app's origin.
// Uploads four files through the real API, then walks the Documents preview
// and Download in a real browser, light and dark.
import { test, expect, request as pwRequest } from '@playwright/test';

const TENANT = process.env.E2E_TENANT_SLUG;
const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || 'test-results';

const PNG = Buffer.from(
  '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489' +
  '0000000d49444154789c630001000000050001000d0a2db40000000049454e44ae426082',
  'hex',
);
const PDF = Buffer.from(
  '%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj ' +
  '3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF',
);
const FILES = {
  htmlpdf: { name: 'e2e972-invoice.pdf', mimeType: 'text/html', buffer: Buffer.from('<html><script>window.__pwned972=1</script>HTML</html>') },
  svg: { name: 'e2e972-logo.svg', mimeType: 'image/svg+xml', buffer: Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" onload="window.__pwned972=2"/>') },
  png: { name: 'e2e972-door.png', mimeType: 'image/png', buffer: PNG },
  pdf: { name: 'e2e972-real.pdf', mimeType: 'application/pdf', buffer: PDF },
};

test('uploaded markup is shown as text, saved on download, never framed or tabbed', async ({ page, baseURL }) => {
  const api = await pwRequest.newContext({ baseURL });
  const login = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(login.ok()).toBeTruthy();
  const { access_token } = await login.json();
  const auth = { authorization: `Bearer ${access_token}`, 'x-tenant-id': TENANT };

  const ids = {};
  for (const [k, f] of Object.entries(FILES)) {
    const r = await api.post('/api/documents', { headers: auth, multipart: { file: f } });
    expect(r.status(), k).toBe(201);
    ids[k] = (await r.json()).id;
  }
  try {
    // Server headers on the live route.
    for (const k of ['htmlpdf', 'svg']) {
      const r = await api.get(`/api/documents/${ids[k]}/download`, { headers: auth });
      const h = r.headers();
      console.log(`HEADERS ${k}: ${h['content-type']} | ${h['content-disposition']} | ${h['x-content-type-options']}`);
      expect(h['content-type']).toBe('application/octet-stream');
      expect(h['content-disposition']).toMatch(/^attachment/);
      expect(h['x-content-type-options']).toBe('nosniff');
    }
    const pngH = (await api.get(`/api/documents/${ids.png}/download`, { headers: auth })).headers();
    console.log(`HEADERS png: ${pngH['content-type']} | ${pngH['content-disposition']}`);
    expect(pngH['content-type']).toBe('image/png');

    // svg-as-photo refused on the live route.
    const asPhoto = await api.post('/api/documents', { headers: auth, multipart: { file: FILES.svg, as_photo: 'true' } });
    console.log(`AS_PHOTO svg: ${asPhoto.status()}`);
    expect(asPhoto.status()).toBe(415);

    await page.addInitScript((a) => {
      if (window !== window.top) return;
      sessionStorage.setItem('gdx_access_token', a.t);
      sessionStorage.setItem('gdx_tenant_slug', a.tid);
    }, { t: access_token, tid: TENANT });

    for (const theme of ['light', 'dark']) {
      await page.addInitScript((t) => { if (window === window.top) localStorage.setItem('gdx_theme', t); }, theme);
      await page.goto('/documents');
      expect(await page.evaluate(() => document.documentElement.getAttribute('data-theme') || 'light')).toBe(theme);
      const content = page.locator('[data-testid="doc-preview-content"]');
      const close = async () => { await page.keyboard.press('Escape'); await expect(content).toBeHidden(); };

      await page.locator(`[data-testid="doc-preview-${ids.htmlpdf}"]`).click();
      await expect(content.locator('[data-testid="doc-preview-text"]')).toContainText('<script>');
      await expect(content.locator('iframe')).toHaveCount(0);
      await page.screenshot({ path: `${SHOTS}/972-htmlpdf-${theme}.png` });

      // Download saves a file under its own name and opens no tab.
      let popups = 0;
      page.context().on('page', () => { popups += 1; });
      const [dl] = await Promise.all([
        page.waitForEvent('download'),
        page.locator('[data-testid="preview-download-btn"]').click(),
      ]);
      expect(dl.suggestedFilename()).toBe(FILES.htmlpdf.name);
      expect(popups).toBe(0);
      await close();

      await page.locator(`[data-testid="doc-preview-${ids.svg}"]`).click();
      await expect(content.locator('[data-testid="doc-preview-text"]')).toContainText('<svg');
      await expect(content.locator('img, iframe')).toHaveCount(0);
      await close();

      await page.locator(`[data-testid="doc-preview-${ids.png}"]`).click();
      await expect(content.locator('img.preview-image')).toBeVisible();
      await close();

      await page.locator(`[data-testid="doc-preview-${ids.pdf}"]`).click();
      await expect(content.locator('iframe.preview-pdf')).toHaveCount(1);
      await page.screenshot({ path: `${SHOTS}/972-pdf-${theme}.png` });
      await close();

      expect(await page.evaluate(() => window.__pwned972)).toBeUndefined();
    }
  } finally {
    for (const id of Object.values(ids)) await api.delete(`/api/documents/${id}`, { headers: auth });
    await api.dispose();
  }
});
