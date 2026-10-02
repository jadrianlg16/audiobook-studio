import { screen, waitFor, within } from "@testing-library/react";
import { useParams } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { freshMockApi, renderWithStudio } from "../../test/renderWithStudio";
import { Shelf } from "./Shelf";

const TITLES = [
  "The Cold Lantern",
  "The Harbour Road",
  "A Short History of Fog Signals",
  "The Long Watch",
];

/** Stands in for the Desk: proves where a link went, nothing more. */
function DeskProbe() {
  const { slug } = useParams();
  return <p>desk for {slug}</p>;
}

/** The shelf is fully drawn once the books and the health pill have answered. */
async function renderShelf() {
  const view = renderWithStudio(<Shelf />, {
    api: await freshMockApi(),
    extraRoutes: [{ path: "/books/:slug", element: <DeskProbe /> }],
  });
  await screen.findByText("4 on the shelf");
  await screen.findByText("studio ok");
  return view;
}

const rowOf = (title: string) => screen.getByRole("link", { name: title }).closest("tr")!;

describe("Shelf, against the mock API", () => {
  it("lists every seeded book as a link to its desk", async () => {
    await renderShelf();

    expect(screen.getByRole("heading", { level: 1, name: "Books" })).toBeInTheDocument();
    expect(screen.queryByText("reading the shelf…")).not.toBeInTheDocument();
    for (const title of TITLES) {
      expect(screen.getByRole("link", { name: title })).toBeInTheDocument();
    }
    expect(screen.getByRole("link", { name: "The Cold Lantern" })).toHaveAttribute(
      "href",
      "/books/cold-lantern",
    );
  });

  it("shows each book's facts in its own row", async () => {
    await renderShelf();

    const fog = within(rowOf("A Short History of Fog Signals"));
    expect(fog.getByText("Ada Voss")).toBeInTheDocument();
    expect(fog.getByText("Non-fiction")).toBeInTheDocument();
    // rendered while STUDIO_OFFLINE=1, so it must say the audio is not speech
    expect(fog.getByText("test audio · fake synth")).toBeInTheDocument();

    const lantern = within(rowOf("The Cold Lantern"));
    expect(lantern.queryByText("test audio · fake synth")).not.toBeInTheDocument();
    expect(lantern.getByRole("link", { name: /job_ren_002/ })).toHaveAttribute(
      "href",
      "/jobs/job_ren_002",
    );

    // The harbour road is mid-render: its job actions are disabled.
    const harbour = within(rowOf("The Harbour Road"));
    expect(harbour.getByText("rendering")).toBeInTheDocument();
    expect(harbour.getByRole("button", { name: "Direct" })).toBeDisabled();
  });

  it("reports the mock studio as offline with a fake director", async () => {
    await renderShelf();
    expect(screen.getByText("offline · fake synth")).toBeInTheDocument();
    expect(screen.getByText("fake director")).toBeInTheDocument();
  });

  it("deletes a book only after the second, explicit confirmation", async () => {
    const { user } = await renderShelf();
    const row = within(rowOf("The Long Watch"));

    await user.click(row.getByRole("button", { name: "Delete" }));
    const confirm = row.getByRole("group", { name: /Delete .The Long Watch.\?/ });
    expect(screen.getByRole("link", { name: "The Long Watch" })).toBeInTheDocument();

    await user.click(within(confirm).getByRole("button", { name: "Delete for good" }));
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "The Long Watch" })).not.toBeInTheDocument(),
    );
    expect(await screen.findByText("3 on the shelf")).toBeInTheDocument();
  });

  it("backs out of a delete with Cancel", async () => {
    const { user } = await renderShelf();
    const row = within(rowOf("The Long Watch"));

    await user.click(row.getByRole("button", { name: "Delete" }));
    await user.click(row.getByRole("button", { name: "Cancel" }));

    expect(row.getByRole("button", { name: "Delete" })).toBeInTheDocument();
    expect(screen.getByText("4 on the shelf")).toBeInTheDocument();
  });

  it("opens a book's desk from its title", async () => {
    const { user } = await renderShelf();
    await user.click(screen.getByRole("link", { name: "The Harbour Road" }));
    expect(await screen.findByText("desk for harbour-road")).toBeInTheDocument();
  });
});
