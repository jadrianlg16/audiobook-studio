import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * resolveMockMode() reads import.meta.env when client.ts is evaluated
 * (MOCK_IS_STICKY), so every case stubs the build env first and then imports a
 * fresh copy of the module.
 */
async function loadResolver(env: { dev: boolean; viteMock?: "1" }) {
  vi.stubEnv("DEV", env.dev);
  vi.stubEnv("PROD", !env.dev);
  vi.stubEnv("VITE_MOCK", env.viteMock ?? "");
  vi.resetModules();
  const { resolveMockMode } = await import("./client");
  return resolveMockMode;
}

/** One page load: the URL it is loaded at, then the mode it resolves. */
function visit(resolve: () => boolean, search: string): boolean {
  window.history.replaceState(null, "", `/${search}`);
  return resolve();
}

const stored = () => window.localStorage.getItem("studioMock");

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  window.history.replaceState(null, "", "/");
});

describe("resolveMockMode", () => {
  it("is always on in a VITE_MOCK=1 build, whatever the URL says", async () => {
    const resolve = await loadResolver({ dev: false, viteMock: "1" });
    expect(visit(resolve, "")).toBe(true);
    expect(visit(resolve, "?mock=0")).toBe(true);
  });

  it("is off by default", async () => {
    expect(visit(await loadResolver({ dev: true }), "")).toBe(false);
    expect(visit(await loadResolver({ dev: false }), "")).toBe(false);
  });

  describe("in a dev build", () => {
    it("remembers ?mock=1 across loads until ?mock=0", async () => {
      const resolve = await loadResolver({ dev: true });

      expect(visit(resolve, "?mock=1")).toBe(true);
      expect(stored()).toBe("1");
      expect(visit(resolve, "")).toBe(true);

      expect(visit(resolve, "?mock=0")).toBe(false);
      expect(stored()).toBe("0");
      expect(visit(resolve, "")).toBe(false);
    });

    it("ignores a mock value it does not understand", async () => {
      const resolve = await loadResolver({ dev: true });
      window.localStorage.setItem("studioMock", "1");
      expect(visit(resolve, "?mock=yes")).toBe(true); // falls through to the stored choice
      expect(stored()).toBe("1");
    });
  });

  describe("in a production build", () => {
    it("honours ?mock=1 for exactly one page load and never writes storage", async () => {
      const resolve = await loadResolver({ dev: false });

      expect(visit(resolve, "?mock=1")).toBe(true);
      expect(stored()).toBeNull();
      expect(visit(resolve, "")).toBe(false);
    });

    it("is not latched by a value a dev build left in storage", async () => {
      window.localStorage.setItem("studioMock", "1");
      const resolve = await loadResolver({ dev: false });
      expect(visit(resolve, "")).toBe(false);
    });
  });

  it("falls back to the real API when storage is unavailable", async () => {
    const resolve = await loadResolver({ dev: true });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("storage disabled", "SecurityError");
    });
    expect(visit(resolve, "")).toBe(false);
  });
});
