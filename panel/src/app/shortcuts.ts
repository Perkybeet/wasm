/**
 * Single-key and two-key shortcuts (`/`, `?`, `g a`), in the style of GitHub and Linear.
 *
 * A shortcut never fires while the operator is typing: in a text field, a select, an editable
 * region, or inside an open dialog or menu, the keys belong to that control. Modified keys
 * (Ctrl, Cmd, Alt) are left to the browser and to the palette's own Mod+K.
 */

import { useEffect, useRef } from "react";

export interface KeyBinding {
  /** Keys pressed one after the other, as KeyboardEvent.key values: ["g", "a"] or ["?"]. */
  keys: readonly string[];
  /** What it does, for the shortcuts dialog. */
  description: string;
  run: () => void;
}

const TEXT_INPUT_TYPES_EXEMPT = new Set(["checkbox", "radio", "button", "submit", "reset", "range", "color", "file", "image"]);

/** Whether keys pressed with focus on `target` are text for that control. */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  if (target instanceof HTMLInputElement) return !TEXT_INPUT_TYPES_EXEMPT.has(target.type);
  return (
    target.closest('[role="textbox"], [role="searchbox"], [role="combobox"], [contenteditable]:not([contenteditable="false"])') !==
    null
  );
}

function insideOverlay(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    target.closest('[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]') !== null
  );
}

function startsWith(keys: readonly string[], prefix: readonly string[]): boolean {
  return prefix.length <= keys.length && prefix.every((key, index) => keys[index] === key);
}

export interface KeySequenceOptions {
  /** A second key must follow the first within this many milliseconds. */
  timeoutMs?: number;
  now?: () => number;
}

/** Builds the keydown handler. Pure over its bindings, so it is tested without a DOM tree. */
export function createKeySequenceHandler(
  getBindings: () => readonly KeyBinding[],
  { timeoutMs = 1_200, now = () => Date.now() }: KeySequenceOptions = {},
): (event: KeyboardEvent) => void {
  let pending: string[] = [];
  let lastAt = 0;

  return (event) => {
    if (event.defaultPrevented || event.isComposing || event.ctrlKey || event.metaKey || event.altKey) {
      pending = [];
      return;
    }
    if (isTypingTarget(event.target) || insideOverlay(event.target)) {
      pending = [];
      return;
    }
    const bindings = getBindings();
    const at = now();
    if (at - lastAt > timeoutMs) pending = [];
    lastAt = at;

    for (const candidate of [[...pending, event.key], [event.key]]) {
      const match = bindings.find((binding) => binding.keys.length === candidate.length && startsWith(binding.keys, candidate));
      if (match) {
        event.preventDefault();
        pending = [];
        match.run();
        return;
      }
      if (bindings.some((binding) => startsWith(binding.keys, candidate))) {
        pending = candidate;
        return;
      }
    }
    pending = [];
  };
}

/** Listens for the bindings on the document while the calling component is mounted. */
export function useKeyboardShortcuts(bindings: readonly KeyBinding[]): void {
  const ref = useRef(bindings);
  useEffect(() => {
    ref.current = bindings;
  });
  useEffect(() => {
    const handler = createKeySequenceHandler(() => ref.current);
    document.addEventListener("keydown", handler);
    return () => {
      document.removeEventListener("keydown", handler);
    };
  }, []);
}

/** Whether the platform labels the primary modifier Cmd rather than Ctrl. */
export function isApplePlatform(): boolean {
  const platform =
    (navigator as Navigator & { userAgentData?: { platform?: string } }).userAgentData?.platform ?? navigator.platform;
  return /mac|iphone|ipad|ipod/i.test(platform);
}

/** The label of the primary modifier key on this platform. */
export function modKeyLabel(): string {
  return isApplePlatform() ? "⌘" : "Ctrl";
}
