import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { THEME_STORAGE_KEY, initTheme, readTheme, setTheme, useTheme } from "./theme";

describe("theme", () => {
  beforeEach(() => {
    initTheme();
  });

  it("follows the system until a theme is chosen", () => {
    expect(readTheme()).toBe("system");
    expect(document.documentElement.dataset["theme"]).toBeUndefined();
  });

  it("persists a pinned theme and applies it to <html>", () => {
    setTheme("dark");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(document.documentElement.dataset["theme"]).toBe("dark");
    setTheme("system");
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
    expect(document.documentElement.dataset["theme"]).toBeUndefined();
  });

  it("applies the stored choice before the first render", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "light");
    initTheme();
    expect(document.documentElement.dataset["theme"]).toBe("light");
  });

  it("ignores a stored value that is not a theme", () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, "neon");
    expect(readTheme()).toBe("system");
  });

  it("re-renders subscribers when the theme changes", () => {
    const { result } = renderHook(() => useTheme());
    expect(result.current[0]).toBe("system");
    act(() => {
      result.current[1]("light");
    });
    expect(result.current[0]).toBe("light");
  });
});
