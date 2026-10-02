/**
 * Render a studio screen the way main.tsx mounts it: inside the API context
 * and a router, against the real mock API (the same one `VITE_MOCK=1` serves).
 *
 *   const { user } = renderWithStudio(<Desk />, {
 *     route: "/books/cold-lantern",
 *     path: "/books/:slug",
 *   });
 *
 * The mock keeps its books in module state, so a test that mutates them (a
 * delete, an edit) should pass `api: await freshMockApi()` to start from the
 * seeded fixtures regardless of what ran before it in the same file.
 */
import { render } from "@testing-library/react";
import type { RenderResult } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { vi } from "vitest";

import { ApiContext } from "../studio/api/client";
import type { StudioApi } from "../studio/api/client";
import { createMockApi } from "../studio/api/mock";

export interface StudioRenderOptions {
  /** The URL the router starts on. */
  route?: string;
  /** The route pattern the element is mounted at, so useParams() works. */
  path?: string;
  /** Defaults to the shared mock; see freshMockApi() for an isolated one. */
  api?: StudioApi;
  /** Other routes to mount next to the element, e.g. a navigation target. */
  extraRoutes?: { path: string; element: ReactElement }[];
}

export type StudioRenderResult = RenderResult & {
  user: ReturnType<typeof userEvent.setup>;
  api: StudioApi;
};

export function renderWithStudio(
  ui: ReactElement,
  { route = "/", path = "*", api = createMockApi(), extraRoutes = [] }: StudioRenderOptions = {},
): StudioRenderResult {
  const user = userEvent.setup();
  const result = render(
    <ApiContext.Provider value={api}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route path={path} element={ui} />
          {extraRoutes.map((r) => (
            <Route key={r.path} path={r.path} element={r.element} />
          ))}
        </Routes>
      </MemoryRouter>
    </ApiContext.Provider>,
  );
  return { ...result, user, api };
}

/**
 * A mock API over freshly seeded fixtures: the mock module is re-evaluated, so
 * books deleted or edited by an earlier test are back.
 */
export async function freshMockApi(): Promise<StudioApi> {
  vi.resetModules();
  const mock = await import("../studio/api/mock");
  return mock.createMockApi();
}
