/**
 * The accessibility baseline: which routes still have *serious* axe
 * violations. routes.spec.ts fails on any *critical* one; Phase 4 of
 * docs/FRONTEND-AUDIT-2026-09-26.md tightens that to serious, which means the
 * table this writes has to be empty.
 *
 * Each route test drops a small summary JSON in the run's output folder; this
 * file is also the suite's globalTeardown, which folds them into a Markdown
 * table. A normal run writes it to the output folder (test-results/, ignored),
 * so `npm run e2e` never modifies a tracked file; some counts (target-size in
 * particular) vary slightly between machines. `AXE_BASELINE_UPDATE=1 npm run
 * e2e` refreshes the committed e2e/axe-baseline.md instead. It only writes when
 * every route reported, so a filtered run (`--grep`) never leaves a partial one.
 */
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import type AxeBuilder from "@axe-core/playwright";
import type { FullConfig, TestInfo } from "@playwright/test";

import { ROUTE_CASES } from "./routes";
import type { RouteCase } from "./routes";

// axe-core itself is only a transitive dependency; take its types from the
// builder that returns them.
type AxeResults = Awaited<ReturnType<AxeBuilder["analyze"]>>;
type Result = AxeResults["violations"][number];

/** The rule sets the 2026-09-26 audit measured with. */
export const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"];

/**
 * Critical violations that existed when this suite was written, by route
 * name. They are the only ones tolerated; any other critical violation fails
 * the run, and so does an entry here that is no longer found (delete it).
 * Fixing them changes the page's semantics, which is Phase 4's job.
 */
export const KNOWN_CRITICAL: Record<string, string[]> = {
  // The bake-off lab (lab/App.tsx, lab/components/EngineControls.tsx): the passage
  // <textarea>, an engine's speed range and its voice <select> have visible
  // captions that are not tied to the control. In the audit's missing-label
  // findings (lab/App.tsx:442); Phase 4.
  lab: ["label", "select-name"],
};

interface RuleHit {
  id: string;
  help: string;
  nodes: number;
}

interface RouteSummary {
  name: string;
  path: string;
  axeVersion: string;
  counts: Record<"critical" | "serious" | "moderate" | "minor", number>;
  critical: RuleHit[];
  serious: RuleHit[];
}

const summaryDir = (outputDir: string) => path.join(outputDir, "axe");

const hits = (violations: Result[], impact: string): RuleHit[] =>
  violations
    .filter((v) => v.impact === impact)
    .map((v) => ({ id: v.id, help: v.help, nodes: v.nodes.length }))
    .sort((a, b) => a.id.localeCompare(b.id));

/** Called by each route test with that route's full axe results. */
export async function recordAxeSummary(
  testInfo: TestInfo,
  route: RouteCase,
  results: AxeResults,
): Promise<RouteSummary> {
  const counts = { critical: 0, serious: 0, moderate: 0, minor: 0 };
  for (const v of results.violations) {
    if (v.impact && v.impact in counts) counts[v.impact as keyof typeof counts] += 1;
  }
  const summary: RouteSummary = {
    name: route.name,
    path: route.path,
    axeVersion: results.testEngine.version,
    counts,
    critical: hits(results.violations, "critical"),
    serious: hits(results.violations, "serious"),
  };
  const dir = summaryDir(testInfo.project.outputDir);
  await mkdir(dir, { recursive: true });
  await writeFile(path.join(dir, `${route.name}.json`), JSON.stringify(summary, null, 2));
  return summary;
}

