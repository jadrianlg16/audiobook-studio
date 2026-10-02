/**
 * Where a single-letter shortcut may act (WCAG 2.1.4, character key shortcuts).
 *
 * A bare letter is a command only when nothing else on the page wants that key:
 * focus is on the page itself, or inside a region that opted in with
 * `data-shortcut-scope` (the desk's script and chapter rail) and not on a
 * control there. A focused button, link, field or slider owns its keys — `a`
 * pressed on the desk's scrub slider used to approve the chapter.
 */
export const SHORTCUT_SCOPE = { "data-shortcut-scope": "" } as const;

/** Elements, and roles, that act on keys themselves. */
const KEY_CONSUMERS = [
  "button",
  "a[href]",
  "input",
  "select",
  "textarea",
  "summary",
  "iframe",
  "audio[controls]",
  "video[controls]",
  "[contenteditable]:not([contenteditable='false'])",
  ...[
    "application",
    "button",
    "checkbox",
    "combobox",
    "grid",
    "gridcell",
    "link",
    "listbox",
    "menu",
    "menubar",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "option",
    "radio",
    "radiogroup",
    "scrollbar",
    "searchbox",
    "slider",
    "spinbutton",
    "switch",
    "tab",
    "tablist",
    "textbox",
    "tree",
    "treegrid",
    "treeitem",
  ].map((role) => `[role="${role}"]`),
].join(",");

/** True when a letter key aimed at `target` may run a page shortcut. */
export function shortcutTargetAllowed(target: EventTarget | null): boolean {
  if (!(target instanceof Element)) return true; // the document or the window
  if (target === target.ownerDocument.body || target === target.ownerDocument.documentElement)
    return true;
  if (target.closest(KEY_CONSUMERS)) return false;
  return target.closest("[data-shortcut-scope]") !== null;
}
