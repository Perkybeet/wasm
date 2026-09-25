import { Badge, StatusGlyph, StatusPill } from "../components/ui";
import type { Status } from "../components/ui";
import { contrastRatio, readThemeTokens } from "../lib/contrast";
import { cx } from "../lib/cx";
import tokensCss from "../styles/tokens.css?raw";
import { Item, Row, Section, Stage } from "./gallery";

const TOKENS = readThemeTokens(tokensCss);

interface Swatch {
  token: string;
  role: string;
  /** Ground the token is read on, to print its contrast. */
  on?: string;
}

const GROUPS: { title: string; swatches: Swatch[] }[] = [
  {
    title: "Surfaces",
    swatches: [
      { token: "bg-sunken", role: "Logs, code, footers" },
      { token: "bg", role: "The page" },
      { token: "surface", role: "Cards, tables" },
      { token: "surface-raised", role: "Popups, dialogs" },
      { token: "border", role: "Hairlines" },
      { token: "border-strong", role: "Control bounds, 3:1", on: "surface" },
    ],
  },
  {
    title: "Text",
    swatches: [
      { token: "text", role: "Primary", on: "bg" },
      { token: "text-muted", role: "Secondary", on: "bg" },
      { token: "text-faint", role: "Meta, placeholders", on: "surface-raised" },
    ],
  },
  {
    title: "Accent",
    swatches: [
      { token: "accent", role: "Primary action fill", on: "surface" },
      { token: "accent-soft", role: "Selection ground" },
      { token: "accent-text", role: "Links, active marks", on: "accent-soft" },
      { token: "focus", role: "Focus ring", on: "bg" },
    ],
  },
  {
    title: "State",
    swatches: [
      { token: "ok", role: "Running, valid", on: "ok-soft" },
      { token: "warn", role: "In progress, expiring", on: "warn-soft" },
      { token: "fail", role: "Failed, down", on: "fail-soft" },
      { token: "idle", role: "Stopped on purpose", on: "idle-soft" },
    ],
  },
];

function ratio(theme: "light" | "dark", fg: string, bg: string): string {
  const a = TOKENS[theme][fg];
  const b = TOKENS[theme][bg];
  if (a === undefined || b === undefined) return "-";
  return `${contrastRatio(a, b).toFixed(1)}:1`;
}

function ThemeChip({ theme, token }: { theme: "light" | "dark"; token: string }) {
  return (
    <div data-theme={theme} className="flex h-12 flex-1 items-end rounded-control border border-border bg-bg p-1.5">
      <div className="h-full w-full rounded-[4px] border border-border" style={{ background: `var(--${token})` }} />
    </div>
  );
}

