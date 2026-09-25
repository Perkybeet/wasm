/**
 * ANSI escape sequences to styled segments.
 *
 * Build tools and services colour their output with SGR sequences (`ESC [ ... m`). The log
 * viewer renders that output as real text, so the sequences are parsed into segments that
 * carry a style, and the style maps onto design tokens: a program's red is the console's
 * fail colour, its green the ok colour. Arbitrary 256-colour and truecolour values collapse
 * to the nearest of those hues so every line keeps the contrast the tokens guarantee.
 *
 * Every other escape (cursor movement, erase line, OSC titles and hyperlinks) is dropped. A
 * carriage return followed by more text replaces what came before it on the line, which is
 * how progress bars redraw themselves in a terminal.
 */

export type AnsiColor =
  | "black"
  | "red"
  | "green"
  | "yellow"
  | "blue"
  | "magenta"
  | "cyan"
  | "white"
  | "gray";

export interface AnsiStyle {
  fg?: AnsiColor;
  bg?: AnsiColor;
  bold?: boolean;
  dim?: boolean;
  italic?: boolean;
  underline?: boolean;
  strike?: boolean;
  inverse?: boolean;
}

export interface AnsiSegment {
  text: string;
  style: AnsiStyle;
}

const BASIC: readonly AnsiColor[] = [
  "black",
  "red",
  "green",
  "yellow",
  "blue",
  "magenta",
  "cyan",
  "white",
];

// CSI (ESC [ params final), OSC (ESC ] ... BEL or ST), and two-byte escapes.
// eslint-disable-next-line no-control-regex -- matching control characters is the point
const ESCAPE = /\x1b(?:\[([0-9;:?<=>]*)[ -/]*([@-~])|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])/g;
// eslint-disable-next-line no-control-regex -- stray C0 controls other than tab
const CONTROL = /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g;

function basic(index: number): AnsiColor {
  return BASIC[index] ?? "white";
}

/** Maps an RGB colour to the nearest hue the console can render with a token. */
export function nearestAnsiColor(r: number, g: number, b: number): AnsiColor {
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  if (max - min < 48) {
    if (max < 64) return "black";
    if (max < 192) return "gray";
    return "white";
  }
  let hue: number;
  if (max === r) hue = ((g - b) / (max - min)) % 6;
  else if (max === g) hue = (b - r) / (max - min) + 2;
  else hue = (r - g) / (max - min) + 4;
  hue = (hue * 60 + 360) % 360;
  if (hue < 25 || hue >= 330) return "red";
  if (hue < 70) return "yellow";
  if (hue < 160) return "green";
  if (hue < 200) return "cyan";
  if (hue < 260) return "blue";
  return "magenta";
}

function color256(n: number): AnsiColor {
  if (n < 8) return basic(n);
  if (n < 16) return n === 8 ? "gray" : basic(n - 8);
  if (n < 232) {
    const i = n - 16;
    const level = (v: number): number => (v === 0 ? 0 : 55 + v * 40);
    return nearestAnsiColor(level(Math.floor(i / 36)), level(Math.floor(i / 6) % 6), level(i % 6));
  }
  const grey = 8 + (n - 232) * 10;
  return nearestAnsiColor(grey, grey, grey);
}

function without(style: AnsiStyle, ...keys: (keyof AnsiStyle)[]): AnsiStyle {
  const next: AnsiStyle = Object.fromEntries(
    Object.entries(style).filter(([key]) => !(keys as string[]).includes(key)),
  );
  return next;
}

