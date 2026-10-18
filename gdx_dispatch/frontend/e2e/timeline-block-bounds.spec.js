/**
 * Dispatch day board — a job card stays inside its card and its lane
 * (GDXA-1486).
 *
 * Two reports, one shape (block geometry exceeding card or lane bounds):
 *   - compact zoom (30px/h): a 1h card's time line spilled onto the next card;
 *   - jobs before the shift start (3, 4, 5, 7 AM against 8 AM) drew above the
 *     lane, over the tray and the tech header.
 *
 * In a real browser against the throwaway serving the new dist:
 *   1. seed five jobs today on one technician: 1h at 3, 4, 5 and 10 AM, and
 *      3h at 6 AM
 *   2. for default and compact zoom, light and dark: every card sits inside
 *      the lane, no two cards overlap, and nothing inside a card renders past
 *      its edges                                             → screenshots
 *   3. clean up: delete the seeded jobs
 */
import { test, expect, request as pwRequest } from '@playwright/test';

const EMAIL = process.env.E2E_EMAIL;
const PASSWORD = process.env.E2E_PASSWORD;
const SHOTS = process.env.E2E_SHOT_DIR || '.';

test('timeline cards stay inside the card and the lane at both zooms', async ({ page, baseURL }) => {
  test.setTimeout(180000);
  await page.setViewportSize({ width: 1600, height: 1200 });

  const api = await pwRequest.newContext({ baseURL });
  const login = await api.post('/auth/login', {
    headers: { 'content-type': 'application/json', 'x-e2e-test': 'true' },
    data: { email: EMAIL, password: PASSWORD },
  });
  expect(login.ok()).toBeTruthy();
  const { access_token } = await login.json();
  const auth = { authorization: `Bearer ${access_token}`, 'content-type': 'application/json', 'x-e2e-test': 'true' };

  const techs = await (await api.get('/api/technicians', { headers: auth })).json();
  const tech = (Array.isArray(techs) ? techs : techs.items || []).find((t) => t.active !== false);
  expect(tech, 'need an active technician').toBeTruthy();
  const customers = await (await api.get('/api/customers?per_page=1', { headers: auth })).json();
  const customer = (Array.isArray(customers) ? customers : customers.items || [])[0];
  expect(customer, 'need a customer').toBeTruthy();

  const now = new Date();
  const stamp = Date.now();
  const jobIds = [];
  try {
    // [start hour, hours]. 6 AM for 3h is a two-line card that starts early,
    // where an early note pinned to a corner drew over the customer.
    for (const [h, dur] of [[3, 1], [4, 1], [5, 1], [6, 3], [10, 1]]) {
      const r = await api.post('/api/jobs', {
        headers: auth,
        data: {
          title: `E2E bounds ${h}h ${stamp}`,
          customer_id: customer.id,
          job_type: 'Service Call',
          scheduled_at: new Date(now.getFullYear(), now.getMonth(), now.getDate(), h, 0, 0).toISOString(),
          technician_id: tech.id,
          assigned_to: tech.id,
          scheduled_duration_hours: dur,
        },
      });
      expect(r.ok(), `seed ${h}h: ${r.status()} ${await r.text()}`).toBeTruthy();
      jobIds.push((await r.json()).id);
    }

    await page.addInitScript((a) => {
      if (window !== window.top) return;
      sessionStorage.setItem('gdx_access_token', a.t);
    }, { t: access_token });

    const body = page.locator(`[data-testid="tech-timeline-body-${tech.id}"]`);
    const cards = jobIds.map((id) => page.locator(`[data-testid="timeline-job-${id}"]`));

    for (const compact of [false, true]) {
      for (const theme of ['light', 'dark']) {
        await page.goto('/dispatch');
        await page.evaluate(([c, t]) => {
          localStorage.setItem('gdx.dispatch.compact', c ? '1' : '0');
          localStorage.setItem('gdx_theme', t);
        }, [compact, theme]);
        await page.reload();
        for (const c of cards) await expect(c).toBeVisible({ timeout: 30000 });

        const geom = await page.evaluate(({ bodyId, ids }) => {
          const b = document.querySelector(`[data-testid="tech-timeline-body-${bodyId}"]`).getBoundingClientRect();
          return {
            body: { top: b.top, bottom: b.bottom },
            cards: ids.map((id) => {
              const el = document.querySelector(`[data-testid="timeline-job-${id}"]`);
              const r = el.getBoundingClientRect();
              // The card's lines (direct children) drawn past any card edge
              // are clipped text; two lines whose boxes cross draw over
              // each other.
              const kids = [...el.children].map((k) => k.getBoundingClientRect()).filter((k) => k.height > 0 && k.width > 0);
              const spill = kids.some((k) => k.top < r.top - 0.5 || k.bottom > r.bottom + 0.5
                || k.left < r.left - 0.5 || k.right > r.right + 0.5);
              const collide = kids.some((a, i) => kids.slice(i + 1).some((b) => a.left < b.right - 0.5
                && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5));
              // A card at least 70px wide has room for its whole start time.
              const start = el.querySelector('.block-start');
              const timeCut = r.width >= 70 && !!start && start.scrollWidth > start.clientWidth + 1;
              return {
                id, top: r.top, bottom: r.bottom, left: r.left, right: r.right,
                spill, collide, timeCut, text: el.innerText, title: el.getAttribute('title'),
              };
            }),
          };
        }, { bodyId: tech.id, ids: jobIds });

        const label = `${compact ? 'compact' : 'default'}/${theme}`;
        for (const c of geom.cards) {
          expect(c.top, `${label} ${c.id} above the lane`).toBeGreaterThanOrEqual(geom.body.top - 0.5);
          expect(c.bottom, `${label} ${c.id} below the lane`).toBeLessThanOrEqual(geom.body.bottom + 0.5);
          expect(c.spill, `${label} ${c.id} content spills out of the card: ${c.text}`).toBe(false);
          expect(c.collide, `${label} ${c.id} lines draw over each other: ${c.text}`).toBe(false);
          expect(c.timeCut, `${label} ${c.id} start time cut on a card wide enough for it`).toBe(false);
          expect(c.title, `${label} ${c.id} hover title names the start time`).toMatch(/\d{1,2}:00\s?AM/);
        }
        for (let i = 0; i < geom.cards.length; i++) {
          for (let j = i + 1; j < geom.cards.length; j++) {
            const a = geom.cards[i]; const b = geom.cards[j];
            const overlap = a.left < b.right - 0.5 && b.left < a.right - 0.5
              && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
            expect(overlap, `${label} cards ${a.id} and ${b.id} draw over each other`).toBe(false);
          }
        }
        // Shift start is the tech's own setting, so the amount is not fixed.
        expect(geom.cards[0].title).toMatch(/\+[\d.]+h early/);
        // The column (tray, lane) in one picture.
        await body.locator('..').screenshot({ path: `${SHOTS}/timeline-bounds-${compact ? 'compact' : 'default'}-${theme}.png` });
      }
    }
  } finally {
    await page.evaluate(() => {
      localStorage.setItem('gdx.dispatch.compact', '0');
      localStorage.setItem('gdx_theme', 'light');
    }).catch(() => {});
    for (const id of jobIds) await api.delete(`/api/jobs/${id}`, { headers: auth });
    await api.dispose();
  }
});