function render(summaries: RouteSummary[]): string {
  const byRoute = new Map(summaries.map((s) => [s.name, s]));
  const ordered = ROUTE_CASES.map((r) => byRoute.get(r.name)!);
  const version = ordered[0]?.axeVersion ?? "unknown";

  const rules = new Map<string, { help: string; routes: string[] }>();
  for (const s of ordered) {
    for (const r of s.serious) {
      const entry = rules.get(r.id) ?? { help: r.help, routes: [] };
      entry.routes.push(s.name);
      rules.set(r.id, entry);
    }
  }

  const cell = (list: RuleHit[]) =>
    list.length ? list.map((r) => `\`${r.id}\` (${r.nodes})`).join(", ") : "—";

  const lines = [
    "# Accessibility baseline (axe)",
    "",
    "Written by `AXE_BASELINE_UPDATE=1 npm run e2e` (e2e/axe-baseline.ts) — do not edit by hand.",
    "Mock mode,",
    `Playwright's Desktop Chrome viewport (1280×720), axe-core ${version}, tags`,
    `${AXE_TAGS.map((t) => `\`${t}\``).join(", ")}.`,
    "",
    "The e2e run fails on any **critical** violation not listed under Known",
    "critical below. Phase 4 of docs/FRONTEND-AUDIT-2026-09-26.md tightens it to",
    "**serious**, so the Critical and Serious columns have to reach zero. The",
    "impact columns count violated rules; numbers in brackets are affected nodes.",
    "The full axe results for each route are attached to its test in the",
    "Playwright report (and saved under test-results/).",
    "",
    "| Route | Path | Critical | Serious | Moderate | Minor | Critical + serious rules (nodes) |",
    "|---|---|---:|---:|---:|---:|---|",
    ...ordered.map(
      (s) =>
        `| ${s.name} | \`${s.path}\` | ${s.counts.critical} | ${s.counts.serious} | ` +
        `${s.counts.moderate} | ${s.counts.minor} | ${cell([...s.critical, ...s.serious])} |`,
    ),
    "",
    "## Known critical (tolerated until Phase 4)",
    "",
  ];
  const known = Object.entries(KNOWN_CRITICAL);
  if (known.length === 0) lines.push("None.");
  for (const [route, ids] of known) {
    const s = byRoute.get(route);
    const detail = ids.map((id) => {
      const hit = s?.critical.find((r) => r.id === id);
      return hit ? `\`${id}\` (${hit.nodes}): ${hit.help}` : `\`${id}\``;
    });
    lines.push(`- **${route}**: ${detail.join("; ")}.`);
  }
  lines.push(
    "",
    "The reasons are next to each entry in `KNOWN_CRITICAL` (e2e/axe-baseline.ts).",
    "",
    "## Serious rules",
    "",
  );
  if (rules.size === 0) {
    lines.push("None.");
  } else {
    lines.push("| Rule | What it checks | Routes |", "|---|---|---|");
    for (const [id, { help, routes }] of [...rules].sort(([a], [b]) => a.localeCompare(b))) {
      const where = routes.length === ordered.length ? "all" : routes.join(", ");
      lines.push(`| \`${id}\` | ${help} | ${where} |`);
    }
  }
  lines.push("");
  return lines.join("\n");
}

export default async function writeAxeBaseline(config: FullConfig): Promise<void> {
  const dir = summaryDir(config.projects[0].outputDir);
  let files: string[] = [];
  try {
    files = (await readdir(dir)).filter((f) => f.endsWith(".json"));
  } catch {
    // no route test ran
  }
  const summaries = await Promise.all(
    files.map(async (f) => JSON.parse(await readFile(path.join(dir, f), "utf8")) as RouteSummary),
  );
  const expected = new Set(ROUTE_CASES.map((r) => r.name));
  const complete = summaries.filter((s) => expected.has(s.name)).length === expected.size;
  if (!complete) {
    console.log(
      `axe baseline: ${summaries.length} of ${expected.size} routes reported; ` +
        "baseline left as it was.",
    );
    return;
  }
  const update = process.env.AXE_BASELINE_UPDATE === "1";
  const out = update
    ? path.join(path.dirname(fileURLToPath(import.meta.url)), "axe-baseline.md")
    : path.join(config.projects[0].outputDir, "axe-baseline.md");
  await writeFile(out, render(summaries));
  console.log(`axe baseline: wrote ${path.relative(process.cwd(), out)}`);
}
