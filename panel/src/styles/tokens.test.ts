import { describe, expect, it } from "vitest";

import { contrastRatio, parseHex, readThemeTokens } from "../lib/contrast";
import fontsCss from "./fonts.css?raw";
import tokensCss from "./tokens.css?raw";

const TOKENS = readThemeTokens(tokensCss);
const THEMES = ["light", "dark"] as const;

const GROUNDS = ["bg", "bg-sunken", "surface", "surface-raised", "surface-hover"];
const STATES = ["ok", "warn", "fail", "idle"];

/** Text and icons on the grounds they are drawn on: 4.5:1 (WCAG 1.4.3). */
const TEXT_PAIRS: [string, string][] = [
  ...["text", "text-muted", "text-faint"].flatMap((fg) => GROUNDS.map((bg): [string, string] => [fg, bg])),
  ["text", "surface-active"],
  ["text-muted", "surface-active"],
  ...["bg", "bg-sunken", "surface", "surface-raised", "accent-soft"].map((bg): [string, string] => ["accent-text", bg]),
  ["on-accent", "accent"],
  ["on-accent", "accent-hover"],
  ["on-accent", "fail-strong"],
  ...STATES.flatMap((state) =>
    ["bg", "bg-sunken", "surface", "surface-raised", `${state}-soft`].map((bg): [string, string] => [state, bg]),
  ),
  ["text", "ok-soft"],
  ["text", "warn-soft"],
  ["text", "fail-soft"],
  ["text", "match"],
  ["text", "selection"],
  // Tooltips are inverted: the page ground as text on the text colour.
  ["bg", "text"],
  ...["ansi-blue", "ansi-magenta", "ansi-cyan"].flatMap((fg) =>
    ["bg-sunken", "surface"].map((bg): [string, string] => [fg, bg]),
  ),
];

/** Boundaries and indicators of controls: 3:1 (WCAG 1.4.11). */
const UI_PAIRS: [string, string][] = [
  ...["border-strong", "focus", "accent"].flatMap((fg) =>
    ["bg", "bg-sunken", "surface", "surface-raised"].map((bg): [string, string] => [fg, bg]),
  ),
  ["on-accent", "border-strong"],
  ["warn", "warn-soft"],
  ["fail", "fail-soft"],
  ["text-muted", "surface-active"],
];

function colour(theme: (typeof THEMES)[number], token: string): string {
  const value = TOKENS[theme][token];
  if (value === undefined) throw new Error(`--${token} is not a solid colour in the ${theme} theme`);
  return value;
}