/** Applies one SGR parameter list to a style and returns the new style. */
export function applySgr(style: AnsiStyle, params: string): AnsiStyle {
  const codes = params === "" ? [0] : params.split(/[;:]/).map((p) => (p === "" ? 0 : Number(p)));
  let next = style;
  for (let i = 0; i < codes.length; i++) {
    const code = codes[i] ?? 0;
    if (code === 0) next = {};
    else if (code === 1) next = { ...next, bold: true };
    else if (code === 2) next = { ...next, dim: true };
    else if (code === 3) next = { ...next, italic: true };
    else if (code === 4) next = { ...next, underline: true };
    else if (code === 7) next = { ...next, inverse: true };
    else if (code === 9) next = { ...next, strike: true };
    else if (code === 22) next = without(next, "bold", "dim");
    else if (code === 23) next = without(next, "italic");
    else if (code === 24) next = without(next, "underline");
    else if (code === 27) next = without(next, "inverse");
    else if (code === 29) next = without(next, "strike");
    else if (code >= 30 && code <= 37) next = { ...next, fg: basic(code - 30) };
    else if (code === 39) next = without(next, "fg");
    else if (code >= 40 && code <= 47) next = { ...next, bg: basic(code - 40) };
    else if (code === 49) next = without(next, "bg");
    else if (code >= 90 && code <= 97) next = { ...next, fg: code === 90 ? "gray" : basic(code - 90) };
    else if (code >= 100 && code <= 107) next = { ...next, bg: code === 100 ? "gray" : basic(code - 100) };
    else if (code === 38 || code === 48 || code === 58) {
      const mode = codes[i + 1];
      let color: AnsiColor | undefined;
      if (mode === 5) {
        color = color256(codes[i + 2] ?? 0);
        i += 2;
      } else if (mode === 2) {
        color = nearestAnsiColor(codes[i + 2] ?? 0, codes[i + 3] ?? 0, codes[i + 4] ?? 0);
        i += 4;
      }
      // 58 sets the underline colour, which the viewer does not draw; its arguments are skipped.
      if (color !== undefined && code === 38) next = { ...next, fg: color };
      else if (color !== undefined && code === 48) next = { ...next, bg: color };
    }
  }
  return next;
}

function sameStyle(a: AnsiStyle, b: AnsiStyle): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]) as Set<keyof AnsiStyle>;
  for (const key of keys) if (a[key] !== b[key]) return false;
  return true;
}

/** Splits one line of terminal output into styled segments. */
export function parseAnsi(input: string): AnsiSegment[] {
  let segments: AnsiSegment[] = [];
  let style: AnsiStyle = {};

  const push = (raw: string): void => {
    const parts = raw.split("\r");
    parts.forEach((part, index) => {
      if (index > 0 && part.length > 0) segments = [];
      const text = part.replace(CONTROL, "");
      if (text === "") return;
      const previous = segments.at(-1);
      if (previous && sameStyle(previous.style, style)) previous.text += text;
      else segments.push({ text, style });
    });
  };

  let cursor = 0;
  for (const match of input.matchAll(ESCAPE)) {
    push(input.slice(cursor, match.index));
    if (match[2] === "m") style = applySgr(style, match[1] ?? "");
    cursor = match.index + match[0].length;
  }
  push(input.slice(cursor));
  return segments;
}

/** The plain text of a line, as a person would copy it from a terminal. */
export function stripAnsi(input: string): string {
  return parseAnsi(input)
    .map((segment) => segment.text)
    .join("");
}

const FG_CLASS: Record<AnsiColor, string> = {
  black: "text-fg-faint",
  gray: "text-fg-faint",
  white: "text-fg",
  red: "text-fail",
  green: "text-ok",
  yellow: "text-warn",
  blue: "text-ansi-blue",
  magenta: "text-ansi-magenta",
  cyan: "text-ansi-cyan",
};

const BG_CLASS: Record<AnsiColor, string> = {
  black: "bg-surface-active",
  gray: "bg-surface-active",
  white: "bg-surface-active",
  red: "bg-fail-soft",
  green: "bg-ok-soft",
  yellow: "bg-warn-soft",
  blue: "bg-surface-active",
  magenta: "bg-surface-active",
  cyan: "bg-surface-active",
};

/** Token-backed utility classes for a segment style. */
export function ansiClassName(style: AnsiStyle): string {
  const classes: string[] = [];
  if (style.inverse) classes.push("bg-fg", "text-bg");
  else {
    if (style.fg) classes.push(FG_CLASS[style.fg]);
    else if (style.dim) classes.push("text-fg-muted");
    if (style.bg) classes.push(BG_CLASS[style.bg]);
  }
  if (style.bold) classes.push("font-semibold");
  if (style.italic) classes.push("italic");
  if (style.underline) classes.push("underline");
  if (style.strike) classes.push("line-through");
  return classes.join(" ");
}
