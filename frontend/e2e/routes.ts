/**
 * Ids and titles the mock API seeds (src/studio/api/mock.ts, MOCK_IDS). Plain
 * data with no imports, so both the Playwright suite (Node) and a Vitest check
 * (src/studio/api/mock.test.ts, which fails if these drift) can read it.
 */
export const MOCK_ROUTES = {
  books: [
    { slug: "cold-lantern", title: "The Cold Lantern" },
    { slug: "harbour-road", title: "The Harbour Road" },
    { slug: "fog-signals", title: "A Short History of Fog Signals" },
    { slug: "long-watch", title: "The Long Watch" },
  ],
  /** Seeded in this order: live, done, interrupted. */
  jobs: [
    { id: "job_ren_001", book: "harbour-road" },
    { id: "job_ren_002", book: "cold-lantern" },
    { id: "job_ren_003", book: "fog-signals" },
  ],
  pass: { slug: "cold-lantern", passId: "pass_9f2ca41", scenes: 42 },
} as const;

export interface RouteCase {
  /** Short, file-name-safe label used in test titles and artifact names. */
  name: string;
  path: string;
  /** Text that is only on screen once the route has loaded its data. */
  ready: string;
}

const titleOf = (slug: string) => MOCK_ROUTES.books.find((b) => b.slug === slug)?.title ?? slug;

/** Every screen the router serves, with real mock ids. */
export const ROUTE_CASES: RouteCase[] = [
  { name: "shelf", path: "/", ready: "The Cold Lantern" },
  { name: "new-book", path: "/books/new", ready: "Paste the text or upload a file" },
  ...MOCK_ROUTES.books.flatMap(({ slug, title }) => [
    { name: `desk-${slug}`, path: `/books/${slug}`, ready: title },
    // every seeded book casts a Narrator
    { name: `cast-${slug}`, path: `/books/${slug}/cast`, ready: "Narrator" },
    // the mock's global lexicon entry, shown on every book
    { name: `lexicon-${slug}`, path: `/books/${slug}/lexicon`, ready: "ASH-grove" },
  ]),
  {
    name: `review-${MOCK_ROUTES.pass.passId}`,
    path: `/books/${MOCK_ROUTES.pass.slug}/review/${MOCK_ROUTES.pass.passId}`,
    ready: `${MOCK_ROUTES.pass.scenes} scenes`,
  },
  { name: "jobs", path: "/jobs", ready: MOCK_ROUTES.jobs[0].id },
  ...MOCK_ROUTES.jobs.map(({ id, book }) => ({
    name: `job-${id}`,
    path: `/jobs/${id}`,
    ready: titleOf(book),
  })),
  { name: "settings", path: "/settings", ready: "http://gateway:8000" },
  // the stubbed gateway's engine (e2e/support.ts)
  { name: "lab", path: "/lab", ready: "Kokoro" },
];
