import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { beforeEach, describe, expect, it } from "vitest";

import { allowConsoleErrors, consoleGuard } from "../../test/console";
import { describeError, RouteBoundary } from "./RouteErrorBoundary";

/** Which slugs throw while rendering. Tests flip entries to "fix" the bug. */
let broken = new Set<string>();

function Screen() {
  const { slug = "home" } = useParams();
  if (broken.has(slug)) throw new TypeError(`cannot read chapters of ${slug}`);
  return <p>screen for {slug}</p>;
}

/**
 * The same shape as main.tsx: each route's element wrapped in a boundary,
 * plus a link outside the routes standing in for the top bar's navigation.
 */
function renderApp(route: string) {
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={[route]}>
      <Link to="/books/fine">another book</Link>
      <Routes>
        <Route
          path="/"
          element={
            <RouteBoundary>
              <Screen />
            </RouteBoundary>
          }
        />
        <Route
          path="/books/:slug"
          element={
            <RouteBoundary>
              <Screen />
            </RouteBoundary>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
  return { user };
}

beforeEach(() => {
  broken = new Set();
  // React reports every error a boundary catches on console.error; that is
  // the point of these tests, not a failure of them.
  allowConsoleErrors();
});

describe("RouteBoundary", () => {
  it("renders the screen untouched when nothing throws", () => {
    renderApp("/books/cold-lantern");
    expect(screen.getByText("screen for cold-lantern")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(consoleGuard.calls).toEqual([]);
  });

  it("shows what went wrong instead of a blank page", () => {
    broken.add("cold-lantern");
    renderApp("/books/cold-lantern");

    const alert = screen.getByRole("alert", { name: "This screen stopped working" });
    expect(alert).toHaveTextContent("TypeError: cannot read chapters of cold-lantern");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to the shelf" })).toHaveAttribute("href", "/");
    // The link outside the boundary survived: only the screen was replaced.
    expect(screen.getByRole("link", { name: "another book" })).toBeInTheDocument();
  });

  it("recovers on Try again once the screen renders", async () => {
    broken.add("cold-lantern");
    const { user } = renderApp("/books/cold-lantern");
    expect(screen.getByRole("alert")).toBeInTheDocument();

    broken.delete("cold-lantern");
    await user.click(screen.getByRole("button", { name: "Try again" }));

    expect(screen.getByText("screen for cold-lantern")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the error again if Try again throws again", async () => {
    broken.add("cold-lantern");
    const { user } = renderApp("/books/cold-lantern");

    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(screen.getByRole("alert")).toHaveTextContent("cannot read chapters of cold-lantern");
  });

  it("resets when the location changes under the same route", async () => {
    // /books/:slug keeps one element (and one boundary) across slugs, so this
    // is the boundary's own reset, not a remount.
    broken.add("cold-lantern");
    const { user } = renderApp("/books/cold-lantern");
    expect(screen.getByRole("alert")).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "another book" }));
    expect(screen.getByText("screen for fine")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("retries on Back to the shelf even when the shelf is what crashed", async () => {
    broken.add("home");
    const { user } = renderApp("/");
    expect(screen.getByRole("alert")).toBeInTheDocument();

    broken.delete("home");
    await user.click(screen.getByRole("link", { name: "Back to the shelf" }));
    expect(screen.getByText("screen for home")).toBeInTheDocument();
  });
});

describe("describeError", () => {
  it("names the error type unless it is a plain Error", () => {
    expect(describeError(new TypeError("x is undefined"))).toBe("TypeError: x is undefined");
    expect(describeError(new Error("HTTP 500"))).toBe("HTTP 500");
    expect(describeError(new Error(""))).toBe("(no message)");
  });

  it("copes with values that are not errors", () => {
    expect(describeError("a string")).toBe("a string");
    expect(describeError(null)).toBe("null");
    expect(describeError({ code: 7 })).toBe("[object Object]");
  });
});
