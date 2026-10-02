import { afterEach, describe, expect, it } from "vitest";

import { shortcutTargetAllowed } from "./shortcuts";

/** Build a fragment in the document and return the element marked `#t`. */
function target(html: string): Element {
  document.body.innerHTML = html;
  return document.querySelector("#t")!;
}

afterEach(() => {
  document.body.innerHTML = "";
});

describe("shortcutTargetAllowed", () => {
  it("allows the page itself", () => {
    expect(shortcutTargetAllowed(document.body)).toBe(true);
    expect(shortcutTargetAllowed(document.documentElement)).toBe(true);
    expect(shortcutTargetAllowed(document)).toBe(true);
    expect(shortcutTargetAllowed(null)).toBe(true);
  });

  it("allows a non-control inside an opted-in region", () => {
    expect(
      target(`<main data-shortcut-scope><div id="t" tabindex="-1">a line</div></main>`),
    ).toSatisfy(shortcutTargetAllowed);
  });

  it("refuses anything outside an opted-in region", () => {
    expect(target(`<aside><div id="t" tabindex="-1">inspector</div></aside>`)).not.toSatisfy(
      shortcutTargetAllowed,
    );
  });

  it.each([
    `<button id="t">Play</button>`,
    `<a id="t" href="/x">link</a>`,
    `<input id="t" />`,
    `<select id="t"></select>`,
    `<textarea id="t"></textarea>`,
    `<div id="t" role="slider" tabindex="0"></div>`,
    `<div id="t" role="tab" tabindex="0"></div>`,
    `<div contenteditable><span id="t">typing</span></div>`,
    `<button><span id="t">inside a button</span></button>`,
  ])("refuses a control, even in an opted-in region: %s", (html) => {
    expect(target(`<main data-shortcut-scope>${html}</main>`)).not.toSatisfy(shortcutTargetAllowed);
  });
});
