import { screen } from "@testing-library/react";
import { useParams } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { freshMockApi, renderWithStudio } from "../../test/renderWithStudio";
import { NewBook } from "./NewBook";

/** Stands in for the Desk: proves where the form went, nothing more. */
function DeskProbe() {
  const { slug } = useParams();
  return <p>desk for {slug}</p>;
}

async function renderNewBook() {
  const api = await freshMockApi();
  const uploadBook = vi.spyOn(api, "uploadBook");
  const createBook = vi.spyOn(api, "createBook");
  const view = renderWithStudio(<NewBook />, {
    api,
    route: "/books/new",
    path: "/books/new",
    extraRoutes: [{ path: "/books/:slug", element: <DeskProbe /> }],
  });
  // The casting default names the studio's setting once settings have loaded.
  await screen.findByRole("option", { name: /^default — Dialogue expressive/ });
  return { ...view, uploadBook, createBook };
}

async function uploadWith(
  user: Awaited<ReturnType<typeof renderNewBook>>["user"],
  castingMode: string | null,
) {
  await user.click(screen.getByRole("tab", { name: /Upload a file/ }));
  await user.upload(
    screen.getByLabelText("File"),
    new File(["The tide came in twice that day."], "tide-tables.txt", { type: "text/plain" }),
  );
  if (castingMode) await user.selectOptions(screen.getByLabelText("Casting mode"), castingMode);
  await user.click(screen.getByRole("button", { name: "Create book" }));
  await screen.findByText("desk for tide-tables");
}

describe("NewBook casting mode", () => {
  it("sends the chosen casting mode with an uploaded file", async () => {
    const { user, uploadBook } = await renderNewBook();
    await uploadWith(user, "all_expressive");

    expect(uploadBook).toHaveBeenCalledTimes(1);
    const form = uploadBook.mock.calls[0][0];
    expect(form.get("casting_mode")).toBe("all_expressive");
    // The mock built the book that way: the narrator is on the expressive engine.
    const book = await uploadBook.mock.results[0].value;
    expect(Object.keys(book.engines)).toEqual(["breeze"]);
    expect(book.directable.lines).toBe(book.utterances);
  });

  it("leaves casting_mode out of an upload that keeps the default", async () => {
    const { user, uploadBook } = await renderNewBook();
    await uploadWith(user, null);

    const form = uploadBook.mock.calls[0][0];
    expect(form.has("casting_mode")).toBe(false);
    const book = await uploadBook.mock.results[0].value;
    expect(Object.keys(book.engines)).toEqual(["kokoro"]);
  });

  it("a pasted book's casting mode reaches the mock's book too", async () => {
    const { user, createBook } = await renderNewBook();
    await user.type(screen.getByLabelText("Title"), "Tide Tables");
    await user.type(screen.getByLabelText("Text"), "The tide came in twice that day.");
    await user.selectOptions(screen.getByLabelText("Casting mode"), "all_expressive");
    await user.click(screen.getByRole("button", { name: "Create book" }));
    await screen.findByText("desk for tide-tables");

    expect(createBook).toHaveBeenCalledWith(
      expect.objectContaining({ casting_mode: "all_expressive" }),
    );
    const book = await createBook.mock.results[0].value;
    expect(Object.keys(book.engines)).toEqual(["breeze"]);
  });
});
