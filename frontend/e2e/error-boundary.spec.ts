import { expect, test } from "@playwright/test";

import { stubNetwork } from "./support";

const BOOM = "e2e: forced render error";

/**
 * The Shelf screen's module, rewritten so the component throws on render. The
 * suite normally runs the production build, where the Shelf is its own lazy
 * chunk (assets/Shelf-<hash>.js) exporting just `Shelf`; under PW_DEV=1 it is
 * the source module. Either way no test-only code ships in the app.
 */
function throwingShelf(pathname: string, source: string): string | null {
  if (pathname === "/src/studio/routes/Shelf.tsx") {
    const marker = "export function Shelf() {";
    return source.includes(marker)
      ? source.replace(marker, `${marker}\n  throw new Error(${JSON.stringify(BOOM)});`)
      : null;
  }
  const exported = /export\s*\{([^}]*)\}\s*;?\s*$/.exec(source);
  if (!exported || !/\bas Shelf\b/.test(exported[1])) return null;
  const names = exported[1].split(",").map((part) =>
    part
      .trim()
      .split(/\s+as\s+/)
      .pop(),
  );
  const boom = `const __e2eBoom=()=>{throw new Error(${JSON.stringify(BOOM)})};`;
  return (
    source.slice(0, exported.index) +
    boom +
    `export{${names.map((n) => `__e2eBoom as ${n}`).join(",")}};`
  );
}

/**
 * Phase 0's acceptance line, in a real browser: "a thrown render error shows
 * the boundary, not a blank page".
 */
test("a screen that throws while rendering shows the crash panel, not a blank page", async ({
  page,
}) => {
  await stubNetwork(page);
  let patched = false;
  await page.route(
    (url) =>
      url.pathname === "/src/studio/routes/Shelf.tsx" ||
      /^\/assets\/Shelf-[\w-]+\.js$/.test(url.pathname),
    async (route) => {
      const response = await route.fetch();
      const source = await response.text();
      const rewritten = throwingShelf(new URL(route.request().url()).pathname, source);
      patched = rewritten !== null;
      await route.fulfill({ response, body: rewritten ?? source });
    },
  );

  await page.goto("/");

  const panel = page.getByRole("alert", { name: "This screen stopped working" });
  await expect(panel).toBeVisible();
  expect(patched, "the Shelf module was rewritten to throw").toBe(true);
  await expect(panel).toContainText(BOOM);
  await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Back to the shelf" })).toHaveAttribute("href", "/");
  await test.info().attach("crash-panel", {
    body: await page.screenshot(),
    contentType: "image/png",
  });

  // The rest of the app is still alive: another route renders normally.
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await stubNetwork(page);
  await page.goto("/jobs");
  await expect(page.getByRole("heading", { level: 1, name: "Jobs" })).toBeVisible();
});
