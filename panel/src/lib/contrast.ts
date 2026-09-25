/**
 * WCAG 2.2 contrast arithmetic and a reader for tokens.css.
 *
 * The tokens are the single source of every colour, so contrast is verified on them rather
 * than on rendered pages: a failing pair is caught before any component uses it.
 */

export type Rgb = readonly [number, number, number];
export type Theme = "light" | "dark";
export type ThemeTokens = Record<Theme, Record<string, string>>;

/** Parses `#rgb` or `#rrggbb` into 0-255 channels. */
export function parseHex(hex: string): Rgb {
  const raw = hex.trim().replace(/^#/, "");
  const full = raw.length === 3 ? raw.replace(/./g, (c) => c + c) : raw;
  if (!/^[0-9a-fA-F]{6}$/.test(full)) {
    throw new Error(`Not a hex colour: ${hex}`);
  }
  return [0, 2, 4].map((i) => Number.parseInt(full.slice(i, i + 2), 16)) as unknown as Rgb;
}

/** Relative luminance as defined by WCAG 2.x. */
export function relativeLuminance([r, g, b]: Rgb): number {
  const lin = (c: number): number => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

/** Contrast ratio between two hex colours, from 1 to 21. */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(parseHex(a));
  const lb = relativeLuminance(parseHex(b));
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

const DECLARATION = /--([a-z0-9-]+)\s*:\s*([^;]+);/g;
const LIGHT_DARK = /^light-dark\(\s*(#[0-9a-fA-F]{3,6})\s*,\s*(#[0-9a-fA-F]{3,6})\s*\)$/;
const HEX = /^#[0-9a-fA-F]{3,6}$/;

/** The body of the first `:root { ... }` rule, braces balanced. */
function rootBlock(css: string): string {
  const open = css.indexOf("{", css.indexOf(":root"));
  if (open === -1) return "";
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === "{") depth += 1;
    else if (css[i] === "}") {
      depth -= 1;
      if (depth === 0) return css.slice(open + 1, i);
    }
  }
  return css.slice(open + 1);
}

/**
 * Reads the solid colour tokens of the `:root` block of tokens.css for both themes.
 *
 * Only hex values are returned: `light-dark(a, b)` yields `a` for light and `b` for dark, a
 * bare hex applies to both. Translucent values (shadows, backdrop) are not text grounds and
 * are skipped.
 */
export function readThemeTokens(css: string): ThemeTokens {
  const root = rootBlock(css);
  const tokens: ThemeTokens = { light: {}, dark: {} };
  for (const match of root.matchAll(DECLARATION)) {
    const [, name, rawValue] = match;
    if (name === undefined || rawValue === undefined) continue;
    const value = rawValue.trim();
    const pair = LIGHT_DARK.exec(value);
    if (pair?.[1] !== undefined && pair[2] !== undefined) {
      tokens.light[name] = pair[1];
      tokens.dark[name] = pair[2];
    } else if (HEX.test(value)) {
      tokens.light[name] = value;
      tokens.dark[name] = value;
    }
  }
  return tokens;
}
