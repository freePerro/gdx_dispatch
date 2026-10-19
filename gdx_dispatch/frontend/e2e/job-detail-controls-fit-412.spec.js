/**
 * GDXA-1876 — at a 412px phone width the Job Details card let the Assigned
 * Crew "Add" button run past the card's right edge (clipped, no sideways
 * scroll). The class: a flex child holding controls had no min-width:0, so it
 * kept its content width and overflowed. The same shape clipped a crew chip's
 * Remove button, the Blocked-by Select below ~330px, and the card grid's
 * 280px minimum column pushed the card itself off a 320px screen.
 *
 * jsdom applies no layout, so only a real browser proves this. Every control
 * must end inside its card, every card inside the screen, and a crew name must
 * stay readable. 700 and 820 are the two-column grid, where the card is
 * narrower than on a phone. The crew is mocked (a lead plus an arrived
 * non-lead tech with a long name, which shows the make-lead, undo-arrival and
 * remove buttons) because the seeded job has none. Light and dark gave identical geometry on the old code, so this runs
 * in dark, the mode the defect was reported in.
 */
import { test, expect } from './_fixtures.js';

const LEAD = '11111111-1111-4111-8111-111111111111';
const CREW = '22222222-2222-4222-8222-222222222222';

const CONTROLS = [
  'job-detail-priority',
  'job-detail-tech',
  `assignment-${LEAD}`,
  `assignment-${CREW}`,
  `assignment-make-lead-${CREW}`,
  `assignment-undo-arrival-${CREW}`,
  'assignment-remove-a2',
  'assignment-add-select',
  'assignment-add-btn',
  'dependency-select',
  'dependency-add-btn',
];

for (const width of [412, 360, 320, 700, 820]) {
  test(`job detail controls stay inside their card at ${width}px`, async ({ page, paramIds }) => {
    await page.setViewportSize({ width, height: 915 });
    await page.addInitScript(() => {
      if (window !== window.top) return;
      localStorage.setItem('gdx_theme', 'dark');
    });
    await page.route('**/api/technicians', (route) => route.fulfill({
      json: [
        { id: LEAD, name: 'Lead Tech' },
        { id: CREW, name: 'Maximiliana Rodriguez-Hernandez' },
        { id: '33333333-3333-4333-8333-333333333333', name: 'Spare Tech' },
      ],
    }));
    await page.route('**/api/jobs/*/assignments', (route) => {
      if (route.request().method() !== 'GET') return route.continue();
      return route.fulfill({
        json: [
          { id: 'a1', tech_id: LEAD, is_lead: true, arrived_at: null },
          { id: 'a2', tech_id: CREW, is_lead: false, arrived_at: '2026-10-19T13:00:00Z' },
        ],
      });
    });
    await page.goto(`/jobs/${paramIds.job}`);
    await expect(page.getByTestId('assignment-add-btn'), 'the seeded job must be patchable so the add row renders')
      .toBeVisible({ timeout: 20000 });
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');

    for (const id of CONTROLS) {
      const el = page.getByTestId(id);
      await expect(el, `${id} must render`).toBeVisible();
      const { right, cardRight } = await el.evaluate((node) => ({
        right: node.getBoundingClientRect().right,
        cardRight: node.closest('.card').getBoundingClientRect().right,
      }));
      expect.soft(right, `${id} right edge ${right} vs card ${cardRight}`).toBeLessThanOrEqual(cardRight + 0.5);
    }

    // Fitting by truncation must still leave the name readable: in the
    // two-column grid at 660-820px the card is narrower than on a phone.
    const nameWidth = await page.getByTestId(`assignment-${CREW}`).locator('.assignment-name')
      .evaluate((n) => n.getBoundingClientRect().width);
    expect.soft(nameWidth, `crew name is ${nameWidth}px wide`).toBeGreaterThanOrEqual(60);

    const maxCardRight = await page.locator('.card').evaluateAll(
      (cards) => Math.max(...cards.map((c) => c.getBoundingClientRect().right)),
    );
    expect.soft(maxCardRight, `rightmost card edge ${maxCardRight} vs screen ${width}`).toBeLessThanOrEqual(width);
  });
}
