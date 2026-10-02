/**
 * Parity: the mock speaks the wire.
 *
 * Every screen test and the whole e2e suite run against the mock, so a mock
 * shaped differently from the studio is a test suite that passes against an API
 * nobody serves (the export row's "ch 1" on every m4b file hid that way). Here
 * every read across every mock book, and the answer to each mutation, is
 * validated against the response schema its *route* declares in
 * docs/studio-openapi.json — OpenAPI 3.1, i.e. JSON Schema 2020-12 — with Ajv.
 *
 * One tightening over the export: objects are closed (no property the schema
 * does not name). The studio serializes pydantic models, which never add keys,
 * so a key the mock invents is a key the UI could start reading and the real
 * service would never send.
 */
import Ajv2020 from "ajv/dist/2020";
import type { ErrorObject, ValidateFunction } from "ajv/dist/2020";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import openapi from "../../../../docs/studio-openapi.json";
import { freshMockApi } from "../../test/renderWithStudio";
import type { StudioApi } from "./client";
import type { JobLogEntry, JobProgress } from "./types";

// ---- the schemas ----------------------------------------------------------------

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
type Doc = { [key: string]: Json };

const REF = "#/components/schemas/";

/** Components become `$defs` of one schema; refs follow; objects are closed. */
function prepare(node: Json): Json {
  if (Array.isArray(node)) return node.map(prepare);
  if (node === null || typeof node !== "object") return node;
  const out: Doc = {};
  for (const [key, value] of Object.entries(node)) {
    out[key] =
      key === "$ref" && typeof value === "string" && value.startsWith(REF)
        ? `studio#/$defs/${value.slice(REF.length)}`
        : key === "properties" && value && typeof value === "object" && !Array.isArray(value)
          ? Object.fromEntries(Object.entries(value).map(([k, v]) => [k, prepare(v)]))
          : prepare(value);
  }
  if (out.type === "object" && out.properties && !("additionalProperties" in out)) {
    out.additionalProperties = false;
  }
  return out;
}

const spec = openapi as unknown as {
  components: { schemas: Doc };
  paths: Record<string, Record<string, { responses: Record<string, Doc> }>>;
};

const ajv = new Ajv2020({ allErrors: true, strict: true });
ajv.addSchema({ $id: "studio", $defs: prepare(spec.components.schemas) });

const compiled = new Map<string, ValidateFunction>();
function compile(key: string, schema: Json): ValidateFunction {
  let fn = compiled.get(key);
  if (!fn) {
    fn = ajv.compile(prepare(schema) as Doc);
    compiled.set(key, fn);
  }
  return fn;
}

type Method = "get" | "post" | "put" | "patch";

/** The JSON success body a route declares (200, 201 or 202). */
function routeSchema(method: Method, path: string): ValidateFunction {
  const op = spec.paths[path]?.[method];
  if (!op) throw new Error(`no ${method.toUpperCase()} ${path} in docs/studio-openapi.json`);
  const ok = op.responses["200"] ?? op.responses["201"] ?? op.responses["202"];
  const schema = (ok?.content as Doc | undefined)?.["application/json"] as Doc | undefined;
  if (!schema?.schema) throw new Error(`${method.toUpperCase()} ${path} declares no JSON body`);
  return compile(`${method} ${path}`, schema.schema);
}

function componentSchema(name: string, drop: string[] = []): ValidateFunction {
  const base = spec.components.schemas[name] as Doc;
  if (!base) throw new Error(`no component ${name}`);
  if (drop.length === 0) return compile(name, { $ref: `${REF}${name}` });
  const props = { ...(base.properties as Doc) };
  for (const key of drop) delete props[key];
  const required = (base.required as string[]).filter((k) => !drop.includes(k));
  return compile(`${name}-${drop.join("-")}`, { ...base, properties: props, required });
}

// ---- checking -------------------------------------------------------------------

const problems: string[] = [];

function describeErrors(errors: ErrorObject[] | null | undefined): string {
  return (errors ?? [])
    .slice(0, 6)
    .map((e) => {
      const extra =
        e.keyword === "additionalProperties"
          ? ` (${String((e.params as { additionalProperty: string }).additionalProperty)})`
          : "";
      return `${e.instancePath || "/"} ${e.message}${extra}`;
    })
    .join("; ");
}

/** Validate what the wire would carry: the value after a JSON round trip. */
function check(label: string, validate: ValidateFunction, value: unknown): void {
  const wire = JSON.parse(JSON.stringify(value)) as unknown;
  if (!validate(wire)) problems.push(`${label}: ${describeErrors(validate.errors)}`);
}

/** Resolve a mock promise, driving the fake clock its delays run on. */
async function settle<T>(promise: Promise<T>): Promise<T> {
  const outcome: { settled: boolean; failed: boolean; value?: T; error?: unknown } = {
    settled: false,
    failed: false,
  };
  promise.then(
    (value) => Object.assign(outcome, { settled: true, value }),
    (error: unknown) => Object.assign(outcome, { settled: true, failed: true, error }),
  );
  for (let i = 0; i < 2000 && !outcome.settled; i++) await vi.advanceTimersByTimeAsync(50);
  if (!outcome.settled) throw new Error("the mock never answered");
  if (outcome.failed) throw outcome.error;
  return outcome.value as T;
}

