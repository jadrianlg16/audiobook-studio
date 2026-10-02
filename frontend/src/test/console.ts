/**
 * What the test setup saw on console.error during the current test.
 *
 * setup.ts fails any test that logs an error. A test that provokes one on
 * purpose (an error boundary, a rejected request) calls allowConsoleErrors()
 * and can then assert on `consoleGuard.calls` itself.
 */
export const consoleGuard = {
  calls: [] as unknown[][],
  allowed: false,
  reset() {
    this.calls = [];
    this.allowed = false;
  },
};

export function allowConsoleErrors(): void {
  consoleGuard.allowed = true;
}
