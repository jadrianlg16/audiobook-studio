import { describe, expect, it, vi } from "vitest";

import { MOCK_ROUTES } from "../../../e2e/routes";
import { freshMockApi } from "../../test/renderWithStudio";
import { createMockApi, MOCK_IDS } from "./mock";

/**
 * The Playwright suite visits the mock by URL and cannot import this module
 * (it runs in Node; the mock needs a window). These checks keep the ids and
 * titles it hardcodes in e2e/routes.ts honest: a renamed fixture fails here,
 * with a clear message, instead of as a timeout in a browser.
 */
describe("the e2e route list matches the mock's fixtures", () => {
  it("names the mock's seeded jobs and pass", async () => {
    expect(MOCK_ROUTES.jobs.map((j) => j.id)).toEqual([
      MOCK_IDS.liveJob,
      MOCK_IDS.doneJob,
      MOCK_IDS.interruptedJob,
    ]);
    expect(MOCK_ROUTES.pass.passId).toBe(MOCK_IDS.pass);

    // listJobs, not getJob: opening the live job would start its clock.
    const bookOf = new Map((await createMockApi().listJobs()).map((j) => [j.id, j.book]));
    for (const { id, book } of MOCK_ROUTES.jobs) expect(bookOf.get(id)).toBe(book);
  });

  it("names every seeded book with its title", async () => {
    const api = createMockApi();
    const books = (await api.listBooks()).map((b) => ({ slug: b.slug, title: b.title }));
    const bySlug = (a: { slug: string }, b: { slug: string }) => a.slug.localeCompare(b.slug);
    expect([...MOCK_ROUTES.books].sort(bySlug)).toEqual(books.sort(bySlug));
  });

  it("points the review route at a pass of that book with that many scenes", async () => {
    const api = createMockApi();
    const pass = await api.getPass(MOCK_ROUTES.pass.slug, MOCK_ROUTES.pass.passId);
    expect(pass.counts.scenes).toBe(MOCK_ROUTES.pass.scenes);
  });
});

/**
 * Audio in mock mode must play with no studio behind the page: a URL under
 * /studio/ is a request to a server that is not there (a 500 through the dev
 * proxy, a console error on every desk that has a rendered chapter).
 */
describe("the mock's audio needs no server", () => {
  /** Record the Blob behind every object URL the mock makes. */
  function captureBlobs() {
    const blobs = new Map<string, Blob>();
    vi.spyOn(URL, "createObjectURL").mockImplementation((obj: Blob | MediaSource) => {
      const url = `blob:test/${blobs.size + 1}`;
      blobs.set(url, obj as Blob);
      return url;
    });
    return blobs;
  }

  async function readWav(blob: Blob | undefined) {
    expect(blob, "the URL is backed by a file the mock made").toBeDefined();
    const buf = await new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as ArrayBuffer);
      reader.onerror = () => reject(reader.error);
      reader.readAsArrayBuffer(blob!);
    });
    const view = new DataView(buf);
    const tag = (at: number) => String.fromCharCode(...new Uint8Array(buf, at, 4));
    expect([tag(0), tag(8), tag(12), tag(36)]).toEqual(["RIFF", "WAVE", "fmt ", "data"]);
    expect(view.getUint16(20, true), "PCM").toBe(1);
    const rate = view.getUint32(24, true);
    const blockAlign = view.getUint16(32, true);
    const dataBytes = view.getUint32(40, true);
    expect(buf.byteLength).toBe(44 + dataBytes);
    return {
      type: blob!.type,
      rate,
      bytes: buf.byteLength,
      ms: (dataBytes / blockAlign / rate) * 1000,
    };
  }

  it("serves a chapter's audio as a WAV of the chapter's length", async () => {
    const blobs = captureBlobs();
    const api = await freshMockApi();
    const chapter = (await api.getScript("cold-lantern")).chapters[0];
    expect(chapter.audio_file).toBe("chapter_000.wav");

    const url = api.bookFileUrl("cold-lantern", chapter.audio_file!);
    expect(url).toMatch(/^blob:/);
    const wav = await readWav(blobs.get(url));
    expect(wav.type).toBe("audio/wav");
    expect(Math.abs(wav.ms - chapter.duration_ms)).toBeLessThan(1);
    // The desk asks on every render; the same chapter is the same file.
    expect(api.bookFileUrl("cold-lantern", chapter.audio_file!)).toBe(url);
    expect(blobs.size).toBe(1);
  });

  it("keeps the two-hour chapter of the long fixture small", async () => {
    const blobs = captureBlobs();
    const api = await freshMockApi();
    const chapter = (await api.getScript("long-watch")).chapters[0];
    expect(chapter.duration_ms).toBeGreaterThan(2 * 3_600_000 - 60_000);

    const wav = await readWav(blobs.get(api.bookFileUrl("long-watch", chapter.audio_file!)));
    expect(Math.abs(wav.ms - chapter.duration_ms)).toBeLessThan(1);
    expect(wav.rate).toBeGreaterThanOrEqual(3_000); // Chrome decodes nothing slower
    expect(wav.bytes).toBeLessThan(25_000_000);
  });

  it("serves a cached line's take as a WAV of the line's length", async () => {
    const blobs = captureBlobs();
    const api = await freshMockApi();
    const line = (await api.getScript("cold-lantern")).lines.find(
      (l) => l.audio_state === "cached",
    )!;

    const url = api.lineAudioUrl("cold-lantern", line.id);
    expect(url).toMatch(/^blob:/);
    const wav = await readWav(blobs.get(url));
    expect(Math.abs(wav.ms - line.duration_ms!)).toBeLessThan(1);
  });

  it("has no take for a line that is not cached, and still makes no request", async () => {
    const blobs = captureBlobs();
    const api = await freshMockApi();
    const line = (await api.getScript("harbour-road")).lines.find(
      (l) => l.audio_state === "missing",
    )!;

    const url = api.lineAudioUrl("harbour-road", line.id);
    expect(url).not.toMatch(/^(\/|https?:)/);
    expect(blobs.size).toBe(0);
  });

  it("answers a single-line render with a take that plays", async () => {
    const blobs = captureBlobs();
    const api = await freshMockApi();
    const line = (await api.getScript("harbour-road")).lines[0];

    const res = await api.renderLine("harbour-road", line.id);
    expect(res.audio_url).toMatch(/^blob:/);
    const wav = await readWav(blobs.get(res.audio_url));
    expect(Math.abs(wav.ms - res.line.duration_ms!)).toBeLessThan(1);
  });
});
