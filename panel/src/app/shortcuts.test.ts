import { describe, expect, it, vi } from "vitest";

import { createKeySequenceHandler, isTypingTarget } from "./shortcuts";
import type { KeyBinding } from "./shortcuts";

function press(handler: (event: KeyboardEvent) => void, key: string, init: KeyboardEventInit = {}, target: EventTarget = document.body) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  Object.defineProperty(event, "target", { value: target });
  handler(event);
  return event;
}

function setup(now = () => 0) {
  const apps = vi.fn();
  const search = vi.fn();
  const help = vi.fn();
  const bindings: KeyBinding[] = [
    { keys: ["g", "a"], description: "Go to applications", run: apps },
    { keys: ["/"], description: "Search", run: search },
    { keys: ["?"], description: "Shortcuts", run: help },
  ];
  return { handler: createKeySequenceHandler(() => bindings, { now }), apps, search, help };
}

describe("createKeySequenceHandler", () => {
  it("runs a two-key sequence and claims the second key", () => {
    const { handler, apps } = setup();
    expect(press(handler, "g").defaultPrevented).toBe(false);
    expect(press(handler, "a").defaultPrevented).toBe(true);
    expect(apps).toHaveBeenCalledOnce();
  });

  it("runs single keys, including shifted ones", () => {
    const { handler, search, help } = setup();
    press(handler, "/");
    press(handler, "?", { shiftKey: true });
    expect(search).toHaveBeenCalledOnce();
    expect(help).toHaveBeenCalledOnce();
  });

  it("never fires while the operator types in a field", () => {
    const { handler, apps, search } = setup();
    for (const field of [document.createElement("input"), document.createElement("textarea"), document.createElement("select")]) {
      press(handler, "g", {}, field);
      press(handler, "a", {}, field);
      press(handler, "/", {}, field);
    }
    const editable = document.createElement("div");
    editable.setAttribute("contenteditable", "");
    press(handler, "/", {}, editable);
    expect(apps).not.toHaveBeenCalled();
    expect(search).not.toHaveBeenCalled();
  });

  it("leaves keys inside an open dialog or menu to that overlay", () => {
    const { handler, search } = setup();
    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    const button = document.createElement("button");
    dialog.append(button);
    press(handler, "/", {}, button);
    expect(search).not.toHaveBeenCalled();
  });

  it("leaves modified keys to the browser", () => {
    const { handler, apps } = setup();
    press(handler, "g", { ctrlKey: true });
    press(handler, "a");
    press(handler, "g");
    press(handler, "a", { metaKey: true });
    expect(apps).not.toHaveBeenCalled();
  });

  it("forgets a first key after a pause", () => {
    let now = 0;
    const { handler, apps } = setup(() => now);
    press(handler, "g");
    now = 5_000;
    press(handler, "a");
    expect(apps).not.toHaveBeenCalled();
  });
});

describe("isTypingTarget", () => {
  it("treats a checkbox as a control, not a text field", () => {
    const box = document.createElement("input");
    box.type = "checkbox";
    expect(isTypingTarget(box)).toBe(false);
    expect(isTypingTarget(document.createElement("input"))).toBe(true);
  });
});