describe("design tokens", () => {
  it("declares every token the pages rely on, for both themes", () => {
    const required = [
      "bg",
      "bg-sunken",
      "surface",
      "surface-raised",
      "border",
      "border-strong",
      "text",
      "text-muted",
      "text-faint",
      "accent",
      "accent-hover",
      "accent-soft",
      "accent-text",
      "ok",
      "ok-soft",
      "warn",
      "warn-soft",
      "fail",
      "fail-soft",
      "idle",
      "idle-soft",
      "focus",
    ];
    for (const theme of THEMES) {
      for (const token of required) expect(TOKENS[theme], `--${token} (${theme})`).toHaveProperty(token);
    }
    for (const token of ["font-sans", "font-mono", "radius-control", "radius-card", "shadow-overlay", "duration-fast", "duration-base", "ease-out"]) {
      expect(tokensCss).toMatch(new RegExp(`--${token}:`));
    }
    expect(tokensCss).toMatch(/--radius-control: 6px;/);
    expect(tokensCss).toMatch(/--radius-card: 10px;/);
    expect(tokensCss).toMatch(/--duration-fast: 120ms;/);
    expect(tokensCss).toMatch(/--duration-base: 180ms;/);
  });

  it.each(THEMES.flatMap((theme) => TEXT_PAIRS.map(([fg, bg]) => [fg, bg, theme] as const)))(
    "%s on %s meets 4.5:1 in %s",
    (fg, bg, theme) => {
      expect(contrastRatio(colour(theme, fg), colour(theme, bg))).toBeGreaterThanOrEqual(4.5);
    },
  );

  it.each(THEMES.flatMap((theme) => UI_PAIRS.map(([fg, bg]) => [fg, bg, theme] as const)))(
    "%s against %s meets 3:1 in %s",
    (fg, bg, theme) => {
      expect(contrastRatio(colour(theme, fg), colour(theme, bg))).toBeGreaterThanOrEqual(3);
    },
  );

  it.each(THEMES)("keeps every surface and text colour achromatic in %s", (theme) => {
    const achromatic = [
      "bg",
      "bg-sunken",
      "surface",
      "surface-raised",
      "surface-hover",
      "surface-active",
      "border",
      "border-strong",
      "text",
      "text-muted",
      "text-faint",
      "idle",
      "idle-soft",
    ];
    for (const token of achromatic) {
      const [r, g, b] = parseHex(colour(theme, token));
      expect([g, b], `--${token} must be grey`).toEqual([r, r]);
    }
  });

  it("gives the dark theme luminance elevation: each surface step is lighter", () => {
    const steps = ["bg-sunken", "bg", "surface", "surface-raised"].map((t) => parseHex(colour("dark", t))[0]);
    expect([...steps].sort((a, b) => a - b)).toEqual(steps);
    expect(new Set(steps).size).toBe(steps.length);
  });

  it("drops motion to zero for people who ask for reduced motion", () => {
    expect(tokensCss).toMatch(/prefers-reduced-motion: reduce[\s\S]*--duration-fast: 0ms;[\s\S]*--duration-base: 0ms;/);
  });

  it("gives the mono stack an emoji-capable fallback, so a build log's own glyphs render", () => {
    // Deploy and build logs are CLI output verbatim (icons like 📦 and 🔨 included, see
    // CLAUDE.md's "a system error is never paraphrased"); JetBrains Mono has none of them, so
    // an emoji font has to follow it in the stack or they draw as tofu boxes.
    const match = /--font-mono:\s*([^;]+);/.exec(tokensCss);
    expect(match).not.toBeNull();
    const stack = match?.[1] ?? "";
    expect(stack).toContain('"JetBrains Mono Variable"');
    expect(stack).toMatch(/"Noto Color Emoji"|"Apple Color Emoji"|"Segoe UI Emoji"/);
  });

  it("follows each real font with its metric-matched fallbacks, each one declared and scaled", () => {
    // Text drawn before the fonts arrive takes the room it will keep, so the swap moves nothing.
    const stack = (token: string): string[] =>
      (new RegExp(`--${token}:\\s*([^;]+);`).exec(tokensCss)?.[1] ?? "").split(",").map((family) => family.trim().replace(/"/g, ""));
    const sans = stack("font-sans");
    const mono = stack("font-mono");
    expect(sans.slice(0, 3)).toEqual(["Mona Sans Variable", "Mona Sans Fallback", "Mona Sans Fallback DejaVu"]);
    expect(mono.slice(0, 4)).toEqual([
      "JetBrains Mono Variable",
      "JetBrains Mono Fallback",
      "JetBrains Mono Fallback Consolas",
      "JetBrains Mono Fallback Liberation",
    ]);
    const faces = [...fontsCss.matchAll(/@font-face\s*{([^}]*)}/g)].map((match) => match[1] ?? "");
    for (const family of [...sans.slice(1, 3), ...mono.slice(1, 4)]) {
      const face = faces.find((body) => body.includes(`font-family: "${family}";`));
      expect(face, family).toBeDefined();
      expect(face).toMatch(/src: local\(/);
      expect(face).toMatch(/size-adjust: \d+(\.\d+)?%;/);
      expect(face).toMatch(/ascent-override: \d+(\.\d+)?%;/);
      expect(face).toMatch(/descent-override: \d+(\.\d+)?%;/);
    }
  });
});
