import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { freshMockApi, renderWithStudio } from "../../test/renderWithStudio";
import type { StudioApi } from "../api/client";
import { Desk } from "./Desk";

/**
 * The Director's Desk against the mock API, on The Cold Lantern's first
 * chapter. Every assertion is about what the user sees or what the studio
 * stored, never about component state.
 */
const SLUG = "cold-lantern";
/** Chapter 1's first line: the narrator, no note. */
const PLAIN_LINE = "The lantern on the harbour wall had not been lit in a year.";
/** Elara's first line in chapter 1, and the note the director gave it. */
const NOTED_LINE = "“The kettle is still warm. I heard the gate.”";
const NOTE = "steady, almost gentle; she has decided something";

async function renderDesk() {
  const api = await freshMockApi();
  const patchLine = vi.spyOn(api, "patchLine");
  const view = renderWithStudio(<Desk />, {
    api,
    route: `/books/${SLUG}`,
    path: "/books/:slug",
  });
  await within(await screen.findByRole("main")).findByText(NOTED_LINE);
  return { ...view, patchLine };
}

/** The script column (the inspector repeats a line's text in its own fields). */
const script = () => within(screen.getByRole("main"));

/** The script row that holds this text. */
const rowOf = (text: string) => script().getByText(text).closest<HTMLElement>("[data-line]")!;

const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

/** The note under a line, shown as a button that opens the row editor. */
const noteButton = (text: string, note: string) =>
  within(rowOf(text)).getByRole("button", { name: new RegExp(`^\\(${escapeRegExp(note)}\\)`) });

/** The newest saved version, as the studio stores it. */
async function newestVersion(api: StudioApi): Promise<number> {
  return Math.max(...(await api.listVersions(SLUG)).map((v) => v.version));
}

/**
 * Wait past the mock's answer time (140 ms), so a request the UI should not
 * have sent would have landed by now.
 */
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 400)));

describe("Desk note editors", () => {
  describe("the row editor", () => {
    it("Esc restores the note, saves nothing and puts focus back on the row", async () => {
      const { user, api, patchLine } = await renderDesk();

      await user.click(noteButton(NOTED_LINE, NOTE));
      const editor = within(rowOf(NOTED_LINE)).getByRole("textbox");
      expect(editor).toHaveFocus();
      await user.clear(editor);
      await user.type(editor, "shouted");
      await user.keyboard("{Escape}");
      await settle();

      expect(patchLine).not.toHaveBeenCalled();
      expect(noteButton(NOTED_LINE, NOTE)).toBeInTheDocument();
      expect(rowOf(NOTED_LINE)).toHaveFocus();
      expect(await newestVersion(api)).toBe(7);
    });

    it("Ctrl+Enter saves the draft once and puts focus back on the row", async () => {
      const { user, api, patchLine } = await renderDesk();

      await user.click(noteButton(NOTED_LINE, NOTE));
      const editor = within(rowOf(NOTED_LINE)).getByRole("textbox");
      await user.clear(editor);
      await user.type(editor, "quietly");
      await user.keyboard("{Control>}{Enter}{/Control}");

      expect(await within(rowOf(NOTED_LINE)).findByText(/^\(quietly\)/)).toBeInTheDocument();
      await settle();
      expect(patchLine).toHaveBeenCalledTimes(1);
      expect(patchLine).toHaveBeenCalledWith(SLUG, expect.any(String), { instruction: "quietly" });
      expect(rowOf(NOTED_LINE)).toHaveFocus();
      expect(await newestVersion(api)).toBe(8);
    });

    it("still saves the draft when focus leaves it", async () => {
      const { user, api, patchLine } = await renderDesk();

      await user.click(within(rowOf(PLAIN_LINE)).getByRole("button", { name: "+ add direction" }));
      await user.type(within(rowOf(PLAIN_LINE)).getByRole("textbox"), "low and slow");
      await user.click(script().getByText(NOTED_LINE));

      expect(await within(rowOf(PLAIN_LINE)).findByText(/^\(low and slow\)/)).toBeInTheDocument();
      await settle();
      expect(patchLine).toHaveBeenCalledTimes(1);
      expect(patchLine).toHaveBeenCalledWith(SLUG, expect.any(String), {
        instruction: "low and slow",
      });
      expect(await newestVersion(api)).toBe(8);
    });
  });

  describe("the inspector's Direction field", () => {
    it("Esc restores the note, saves nothing and puts focus back on the row", async () => {
      const { user, api, patchLine } = await renderDesk();

      await user.click(script().getByText(NOTED_LINE));
      const field = screen.getByLabelText("Direction");
      expect(field).toHaveValue(NOTE);
      await user.clear(field);
      await user.type(field, "shouted");
      await user.keyboard("{Escape}");
      await settle();

      expect(patchLine).not.toHaveBeenCalled();
      expect(screen.getByLabelText("Direction")).toHaveValue(NOTE);
      expect(noteButton(NOTED_LINE, NOTE)).toBeInTheDocument();
      expect(rowOf(NOTED_LINE)).toHaveFocus();
      expect(await newestVersion(api)).toBe(7);
    });

    it("Ctrl+Enter saves the note with exactly one PATCH", async () => {
      const { user, api, patchLine } = await renderDesk();

      await user.click(script().getByText(NOTED_LINE));
      const field = screen.getByLabelText("Direction");
      await user.clear(field);
      await user.type(field, "quietly");
      await user.keyboard("{Control>}{Enter}{/Control}");

      await waitFor(() => expect(noteButton(NOTED_LINE, "quietly")).toBeInTheDocument());
      await settle();
      expect(patchLine).toHaveBeenCalledTimes(1);
      expect(patchLine).toHaveBeenCalledWith(SLUG, expect.any(String), { instruction: "quietly" });
      expect(screen.getByLabelText("Direction")).toHaveValue("quietly");
      expect(await newestVersion(api)).toBe(8);
    });

    it("Ctrl+Enter on an unchanged note sends nothing", async () => {
      const { user, api, patchLine } = await renderDesk();

      await user.click(script().getByText(NOTED_LINE));
      await user.click(screen.getByLabelText("Direction"));
      await user.keyboard("{Control>}{Enter}{/Control}");
      await settle();

      expect(patchLine).not.toHaveBeenCalled();
      expect(await newestVersion(api)).toBe(7);
    });

    it("still saves the note when focus leaves it", async () => {
      const { user, api, patchLine } = await renderDesk();

      await user.click(script().getByText(NOTED_LINE));
      const field = screen.getByLabelText("Direction");
      await user.clear(field);
      await user.type(field, "quietly");
      await user.click(script().getByText(PLAIN_LINE));

      await waitFor(() => expect(noteButton(NOTED_LINE, "quietly")).toBeInTheDocument());
      await settle();
      expect(patchLine).toHaveBeenCalledTimes(1);
      expect(patchLine).toHaveBeenCalledWith(SLUG, expect.any(String), { instruction: "quietly" });
      expect(await newestVersion(api)).toBe(8);
    });
  });
});

