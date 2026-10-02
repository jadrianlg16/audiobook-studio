import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderWithStudio } from "../../test/renderWithStudio";
import { ExportsPanel } from "./DeskPanels";

describe("ExportsPanel", () => {
  // The service sends the ACX columns on every row, as null on a non-ACX one
  // (STUDIO-API.md v1.4). The row used to test `!== undefined`, so a live m4b
  // export read "ch 1" and a null duration; the old mock omitted the keys and hid it.
  it("labels chapters only on the rows that have one", async () => {
    renderWithStudio(<ExportsPanel slug="cold-lantern" onClose={() => undefined} />);

    const m4b = (await screen.findByText("the-cold-lantern.m4b")).closest(".panelrow");
    expect(m4b).not.toBeNull();
    expect(within(m4b as HTMLElement).queryByText(/^ch \d+$/)).toBeNull();

    const acx = screen.getByText("the-cold-lantern-000.mp3").closest(".panelrow");
    expect(within(acx as HTMLElement).getByText("ch 1")).toBeInTheDocument();
  });
});
