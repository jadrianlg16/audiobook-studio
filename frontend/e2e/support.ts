import type { Page } from "@playwright/test";

import type { EngineStatus } from "../src/lab/types";

/**
 * The e2e suite runs the app in mock mode (VITE_MOCK=1): every studio API call
 * is answered in the browser by src/studio/api/mock.ts, and its chapter and
 * line audio are silent WAVs made in the page. Two things still leave the
 * page, and are answered here so a run needs no network and no backend:
 *
 * - Google Fonts (index.html). Every font rule names a local fallback.
 * - The lab's gateway calls (/api/*): the bake-off lab has no mock of its own.
 *   Only the two reads it makes on load are answered.
 *
 * Any other /api or /studio request gets a 503 that names this file, so a new
 * call shows up as a failure (a console error) instead of a silent pass.
 */
export async function stubNetwork(page: Page): Promise<void> {
  await page.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, (route) =>
    route.fulfill({ status: 200, contentType: "text/css", body: "" }),
  );

  const notStubbed = (pathname: string) => ({
    status: 503,
    json: { detail: `e2e: ${pathname} is not stubbed (frontend/e2e/support.ts)` },
  });

  await page.route(
    (url) => url.pathname.startsWith("/studio/"),
    (route) => route.fulfill(notStubbed(new URL(route.request().url()).pathname)),
  );

  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    (route) => {
      const { pathname } = new URL(route.request().url());
      if (pathname === "/api/engines") return route.fulfill({ json: GATEWAY_ENGINES });
      if (pathname === "/api/director/defaults") {
        return route.fulfill({ json: { provider: "ollama", model: "gemma3:12b" } });
      }
      return route.fulfill(notStubbed(pathname));
    },
  );
}

/** Console errors and uncaught exceptions, as readable lines. */
export function collectPageErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const { url, lineNumber } = msg.location();
    errors.push(`console.error: ${msg.text()}${url ? ` (${url}:${lineNumber})` : ""}`);
  });
  page.on("pageerror", (err) => errors.push(`uncaught ${err.name}: ${err.message}`));
  return errors;
}

/** Text the Spinner component shows while a screen is loading. */
export const LOADING_TEXT = /^(loading|reading|opening|attaching|starting)\b.*…$/;

/** One engine up with its info, one down: both paths of the lab's engine cards. */
const GATEWAY_ENGINES: EngineStatus[] = [
  {
    id: "kokoro",
    url: "http://kokoro:8000",
    available: true,
    loaded: true,
    error: null,
    info: {
      id: "kokoro",
      name: "Kokoro",
      description: "Small, fast, CPU-friendly English TTS.",
      license: "Apache-2.0",
      homepage: "https://huggingface.co/hexgrad/Kokoro-82M",
      sample_rate: 24000,
      capabilities: {
        cloning: false,
        emotion_scalar: false,
        emotion_tags: false,
        emotion_reference: false,
        instruction: false,
        multi_speaker: true,
        streaming: false,
        requires_gpu: false,
        languages: ["en"],
      },
      voices: [
        { id: "af_heart", label: "Heart (US female)", language: "en" },
        { id: "bf_emma", label: "Emma (UK female)", language: "en" },
      ],
      emotion_labels: [],
      audio_tags: [],
    },
  },
  {
    id: "breeze",
    url: "http://breeze:8000",
    available: false,
    loaded: false,
    error: "connection refused",
    info: null,
  },
];
