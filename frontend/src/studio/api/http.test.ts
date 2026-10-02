import { describe, expect, it, vi } from "vitest";

import { fetchEngines } from "../../lab/api";
import { ApiError, errorMessage, http, jsonBody } from "./http";

function answer(body: string | null, init: ResponseInit) {
  const fetch = vi.fn(async () => new Response(body, init));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

async function failure(url = "/studio/api/books/x"): Promise<ApiError> {
  try {
    await http(url);
  } catch (e) {
    expect(e).toBeInstanceOf(ApiError);
    return e as ApiError;
  }
  throw new Error("http() resolved on an error status");
}

describe("http()", () => {
  it("parses a JSON body and passes the request through", async () => {
    const fetch = answer('{"slug":"x"}', { status: 200 });
    const init = jsonBody("PATCH", { flag: true });
    await expect(http("/studio/api/books/x", init)).resolves.toEqual({ slug: "x" });
    expect(fetch).toHaveBeenCalledWith("/studio/api/books/x", init);
    expect(init).toEqual({
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: '{"flag":true}',
    });
  });

  it("answers undefined for a 204 or an empty body", async () => {
    answer(null, { status: 204 });
    await expect(http("/studio/api/books/x")).resolves.toBeUndefined();
    answer("", { status: 200 });
    await expect(http("/api/unload/kokoro")).resolves.toBeUndefined();
  });

  it("throws an ApiError carrying the studio's detail, the status and the URL", async () => {
    answer('{"detail":"chapter(s) 2 are not rendered at their current settings"}', {
      status: 409,
      headers: { "content-type": "application/json" },
    });
    const e = await failure("/studio/api/books/x/export");
    expect(e.status).toBe(409);
    expect(e.detail).toBe("chapter(s) 2 are not rendered at their current settings");
    expect(e.message).toBe(e.detail); // what screens showed before is unchanged
    expect(e.url).toBe("/studio/api/books/x/export");
    expect(e.name).toBe("ApiError");
    expect(e).toBeInstanceOf(Error);
  });

  it("flattens FastAPI's own 422 list (the gateway still sends it)", async () => {
    answer(
      JSON.stringify({
        detail: [{ loc: ["body", "text"], msg: "Field required", type: "missing" }],
      }),
      { status: 422 },
    );
    expect((await failure()).detail).toBe("body.text: Field required");
  });

  it("falls back to the text of a non-JSON body, and to the status line", async () => {
    answer("upstream timed out", { status: 504 });
    expect((await failure()).detail).toBe("upstream timed out");

    answer("x".repeat(1000), { status: 500 });
    expect((await failure()).detail).toHaveLength(301);

    answer("<html><body><h1>502 Bad Gateway</h1></body></html>", {
      status: 502,
      statusText: "Bad Gateway",
      headers: { "content-type": "text/html" },
    });
    expect((await failure()).detail).toBe("HTTP 502 Bad Gateway");

    answer(null, { status: 500 });
    expect((await failure()).detail).toBe("HTTP 500");
  });

  it("lets a network failure reject as fetch does", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    await expect(http("/studio/api/books")).rejects.toThrow(TypeError);
  });
});

describe("errorMessage()", () => {
  it("is the message of an Error (an ApiError's detail) and the string of anything else", () => {
    expect(errorMessage(new ApiError(404, "no book 'x'", "/studio/api/books/x"))).toBe(
      "no book 'x'",
    );
    expect(errorMessage(new Error("boom"))).toBe("boom");
    expect(errorMessage("plain")).toBe("plain");
    expect(errorMessage(42)).toBe("42");
  });
});

describe("the lab's gateway client", () => {
  it("goes through http(), so a failure is an ApiError", async () => {
    answer('{"detail":"gateway is starting"}', { status: 503 });
    const e = await fetchEngines().catch((err: unknown) => err);
    expect(e).toBeInstanceOf(ApiError);
    expect(e).toMatchObject({ status: 503, detail: "gateway is starting", url: "/api/engines" });
  });
});