function Palette() {
  return (
    <div className="grid gap-8 lg:grid-cols-2">
      {GROUPS.map((group) => (
        <div key={group.title} className="min-w-0">
          <h3 className="mb-3 text-13 font-semibold text-fg">{group.title}</h3>
          <ul className="flex flex-col divide-y divide-border rounded-card border border-border bg-surface">
            {group.swatches.map((swatch) => (
              <li key={swatch.token} className="grid grid-cols-[7.5rem_1fr] items-center gap-4 px-3 py-2.5 sm:grid-cols-[9rem_1fr_auto]">
                <div className="flex gap-1.5">
                  <ThemeChip theme="light" token={swatch.token} />
                  <ThemeChip theme="dark" token={swatch.token} />
                </div>
                <div className="min-w-0">
                  <div className="mono truncate text-12 text-fg">--{swatch.token}</div>
                  <div className="text-12 text-fg-muted">{swatch.role}</div>
                </div>
                <div className="mono col-span-2 text-12 text-fg-faint sm:col-span-1 sm:text-right">
                  <div>
                    {TOKENS.light[swatch.token]} / {TOKENS.dark[swatch.token]}
                  </div>
                  {swatch.on !== undefined ? (
                    <div className="text-fg-muted">
                      {ratio("light", swatch.token, swatch.on)} / {ratio("dark", swatch.token, swatch.on)}
                      <span className="text-fg-faint"> on {swatch.on}</span>
                    </div>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

const TYPE: { name: string; spec: string; className: string; sample: string }[] = [
  { name: "Display", spec: "32/40 600 wide", className: "display text-32", sample: "Your server, deployed." },
  { name: "Title", spec: "24/32 600 wide", className: "title text-24", sample: "shop.arenna.dev" },
  { name: "Heading", spec: "18/24 600 wide", className: "title text-18", sample: "Deployments" },
  { name: "Lead", spec: "16/24 400", className: "text-16", sample: "Builds run in their own release directory." },
  { name: "Body", spec: "14/20 400", className: "text-14", sample: "The previous release keeps serving until the new one passes its health check." },
  { name: "Dense", spec: "13/20 400", className: "text-13", sample: "Restarted wasm-shop.arenna.dev.service after a configuration change." },
  { name: "Caption", spec: "12/16 500", className: "text-12 font-medium text-fg-muted", sample: "Deployed 12 min ago by webhook" },
  { name: "Mono", spec: "13/20 400", className: "mono text-13", sample: "/var/www/apps/shop/releases/20260925-143012-a1b2c3d" },
];

function TypeScale() {
  return (
    <Stage plain flush>
      <ul className="divide-y divide-border">
        {TYPE.map((row) => (
          <li key={row.name} className="grid gap-1 px-6 py-4 sm:grid-cols-[8rem_1fr] sm:items-baseline sm:gap-6 max-sm:px-4">
            <div className="flex items-baseline gap-2 sm:flex-col sm:gap-0.5">
              <span className="text-13 font-medium text-fg">{row.name}</span>
              <span className="mono text-12 text-fg-faint">{row.spec}</span>
            </div>
            <p className={cx("min-w-0 break-words text-fg", row.className)}>{row.sample}</p>
          </li>
        ))}
      </ul>
    </Stage>
  );
}

const SPACE = [4, 8, 12, 16, 20, 24, 32, 40, 48, 64];

function Spacing() {
  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_1fr]">
      <Stage plain>
        <ul className="flex flex-col gap-2">
          {SPACE.map((px) => (
            <li key={px} className="grid grid-cols-[3rem_1fr] items-center gap-3">
              <span className="mono text-12 text-fg-muted">{px}</span>
              <span className="block h-3 rounded-[2px] bg-border-strong" style={{ width: px * 2 }} />
            </li>
          ))}
        </ul>
      </Stage>
      <Stage plain>
        <Row>
          <Item label="Control, 6">
            <div className="size-16 rounded-control border border-border-strong bg-surface" />
          </Item>
          <Item label="Card, 10">
            <div className="size-16 rounded-card border border-border bg-surface shadow-raised" />
          </Item>
          <Item label="Pill, 999">
            <div className="h-8 w-20 rounded-pill border border-border bg-surface" />
          </Item>
        </Row>
      </Stage>
    </div>
  );
}

function Elevation() {
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {(["light", "dark"] as const).map((theme) => (
        <div key={theme} data-theme={theme} className="rounded-card border border-border bg-bg-sunken p-5 text-fg">
          <p className="mb-4 text-12 text-fg-muted">{theme === "light" ? "Light: surface plus a whisper of shadow" : "Dark: each step up is lighter"}</p>
          <div className="rounded-card border border-border bg-bg p-4">
            <span className="text-12 text-fg-faint">bg</span>
            <div className="mt-2 rounded-card border border-border bg-surface p-4 shadow-raised">
              <span className="text-12 text-fg-faint">surface</span>
              <div className="mt-2 rounded-card border border-border bg-surface-raised p-4 shadow-overlay">
                <span className="text-12 text-fg-faint">surface-raised, overlay shadow</span>
              </div>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

const STATES: Status[] = ["running", "deploying", "failed", "stopped", "static", "unknown"];

function StateLanguage() {
  return (
    <div className="grid gap-4 sm:grid-cols-2">
      {(["light", "dark"] as const).map((theme) => (
        <div key={theme} data-theme={theme} className="rounded-card border border-border bg-bg p-5 text-fg">
          <ul className="grid grid-cols-3 gap-x-4 gap-y-5">
            {STATES.map((state) => (
              <li key={state} className="flex flex-col items-start gap-2">
                <StatusGlyph state={state} size={24} className={toneOf(state)} />
                <StatusPill state={state} />
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function toneOf(state: Status): string {
  if (state === "running" || state === "static") return "text-ok";
  if (state === "deploying") return "text-warn";
  if (state === "failed") return "text-fail";
  return "text-idle";
}

export function Foundations() {
  return (
    <>
      <Section
        id="colour"
        title="Colour"
        description="Surfaces carry no hue. Anything coloured on screen is either a state or something you can act on. Each row shows the light and dark value and, for foreground tokens, the contrast on the ground they are read on."
      >
        <Palette />
      </Section>
      <Section
        id="state"
        title="State language"
        description="Every state is said three ways: colour, a distinct shape and a word. The shapes differ in silhouette, so a failed service still reads as failed in greyscale."
      >
        <StateLanguage />
      </Section>
      <Section
        id="type"
        title="Type"
        description="Mona Sans for the interface, set a notch wider and tighter in titles; JetBrains Mono for every value that comes from the system. Figures are tabular wherever numbers change."
      >
        <TypeScale />
      </Section>
      <Section
        id="space"
        title="Space and shape"
        description="Space steps through 4, 8, 12, 16, 20, 24, 32, 40, 48 and 64. Three radii: controls, cards, pills. Borders are always one pixel."
      >
        <Spacing />
      </Section>
      <Section
        id="elevation"
        title="Elevation"
        description="In dark, height is luminance: each layer is a step lighter. In light, surfaces add a whisper of shadow. Pronounced shadows are reserved for overlays."
      >
        <Elevation />
        <Row>
          <Badge mono>--duration-fast 120ms</Badge>
          <Badge mono>--duration-base 180ms</Badge>
          <Badge mono>--ease-out cubic-bezier(0.16, 1, 0.3, 1)</Badge>
        </Row>
      </Section>
    </>
  );
}