describe("Desk keyboard shortcuts", () => {
  /** The line before NOTED_LINE; it has no note, so the two are easy to tell apart. */
  const STAIRS_LINE = "The stairs complained under him the way old wood does.";

  /** Chapter 1 starts out approved; its sign-off shows in the header and the rail. */
  const signOff = (status: string) =>
    screen.queryAllByRole("button", { name: new RegExp(`^Chapter 1 sign-off: ${status}\\.`) });

  /** Select STAIRS_LINE: the inspector's Direction field is then empty. */
  async function selectStairs(user: Awaited<ReturnType<typeof renderDesk>>["user"]) {
    await user.click(script().getByText(STAIRS_LINE));
    expect(screen.getByLabelText("Direction")).toHaveValue("");
  }

  const focusPage = () => act(() => (document.activeElement as HTMLElement | null)?.blur());

  it("j and k move the selection from the page and from the script", async () => {
    const { user } = await renderDesk();
    await selectStairs(user);
    expect(rowOf(STAIRS_LINE)).toHaveFocus();

    await user.keyboard("j");
    expect(screen.getByLabelText("Direction")).toHaveValue(NOTE);
    await user.keyboard("k");
    expect(screen.getByLabelText("Direction")).toHaveValue("");

    await focusPage();
    expect(document.body).toHaveFocus();
    await user.keyboard("j");
    expect(screen.getByLabelText("Direction")).toHaveValue(NOTE);
  });

  it("f flags the selected line from the page", async () => {
    const { user, patchLine } = await renderDesk();
    await selectStairs(user);
    await focusPage();

    await user.keyboard("f");
    await waitFor(() =>
      expect(
        within(rowOf(STAIRS_LINE)).getByRole("button", { name: /^Flag line/ }),
      ).toHaveAttribute("aria-pressed", "true"),
    );
    expect(patchLine).toHaveBeenCalledTimes(1);
    expect(patchLine).toHaveBeenCalledWith(SLUG, expect.any(String), { flag: true });
  });

  it("Shift+A signs the chapter off; a alone does nothing", async () => {
    const { user } = await renderDesk();
    expect(signOff("approved").length).toBeGreaterThan(0);
    await focusPage();

    await user.keyboard("a");
    await settle();
    expect(signOff("approved").length).toBeGreaterThan(0);

    // On an approved chapter the shortcut takes it back to unheard.
    await user.keyboard("{Shift>}A{/Shift}");
    await waitFor(() => expect(signOff("unheard").length).toBeGreaterThan(0));
    expect(signOff("approved")).toHaveLength(0);
  });

  it.each([
    ["the scrub slider", () => screen.getByRole("slider", { name: "Chapter 1 position" })],
    ["a filter button", () => screen.getByRole("button", { name: "stale" })],
    ["a chapter in the rail", () => screen.getByRole("button", { name: /^2 · The Letter/ })],
    [
      "a line's flag button",
      () => within(rowOf(STAIRS_LINE)).getByRole("button", { name: /^Flag/ }),
    ],
  ])("no shortcut acts while focus is on %s", async (_name, control) => {
    const { user, patchLine } = await renderDesk();
    await selectStairs(user);
    const target = control();
    await act(() => target.focus());
    expect(target).toHaveFocus();

    await user.keyboard("j");
    await user.keyboard("f");
    await user.keyboard("a");
    await user.keyboard("{Shift>}A{/Shift}");
    await settle();

    expect(screen.getByLabelText("Direction")).toHaveValue("");
    expect(patchLine).not.toHaveBeenCalled();
    expect(signOff("approved").length).toBeGreaterThan(0);
  });

  it("the script's hint names Shift+A for the sign-off", async () => {
    await renderDesk();
    expect(screen.getByText(/j\/k move · f flags · Shift\+A signs off/)).toBeInTheDocument();
  });
});

