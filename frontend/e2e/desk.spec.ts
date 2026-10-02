import type { Page } from "@playwright/test";
import { expect, test } from "@playwright/test";

import { collectPageErrors, stubNetwork } from "./support";

/**
 * The desk's windowed script in a real browser, where layout decides which
 * element scrolls: the script column on a wide window, the whole desk at 760 px
 * and below (studio.css). The Long Watch's first chapter is the mock's
 * 1,200-line chapter.
 */
const LONG_DESK = "/books/long-watch";
const CHAPTER_LINES = 1200;

/** Far enough down a 1,200-line chapter that none of its first rows is in view. */
const SCROLL_PX = 20_000;

/** What a reader would see: how far the script has moved, and what is on screen. */
async function scriptView(page: Page) {
  return page.evaluate(() => {
    const list = document.querySelector(".vrows");
    // Whichever ancestor scrolls, the list has moved by the sum of their offsets.
    let scrolled = 0;
    for (let n = list?.parentElement ?? null; n; n = n.parentElement) scrolled += n.scrollTop;
    const hit = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
    return {
      scrolled,
      lineAtCentre: hit?.closest("[data-line]")?.getAttribute("data-line") ?? null,
      linesInDom: document.querySelectorAll("[data-line]").length,
    };
  });
}

/** Scroll with the wheel over the middle of the window, as a reader would. */
async function wheelDown(page: Page, px: number) {
  const size = page.viewportSize()!;
  await page.mouse.move(size.width / 2, size.height / 2);
  for (let done = 0; done < px; done += 2_000) await page.mouse.wheel(0, 2_000);
}

async function openLongDesk(page: Page, width: number) {
  await page.setViewportSize({ width, height: 900 });
  await page.goto(LONG_DESK);
  await expect(page.getByText(`${CHAPTER_LINES} lines`)).toBeVisible();
  await expect(page.locator("[data-line]").first()).toBeVisible();
}

/** After scrolling: rows are on screen, and the page still holds only a window of them. */
async function expectWindowedRowsInView(page: Page) {
  await expect.poll(async () => (await scriptView(page)).scrolled).toBeGreaterThan(SCROLL_PX / 2);
  await expect.poll(async () => (await scriptView(page)).lineAtCentre).not.toBeNull();
  const { linesInDom } = await scriptView(page);
  expect(linesInDom).toBeGreaterThan(0);
  expect(linesInDom).toBeLessThan(CHAPTER_LINES / 4);
}

for (const width of [1440, 700]) {
  test.describe(`long script at ${width} px`, () => {
    test("scrolling keeps rows in view and the list windowed", async ({ page }) => {
      const errors = collectPageErrors(page);
      await stubNetwork(page);
      await openLongDesk(page, width);

      await wheelDown(page, SCROLL_PX);
      await expectWindowedRowsInView(page);
      expect(errors).toEqual([]);
    });

    test("j scrolls the selected line back into view", async ({ page }) => {
      await stubNetwork(page);
      await openLongDesk(page, width);
      const second = await page.locator("[data-line]").nth(1).getAttribute("data-line");

      await wheelDown(page, SCROLL_PX);
      await expectWindowedRowsInView(page);

      // The first line is selected on open; j selects the second and has to
      // bring it back from 20,000 px above.
      await page.keyboard.press("j");
      await expect(page.locator(`[data-line="${second}"]`)).toBeInViewport();
    });
  });
}

test("a resize across 760 px re-targets the list at the new scroller", async ({ page }) => {
  await stubNetwork(page);
  await openLongDesk(page, 1440);

  await page.setViewportSize({ width: 700, height: 900 });
  await wheelDown(page, SCROLL_PX);
  await expectWindowedRowsInView(page);
});

test("the mock's chapter audio loads and plays with no studio behind it", async ({ page }) => {
  const errors = collectPageErrors(page);
  const studioRequests: string[] = [];
  page.on("request", (req) => {
    if (new URL(req.url()).pathname.startsWith("/studio/")) studioRequests.push(req.url());
  });
  await stubNetwork(page);
  await page.goto("/books/cold-lantern");
  await expect(page.getByText("chapter_000.wav")).toBeVisible();

  // A chapter WAV that failed to load puts the transport on a timer, and says so.
  const transport = page.locator(".transport");
  await page.waitForLoadState("networkidle");
  await expect(transport.getByText("simulated clock — no audio")).toHaveCount(0);

  await transport.getByRole("button", { name: "Play" }).click();
  await expect(transport.getByText(/^0:0[1-9] \/ 0:28$/)).toBeVisible();
  await expect(transport.getByText("simulated clock — no audio")).toHaveCount(0);

  expect(studioRequests).toEqual([]);
  expect(errors).toEqual([]);
});
