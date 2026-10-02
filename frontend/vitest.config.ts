import { defineConfig, mergeConfig } from "vitest/config";

import viteConfig from "./vite.config";

// Unit and component tests. The Playwright suite in e2e/ is separate
// (`npm run e2e`) and never collected here.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      include: ["src/**/*.test.{ts,tsx}"],
      setupFiles: ["./src/test/setup.ts"],
      // Every spy and env stub is undone after each test, so no test leans on
      // another's leftovers.
      restoreMocks: true,
      unstubEnvs: true,
      unstubGlobals: true,
    },
  }),
);