describe("Desk version and counts after an edit", () => {
  /** Open the rail's Versions panel; the seeded book is at v7. */
  async function openVersions(user: Awaited<ReturnType<typeof renderDesk>>["user"]) {
    await user.click(screen.getByRole("button", { name: /^Versions/ }));
    const panel = await screen.findByRole("region", { name: "Versions" });
    await within(panel).findByText("v7", { exact: true });
    expect(within(panel).getByText("current v7")).toBeInTheDocument();
    return panel;
  }

  /** The Restore button on version `v`'s row of the panel. */
  const restoreOf = (panel: HTMLElement, v: number) =>
    within(within(panel).getByText(`v${v}`, { exact: true }).parentElement!).getByRole("button", {
      name: "Restore",
    });

  it("a note shows the new version in the panel and the new count in the header", async () => {
    const { user, api } = await renderDesk();
    const before = await api.getBook(SLUG);
    const panel = await openVersions(user);

    await user.click(within(rowOf(PLAIN_LINE)).getByRole("button", { name: "+ add direction" }));
    await user.type(within(rowOf(PLAIN_LINE)).getByRole("textbox"), "low and slow");
    await user.keyboard("{Control>}{Enter}{/Control}");

    expect(
      await screen.findByText(`${before.utterances} ln · ${before.directed.directed + 1} directed`),
    ).toBeInTheDocument();
    expect(await within(panel).findByText("current v8")).toBeInTheDocument();
    await within(panel).findByText("v8", { exact: true });
    expect(restoreOf(panel, 8)).toBeDisabled();
    expect(restoreOf(panel, 7)).toBeEnabled();
  });

  it("skipping a chapter shows the version it saved", async () => {
    const { user } = await renderDesk();
    const panel = await openVersions(user);

    await user.click(screen.getByRole("button", { name: "Skip chapter 2" }));

    expect(await within(panel).findByText("current v8")).toBeInTheDocument();
    await within(panel).findByText("v8", { exact: true });
    expect(restoreOf(panel, 8)).toBeDisabled();
    expect(restoreOf(panel, 7)).toBeEnabled();
  });

  it("recasting a line shows the new version and the new directable count", async () => {
    const { user, api } = await renderDesk();
    const before = await api.getBook(SLUG);
    const panel = await openVersions(user);

    // The narrator is on kokoro, Elara on breeze, which takes a written note.
    await user.click(script().getByText(PLAIN_LINE));
    await user.selectOptions(screen.getByLabelText("Speaker"), "Elara");

    expect(await within(panel).findByText("current v8")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Direct this book" })).toHaveAttribute(
        "title",
        `Write scene notes for the ${before.directable.lines + 1} lines that can use them.`,
      ),
    );
    await within(panel).findByText("v8", { exact: true });
  });
});
