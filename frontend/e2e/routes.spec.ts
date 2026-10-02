import { writeFile } from "node:fs/promises";

import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { AXE_TAGS, KNOWN_CRITICAL, recordAxeSummary } from "./axe-baseline";
import { ROUTE_CASES } from "./routes";
import { collectPageErrors, LOADING_TEXT, stubNetwork } from "./support";

/**
 * Every route, once, in mock mode: it renders its screen (not a spinner, not
 * the crash panel, not a redirect), logs no console error and throws nothing,
 * and has no critical axe violation beyond the listed known ones. Serious
 * ones are recorded in e2e/axe-baseline.md for Phase 4.
 */
for (const route of ROUTE_CASES) {
  test(`${route.name}: ${route.path}`, async ({ page }, testInfo) => {
    const errors = collectPageErrors(page);
    await stubNetwork(page);

    await page.goto(route.path);

    const isLab = route.path === "/lab";
    await expect(page.locator(isLab ? ".labnote" : "header.topbar")).toBeVisible();
    await expect(page.getByText(route.ready).first()).toBeVisible();
    await expect(page.getByText(LOADING_TEXT)).toHaveCount(0);
    await expect(page.getByRole("alert", { name: "This screen stopped working" })).toHaveCount(0);
    // The catch-all route redirects unknown paths to the shelf; this one was known.
    expect(new URL(page.url()).pathname).toBe(route.path);
    await page.waitForLoadState("networkidle");

    const results = await new AxeBuilder({ page }).withTags(AXE_TAGS).analyze();
    const resultsPath = testInfo.outputPath(`axe-${route.name}.json`);
    await writeFile(resultsPath, JSON.stringify(results, null, 2));
    await testInfo.attach(`axe-${route.name}.json`, {
      path: resultsPath,
      contentType: "application/json",
    });
    const summary = await recordAxeSummary(testInfo, route, results);

    expect.soft(errors, "console errors and uncaught exceptions").toEqual([]);

    const known = KNOWN_CRITICAL[route.name] ?? [];
    expect
      .soft(
        summary.critical
          .filter((r) => !known.includes(r.id))
          .map((r) => `${r.id} (${r.nodes} nodes): ${r.help}`),
        "critical axe violations",
      )
      .toEqual([]);
    // Like an unused eslint-disable: a fixed violation must come off the list.
    expect(
      known.filter((id) => !summary.critical.some((r) => r.id === id)),
      "fixed, so remove from KNOWN_CRITICAL in e2e/axe-baseline.ts",
    ).toEqual([]);
  });
}
