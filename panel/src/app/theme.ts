/**
 * The theme choice: follow the system, or pin light or dark.
 *
 * tokens.css writes every colour as light-dark(), so the document follows the system through
 * `color-scheme`; `data-theme` on <html> pins it. The choice is stored per browser and
 * applied in main.tsx before the first render, so a pinned theme does not flash the other one.
 */

import { useSyncExternalStore } from "react";

export type ThemeChoice = "system" | "light" | "dark";

export const THEME_STORAGE_KEY = "wasm.theme";

export const THEME_CHOICES: readonly { value: ThemeChoice; label: string }[] = [
  { value: "system", label: "System" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

function isThemeChoice(value: unknown): value is ThemeChoice {
  return value === "system" || value === "light" || value === "dark";
}

export function readTheme(): ThemeChoice {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isThemeChoice(stored) ? stored : "system";
  } catch {
    // Storage can be disabled (privacy modes, some embedded browsers): the system decides.
    return "system";
  }
}

export function applyTheme(choice: ThemeChoice, root: HTMLElement = document.documentElement): void {
  if (choice === "system") delete root.dataset["theme"];
  else root.dataset["theme"] = choice;
}

const listeners = new Set<() => void>();
let current: ThemeChoice | null = null;

function snapshot(): ThemeChoice {
  current ??= readTheme();
  return current;
}

/** Applies the stored choice to <html>. Call once, before the first render. */
export function initTheme(): void {
  current = readTheme();
  applyTheme(current);
}

export function setTheme(choice: ThemeChoice): void {
  try {
    if (choice === "system") window.localStorage.removeItem(THEME_STORAGE_KEY);
    else window.localStorage.setItem(THEME_STORAGE_KEY, choice);
  } catch {
    // Not persisted, still applied for this page: the operator sees what they picked.
  }
  current = choice;
  applyTheme(choice);
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  // Another tab changed the theme: follow it, so two tabs of one console never disagree.
  const onStorage = (event: StorageEvent): void => {
    if (event.key !== THEME_STORAGE_KEY && event.key !== null) return;
    current = readTheme();
    applyTheme(current);
    listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

export function useTheme(): readonly [ThemeChoice, (choice: ThemeChoice) => void] {
  return [useSyncExternalStore(subscribe, snapshot), setTheme] as const;
}