let api: StudioApi;

beforeEach(async () => {
  vi.useFakeTimers();
  problems.length = 0;
  api = await freshMockApi();
});

afterEach(() => {
  vi.useRealTimers();
});

// ---- the tests ----------------------------------------------------------------------

describe("the mock speaks the wire (docs/studio-openapi.json)", () => {
  it("answers every read, for every book, in the route's declared shape", async () => {
    const books = await settle(api.listBooks());
    check("listBooks", routeSchema("get", "/api/books"), books);
    expect(books.length).toBeGreaterThanOrEqual(4);

    for (const { slug } of books) {
      check(
        `getBook ${slug}`,
        routeSchema("get", "/api/books/{slug}"),
        await settle(api.getBook(slug)),
      );
      check(
        `getScript ${slug}`,
        routeSchema("get", "/api/books/{slug}/script"),
        await settle(api.getScript(slug)),
      );
      check(
        `getCast ${slug}`,
        routeSchema("get", "/api/books/{slug}/cast"),
        await settle(api.getCast(slug)),
      );
      check(
        `getLexicon ${slug}`,
        routeSchema("get", "/api/books/{slug}/lexicon"),
        await settle(api.getLexicon(slug)),
      );
      check(
        `listVersions ${slug}`,
        routeSchema("get", "/api/books/{slug}/versions"),
        await settle(api.listVersions(slug)),
      );
      check(
        `listExports ${slug}`,
        routeSchema("get", "/api/books/{slug}/exports"),
        await settle(api.listExports(slug)),
      );
      const passes = await settle(api.listPasses(slug));
      check(`listPasses ${slug}`, routeSchema("get", "/api/books/{slug}/passes"), passes);
      for (const p of passes) {
        check(
          `getPass ${slug}/${p.id}`,
          routeSchema("get", "/api/books/{slug}/passes/{pass_id}"),
          await settle(api.getPass(slug, p.id)),
        );
      }
    }

    const jobs = await settle(api.listJobs());
    check("listJobs", routeSchema("get", "/api/jobs"), jobs);
    for (const j of jobs) {
      check(
        `getJob ${j.id}`,
        routeSchema("get", "/api/jobs/{job_id}"),
        await settle(api.getJob(j.id)),
      );
    }
    check("getSettings", routeSchema("get", "/api/settings"), await settle(api.getSettings()));
    check("health", routeSchema("get", "/api/health"), await settle(api.health()));
    // listEngines is Settings.engines (v1.1), not a route of its own.
    const engine = componentSchema("EngineOption");
    for (const e of await settle(api.listEngines())) check(`listEngines ${e.id}`, engine, e);

    expect(problems).toEqual([]);
  });

  it("answers every mutation in the route's declared shape", async () => {
    const slug = "cold-lantern";
    const script = await settle(api.getScript(slug));
    const breeze = script.lines.find((l) => l.render.engine === "breeze")!;
    const kokoro = script.lines.find((l) => l.render.engine === "kokoro")!;
    const patchLine = routeSchema("patch", "/api/books/{slug}/lines/{line_id}");

    check(
      "patchLine note",
      patchLine,
      await settle(api.patchLine(slug, breeze.id, { instruction: "Hushed." })),
    );
    check(
      "patchLine clear",
      patchLine,
      await settle(api.patchLine(slug, breeze.id, { instruction: null })),
    );
    check(
      "patchLine QC",
      patchLine,
      await settle(api.patchLine(slug, kokoro.id, { flag: true, note: "n" })),
    );
    check(
      "patchLine text",
      patchLine,
      await settle(api.patchLine(slug, kokoro.id, { text: "New words." })),
    );
    check(
      "patchLine engine",
      patchLine,
      await settle(api.patchLine(slug, kokoro.id, { engine: "breeze" })),
    );
    check(
      "patchLine new speaker",
      patchLine,
      await settle(api.patchLine(slug, breeze.id, { speaker: "Harbour master" })),
    );
    check(
      "patchChapter",
      routeSchema("patch", "/api/books/{slug}/chapters/{index}"),
      await settle(api.patchChapter(slug, 0, { title: "Opening", qc_status: "approved" })),
    );
    check(
      "renderLine",
      routeSchema("post", "/api/books/{slug}/lines/{line_id}/render"),
      await settle(api.renderLine(slug, kokoro.id)),
    );
    const cast = script.cast.find((c) => c.character === "Narrator")!;
    check(
      "patchCast",
      routeSchema("patch", "/api/books/{slug}/cast"),
      await settle(api.patchCast(slug, [{ ...cast, voice_id: "af_bella" }])),
    );
    check(
      "mergeCast",
      routeSchema("post", "/api/books/{slug}/cast/merge"),
      await settle(api.mergeCast(slug, "Unknown", "Marcus")),
    );
    check(
      "putLexicon",
      routeSchema("put", "/api/books/{slug}/lexicon"),
      await settle(api.putLexicon(slug, { Marcus: "MAR-kus" })),
    );
    check(
      "restoreVersion",
      routeSchema("post", "/api/books/{slug}/versions/{n}/restore"),
      await settle(api.restoreVersion(slug, 3)),
    );

    const passId = (await settle(api.listPasses(slug)))[0].id;
    const pass = await settle(api.getPass(slug, passId));
    const decisions = pass.scenes.map((s, i) => ({
      scene_id: s.scene_id,
      decision: (i === 0 ? "edited" : i === 1 ? "rejected" : "accepted") as
        "edited" | "rejected" | "accepted",
      ...(i === 0 ? { instruction: "Close in." } : {}),
    }));
    check(
      "decide",
      routeSchema("post", "/api/books/{slug}/passes/{pass_id}/decide"),
      await settle(api.decide(slug, passId, { decisions })),
    );
    check(
      "applyPass",
      routeSchema("post", "/api/books/{slug}/passes/{pass_id}/apply"),
      await settle(api.applyPass(slug, passId)),
    );

    const job = routeSchema("post", "/api/books/{slug}/render");
    const rendered = await settle(api.render(slug, { scope: "stale" }));
    check("render", job, rendered);
    check(
      "pauseJob",
      routeSchema("post", "/api/jobs/{job_id}/pause"),
      await settle(api.pauseJob(rendered.id)),
    );
    check(
      "resumeJob",
      routeSchema("post", "/api/jobs/{job_id}/resume"),
      await settle(api.resumeJob(rendered.id)),
    );
    check(
      "cancelJob",
      routeSchema("post", "/api/jobs/{job_id}/cancel"),
      await settle(api.cancelJob(rendered.id)),
    );
    check(
      "retryPass",
      routeSchema("post", "/api/books/{slug}/passes/{pass_id}/retry"),
      await settle(api.retryPass(slug, passId, [pass.scenes[0].scene_id])),
    );
    check(
      "direct",
      routeSchema("post", "/api/books/{slug}/direct"),
      await settle(api.direct(slug, { scope: "all" })),
    );

    // fog-signals has every included chapter fresh, so it can export.
    for (const format of ["m4b", "acx"] as const) {
      const started = await settle(api.exportBook("fog-signals", { format }));
      check(`exportBook ${format}`, routeSchema("post", "/api/books/{slug}/export"), started);
      await vi.advanceTimersByTimeAsync(60_000);
      check(
        `export job ${format}`,
        routeSchema("get", "/api/jobs/{job_id}"),
        await settle(api.getJob(started.id)),
      );
    }
    check(
      "listExports after export",
      routeSchema("get", "/api/books/{slug}/exports"),
      await settle(api.listExports("fog-signals")),
    );

    check(
      "patchSettings",
      routeSchema("patch", "/api/settings"),
      await settle(
        api.patchSettings({
          casting_mode: "all_expressive",
          export: { format: "mp3", loudness: "none", sample_rate: 44100 },
          director: { provider: "ollama", model: "gemma3:12b" },
        }),
      ),
    );
    const created = await settle(
      api.createBook({
        title: "Parity",
        author: "",
        text: "One.\n\nTwo.\n\nThree.",
        book_type: "auto",
        voices: "auto",
        casting_mode: "all_expressive",
      }),
    );
    check("createBook", routeSchema("post", "/api/books"), created);
    const form = new FormData();
    form.append("file", new File(["Up one.\n\nUp two."], "up.txt", { type: "text/plain" }));
    check(
      "uploadBook",
      routeSchema("post", "/api/books/upload"),
      await settle(api.uploadBook(form)),
    );
    check(
      "getScript new book",
      routeSchema("get", "/api/books/{slug}/script"),
      await settle(api.getScript(created.slug)),
    );

    expect(problems).toEqual([]);
  });

  it("streams job frames shaped like the SSE events", async () => {
    const progress = componentSchema("Job", ["log"]);
    const done = componentSchema("Job");
    const log = componentSchema("JobLogEntry");
    const frames: { kind: string; data: JobProgress | JobLogEntry }[] = [];

    const started = await settle(api.render("harbour-road", { scope: "all" }));
    let finished = false;
    const stop = api.subscribeJob(started.id, {
      onProgress: (data) => frames.push({ kind: "progress", data }),
      onLog: (data) => frames.push({ kind: "log", data }),
      onDone: (data) => {
        frames.push({ kind: "done", data });
        finished = true;
      },
    });
    for (let i = 0; i < 400 && !finished; i++) await vi.advanceTimersByTimeAsync(600);
    stop();

    expect(finished).toBe(true);
    expect(frames[0]?.kind).toBe("progress"); // the contract: a progress first, on connect
    for (const [i, f] of frames.entries()) {
      check(
        `${f.kind} frame ${i}`,
        f.kind === "log" ? log : f.kind === "done" ? done : progress,
        f.data,
      );
    }
    expect(problems).toEqual([]);
  });
});
