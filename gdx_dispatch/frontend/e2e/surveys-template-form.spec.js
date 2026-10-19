/**
 * Surveys page — template create and edit reach the database (GDXA-1676).
 *
 * The dialog used to post template_type/send_channel/message: create was a
 * 422 every time and edit answered 200 while changing nothing. This walk
 * creates a CSAT template through the real dialog, edits its question and
 * type, and reads both back from the API. Screenshots of the dialog in light
 * and dark mode land in test-results/.
 *
 * Runs against a throwaway container serving the working tree.
 */
import { expect, request as pwRequest, test } from '@playwright/test';

const ENV = process.env;
const TENANT = ENV.E2E_TENANT_SLUG;
const EMAIL = ENV.E2E_EMAIL;
const PASSWORD = ENV.E2E_PASSWORD;

async function signIn(page, baseURL, theme) {
  const api = await pwRequest.newContext({ baseURL });
  const r = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-tenant-id': TENANT, 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(r.ok(), 'login').toBeTruthy();
  const { access_token: token } = await r.json();
  await page.addInitScript((a) => {
    if (window !== window.top) return;
    sessionStorage.setItem('gdx_access_token', a.token);
    sessionStorage.setItem('gdx_tenant_slug', a.tenant);
    localStorage.setItem('gdx_theme', a.theme);
  }, { token, tenant: TENANT, theme });
  const authed = await pwRequest.newContext({
    baseURL,
    extraHTTPHeaders: { authorization: `Bearer ${token}`, 'x-tenant-id': TENANT },
  });
  await api.dispose();
  return authed;
}

async function chooseKind(page, label) {
  await page.locator('[data-testid="survey-template-kind"]').click();
  await page.getByRole('option', { name: label }).click();
  await expect(page.getByRole('listbox')).toBeHidden();
}

for (const theme of ['light', 'dark']) {
  test(`survey template create + edit persist kind and question (${theme})`, async ({ page, baseURL }, info) => {
    const api = await signIn(page, baseURL, theme);
    const name = `E2E CSAT ${theme} ${Date.now()}`;

    await page.goto('/surveys');
    await page.getByRole('button', { name: '+ Template' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toBeVisible();
    await dialog.locator('input').first().fill(name);
    await chooseKind(page, 'CSAT (1–5)');
    await page.locator('[data-testid="survey-template-question"]').fill('How satisfied were you with the repair?');
    await page.locator('[data-testid="survey-template-follow-up"]').fill('Anything we could do better?');
    await page.screenshot({ path: info.outputPath(`surveys-dialog-${theme}.png`) });
    await page.locator('[data-testid="survey-template-save"]').click();
    await expect(dialog).toBeHidden();

    const row = page.getByRole('row', { name: new RegExp(name) });
    await expect(row).toContainText('csat');
    await expect(row).toContainText('How satisfied were you with the repair?');

    let tpl = (await (await api.get('/api/surveys/templates')).json()).find((t) => t.name === name);
    expect(tpl).toMatchObject({
      kind: 'csat',
      question: 'How satisfied were you with the repair?',
      follow_up_question: 'Anything we could do better?',
      active: true,
    });

    await row.getByRole('button', { name: 'Edit' }).click();
    await expect(page.locator('[data-testid="survey-template-question"]'))
      .toHaveValue('How satisfied were you with the repair?');
    await chooseKind(page, 'NPS (0–10)');
    await page.locator('[data-testid="survey-template-question"]').fill('How likely are you to recommend us?');
    await page.locator('[data-testid="survey-template-save"]').click();
    await expect(dialog).toBeHidden();
    await expect(row).toContainText('How likely are you to recommend us?');
    await page.screenshot({ path: info.outputPath(`surveys-list-${theme}.png`) });

    tpl = (await (await api.get('/api/surveys/templates')).json()).find((t) => t.name === name);
    expect(tpl.kind).toBe('nps');
    expect(tpl.question).toBe('How likely are you to recommend us?');

    // Soft-delete the fixture so the shared dev database is left as found.
    expect((await api.delete(`/api/surveys/templates/${tpl.id}`)).status()).toBe(204);
    await api.dispose();
  });
}
