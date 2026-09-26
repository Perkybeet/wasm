import { Maximize2, ZoomIn, ZoomOut } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { CSSProperties, KeyboardEvent, ReactElement, ReactNode } from "react";
import uPlot from "uplot";

import { cx } from "../../lib/cx";
import { isHttpUrl } from "../../lib/url";
import { Button } from "./Button";
import { Dialog } from "./Dialog";
import { IconButton } from "./IconButton";
import { STATUS, StatusGlyph } from "./StatusPill";
import type { Status } from "./StatusPill";
import { Tooltip } from "./Tooltip";

export interface ChartSeries {
  label: string;
  values: readonly (number | null)[];
}

export interface ChartMarker {
  /** Unix seconds. */
  at: number;
  /**
   * Full accessible words: e.g. "Deploy 25, succeeded, Sep 25, 19:42". This is the marker's
   * accessible name everywhere it appears, and its visible text in the table view's list.
   */
  label: string;
  /** Colour and shape, the same vocabulary as everywhere else a state is drawn. */
  state: Status;
  /** A plain link target, used when there is no `renderMarker`. Only an http(s) URL is drawn
   * as a link; anything else (a relative path, a dangerous scheme) falls back to an inert
   * control, the same as having neither. */
  href?: string;
  /**
   * Wraps the marker's affordance in a link. Chart does not import a router, so a caller
   * that needs client-side navigation (TanStack's `<Link>`) passes this instead of `href`.
   * Spread `linkProps` onto the returned element (it carries the marker's look: size, shape,
   * colour) and give it an accessible name from `marker.label` (e.g. `aria-label`). Returns
   * one element - the marker's whole affordance, not a fragment or a list.
   */
  renderMarker?: (
    marker: ChartMarker,
    children: ReactNode,
    linkProps: { className: string; style: CSSProperties },
  ) => ReactElement<Record<string, unknown>>;
}

/**
 * The page's own range selector, repeated in the enlarged chart so the range can be changed
 * without closing it. The page owns the range (usually in the URL) and re-renders the chart
 * with the new data; `value` tells the chart the range changed, which resets any zoom.
 */
export interface ChartRangeSelector {
  value: string;
  control: ReactNode;
}

export interface ChartProps {
  title: string;
  /** Window of the data in words, e.g. "Last 30 minutes". Part of the summary. */
  description?: string;
  /** Unix seconds, ascending, one per value of every series. */
  timestamps: readonly number[];
  /** Up to three series. The first is drawn in the accent; the others dashed and dotted. */
  series: readonly ChartSeries[];
  /** Events drawn on the time axis: deploys today, generically anything with a moment and a state. */
  markers?: readonly ChartMarker[];
  /** Formats a value for axis, legend, summary and table. Pass a stable function. */
  formatValue?: (value: number) => string;
  /** Fixes the value axis, e.g. [0, 100] for percentages. */
  yRange?: readonly [number, number];
  /**
   * Overrides the axis and table time format normally derived from the data's span: a clock
   * under about two days, the date beyond it. Rarely needed; a caller that always wants one
   * or the other (a form scoped to a single day, say) can force it.
   */
  timeFormat?: "clock" | "date";
  height?: number;
  /** Shown in the enlarged chart, where the page has one. */
  rangeSelector?: ChartRangeSelector;
  className?: string;
}

interface Palette {
  series: string[];
  fill: string;
  grid: string;
  axis: string;
  /** One resolved colour per state tone, for the marker hairlines the canvas draws itself. */
  tone: Record<Tone, string>;
}

type Tone = "ok" | "warn" | "fail" | "idle";

const DASHES: (number[] | undefined)[] = [undefined, [4, 3], [1.5, 3]];
const SERIES_TOKENS = ["--accent", "--text-muted", "--text-faint"];
const TONE_TOKENS: Record<Tone, string> = { ok: "--ok", warn: "--warn", fail: "--fail", idle: "--idle" };
const AXIS_FONT = '11px "JetBrains Mono Variable", ui-monospace, monospace';

/**
 * Resolves tokens to concrete colours for the canvas. Tokens are light-dark() pairs, so the
 * value depends on the colour scheme in force at the chart, not on the root.
 */
function readPalette(host: HTMLElement): Palette {
  const probe = document.createElement("span");
  // A transition on colour would make the read below return the previous token.
  probe.style.transition = "none";
  host.append(probe);
  const resolve = (token: string): string => {
    probe.style.color = `var(${token})`;
    return getComputedStyle(probe).color;
  };
  const series = SERIES_TOKENS.map(resolve);
  const grid = resolve("--border");
  const axis = resolve("--text-faint");
  const tone = {
    ok: resolve(TONE_TOKENS.ok),
    warn: resolve(TONE_TOKENS.warn),
    fail: resolve(TONE_TOKENS.fail),
    idle: resolve(TONE_TOKENS.idle),
  };
  probe.remove();
  const accent = series[0] ?? "currentColor";
  const fill = accent.startsWith("rgb(") ? accent.replace("rgb(", "rgba(").replace(")", ", 0.08)") : "transparent";
  return { series, fill, grid, axis, tone };
}

/** The narrowest the value axis gets, in CSS pixels; also its width before the first draw. */
const VALUE_AXIS_MIN = 40;
const VALUE_AXIS_PADDING = 12;

/**
 * Width of the value axis for the labels it is about to draw. uPlot asks with the formatted
 * labels; the canvas measures them in the axis font (its pixels are device pixels).
 */
export function valueAxisSize(u: Pick<uPlot, "ctx">, values: readonly string[] | null | undefined): number {
  const longest = (values ?? []).reduce((widest, label) => (label.length > widest.length ? label : widest), "");
  if (longest === "") return VALUE_AXIS_MIN;
  u.ctx.font = AXIS_FONT;
  const width = u.ctx.measureText(longest).width;
  return Math.max(VALUE_AXIS_MIN, Math.ceil(width + VALUE_AXIS_PADDING));
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;

/** The span, in seconds, past which an axis or table needs a date rather than a bare clock. */
const TWO_DAYS = 2 * 86_400;
/** Below this, a short range (an hour, say) still gets a date if it happens to cross
 * midnight; well short of the 24h range's own span, which always crosses exactly one
 * midnight by construction and stays a bare clock regardless. */
const HALF_DAY = 43_200;

function formatTime(seconds: number): string {
  // 24-hour clock: shorter on the axis and the way server logs print time.
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

function isMidnightLocal(date: Date): boolean {
  return date.getHours() === 0 && date.getMinutes() === 0;
}

function formatDateOnly(date: Date): string {
  return `${MONTHS[date.getMonth()] ?? ""} ${String(date.getDate())}`;
}

/**
 * A moment in the format its span calls for: a bare 24-hour clock under about two days, the
 * date and time beyond it ("Sep 25 14:00"), or the bare date at a tick that lands exactly on
 * midnight. Shared by the axis and the table's time column, so both read the same way.
 */
export function formatChartTime(seconds: number, withDate: boolean): string {
  if (!withDate) return formatTime(seconds);
  const date = new Date(seconds * 1000);
  return isMidnightLocal(date) ? formatDateOnly(date) : `${formatDateOnly(date)} ${formatTime(seconds)}`;
}

/**
 * Whether the axis and table need a date, not just a clock: the data spans more than about
 * two days (the 7d and 30d ranges), or a much shorter one - under half a day, so this never
 * catches the 24h range, which always crosses exactly one midnight by construction - that
 * happens to cross local midnight (23:30 to 00:30 is a different day without one, even though
 * neither reading repeats a clock the other already showed).
 */
export function needsDateFormat(timestamps: readonly number[]): boolean {
  const first = timestamps[0];
  const last = timestamps.at(-1);
  if (first === undefined || last === undefined) return false;
  const span = last - first;
  if (span > TWO_DAYS) return true;
  if (span >= HALF_DAY) return false;
  return new Date(first * 1000).toDateString() !== new Date(last * 1000).toDateString();
}

/** The markers whose moment falls within `[from, to]`; the rest are not drawn anywhere. */
export function markersInRange(markers: readonly ChartMarker[], from: number, to: number): ChartMarker[] {
  return markers.filter((marker) => marker.at >= from && marker.at <= to);
}

/** The slice of a uPlot instance that marker positioning needs, so the math is testable
 * without a real plot: a canvas-pixel plot-area box and the scale's own value-to-pixel map. */
export interface MarkerPlot {
  valToPos: (value: number, scale: "x", canvasPixels?: boolean) => number;
  bbox: { left: number; top: number; width: number; height: number };
}

export interface MarkerPosition {
  marker: ChartMarker;
  /** CSS pixels, relative to the chart's own root element (uPlot's target). */
  left: number;
  top: number;
}

/**
 * Where each marker's affordance sits over the plot, from uPlot's own geometry: `bbox` is the
 * plot area's offset in canvas pixels, `valToPos` maps a value to a position. No DOM
 * measuring: this runs inside the chart's own draw hook, after every draw and resize.
 */
export function positionMarkers(u: MarkerPlot, markers: readonly ChartMarker[], pxRatio: number): MarkerPosition[] {
  const leftCss = u.bbox.left / pxRatio;
  const topCss = u.bbox.top / pxRatio;
  return markers.map((marker) => ({
    marker,
    left: leftCss + u.valToPos(marker.at, "x", false),
    top: topCss,
  }));
}

/** Draws a dashed hairline at each marker's x, straight on the canvas, top to bottom of the
 * plot area. Colours are resolved concrete values (canvas cannot use a CSS custom property). */
function drawMarkerLines(u: uPlot, markers: readonly ChartMarker[], colorOf: (state: Status) => string): void {
  if (markers.length === 0) return;
  const { ctx } = u;
  const dpr = uPlot.pxRatio || 1;
  ctx.save();
  ctx.lineWidth = Math.max(1, dpr);
  for (const marker of markers) {
    const x = u.valToPos(marker.at, "x", true);
    ctx.strokeStyle = colorOf(marker.state);
    ctx.setLineDash([4 * dpr, 3 * dpr]);
    ctx.beginPath();
    ctx.moveTo(x, u.bbox.top);
    ctx.lineTo(x, u.bbox.top + u.bbox.height);
    ctx.stroke();
  }
  ctx.restore();
}

/**
 * A description read on after the title: its first word lowercased when it is an ordinary word
 * ("Last hour" -> "last hour", not "CPU" or "GB"), and its closing full stop dropped, since the
 * summary puts its own.
 */
export function continuing(description: string): string {
  const text = description.trim().replace(/\.+$/, "");
  return /^[A-Z][a-z]/.test(text) ? `${text.charAt(0).toLowerCase()}${text.slice(1)}` : text;
}

function summarise(
  title: string,
  description: string | undefined,
  series: readonly ChartSeries[],
  format: (value: number) => string,
  markerCount: number | undefined,
): string {
  const parts = series.map((s) => {
    const values = s.values.filter((v): v is number => v !== null);
    if (values.length === 0) return `${s.label}: no data`;
    const latest = values.at(-1) ?? 0;
    return `${s.label}: latest ${format(latest)}, low ${format(Math.min(...values))}, high ${format(Math.max(...values))}`;
  });
  const base = `${title}${description ? `, ${continuing(description)}` : ""}. ${parts.join("; ")}.`;
  if (markerCount === undefined) return base;
  return `${base} ${String(markerCount)} marker${markerCount === 1 ? "" : "s"} in view.`;
}

function SeriesSwatch({ index }: { index: number }) {
  const dash = DASHES[index];
  return (
    <svg width="16" height="8" viewBox="0 0 16 8" aria-hidden="true" className="shrink-0">
      <line
        x1="1"
        y1="4"
        x2="15"
        y2="4"
        stroke={`var(${SERIES_TOKENS[index] ?? "--text-faint"})`}
        strokeWidth="2"
        strokeLinecap="round"
        {...(dash ? { strokeDasharray: dash.join(" ") } : {})}
      />
    </svg>
  );
}

const MARKER_ICON_CLASS =
  "flex size-6 items-center justify-center rounded-pill bg-surface hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-focus";
const MARKER_CHIP_CLASS =
  "flex h-7 items-center gap-1.5 rounded-pill border border-border bg-surface px-2.5 text-12 hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-focus";

/** A marker's affordance: `renderMarker` when given, else a plain link, else a focusable
 * (but inert) span. Every branch gets the same look and the same accessible name. */
function markerAffordance(marker: ChartMarker, children: ReactNode, className: string): ReactElement<Record<string, unknown>> {
  const style: CSSProperties = { color: `var(${TONE_TOKENS[STATUS[marker.state].tone]})` };
  if (marker.renderMarker) return marker.renderMarker(marker, children, { className, style });
  if (marker.href !== undefined && isHttpUrl(marker.href)) {
    return (
      <a href={marker.href} aria-label={marker.label} className={className} style={style}>
        {children}
      </a>
    );
  }
  // Neither a link nor a caller's own render - or an href that is not safe to navigate to:
  // still a real, natively focusable control (so the tooltip reaches keyboard users), marked
  // aria-disabled since, unlike a native `disabled`, that still lets it take focus.
  return (
    <button type="button" aria-disabled="true" aria-label={marker.label} className={className} style={style}>
      {children}
    </button>
  );
}

/** A stretch of the time axis, Unix seconds, [from, to]. */
export type ChartWindow = readonly [number, number];

function defaultFormat(value: number): string {
  return value.toLocaleString(undefined, { maximumFractionDigits: 1 });
}

/** The first and last index whose moment is inside `window` (all of them without one). */
export function visibleBounds(timestamps: readonly number[], window: ChartWindow | null): readonly [number, number] | null {
  if (timestamps.length === 0) return null;
  if (window === null) return [0, timestamps.length - 1];
  const lo = timestamps.findIndex((t) => t >= window[0]);
  const hi = timestamps.findLastIndex((t) => t <= window[1]);
  return lo === -1 || hi === -1 || hi < lo ? null : [lo, hi];
}

/** The moment in full, for assistive technology: "Sep 25, 2026, 14:32:05". */
function absoluteTime(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "medium", hourCycle: "h23" });
}

/** What the readout says of one sample, e.g. "14:32, CPU 12.4%". */
export function readoutWords(
  seconds: number,
  withDate: boolean,
  series: readonly ChartSeries[],
  index: number,
  format: (value: number) => string,
): string {
  const values = series.map((s) => {
    const value = s.values[index];
    return `${s.label} ${value === null || value === undefined ? "no reading" : format(value)}`;
  });
  return [formatChartTime(seconds, withDate), ...values].join(", ");
}

/**
 * `value`, at most once per `ms`: the newest value always lands, but a key held down does not
 * queue a sentence per sample for a screen reader to work through.
 */
export function useThrottled(value: string, ms: number): string {
  const [shown, setShown] = useState(value);
  const last = useRef(0);
  useEffect(() => {
    const wait = Math.max(0, last.current + ms - Date.now());
    const timer = setTimeout(() => {
      last.current = Date.now();
      setShown(value);
    }, wait);
    return () => {
      clearTimeout(timer);
    };
  }, [value, ms]);
  return shown;
}

const ANNOUNCE_EVERY_MS = 400;

interface ChartBodyProps {
  title: string;
  summary: string;
  timestamps: readonly number[];
  series: readonly ChartSeries[];
  markers: readonly ChartMarker[] | undefined;
  formatValue: (value: number) => string;
  yRange: readonly [number, number] | undefined;
  withDate: boolean;
  height: number;
  asTable: boolean;
  tableId: string;
  /** The stretch shown; null for all of it. */
  zoom: ChartWindow | null;
  /** Set only where the chart zooms (the enlarged one): dragging across it selects a stretch. */
  onZoom?: (window: ChartWindow | null) => void;
}

/**
 * Everything under a chart's title: the readout, and the plot or its table. Shared by the
 * chart on the page and the enlarged one, so both read, step and draw the same way.
 */
function ChartBody({
  title,
  summary,
  timestamps,
  series,
  markers,
  formatValue,
  yRange,
  withDate,
  height,
  asTable,
  tableId,
  zoom,
  onZoom,
}: ChartBodyProps) {
  const [positions, setPositions] = useState<readonly MarkerPosition[]>([]);
  // The sample under the cursor (pointer or keyboard); null shows the latest values.
  const [cursor, setCursor] = useState<number | null>(null);
  const [spoken, setSpoken] = useState("");
  const announcement = useThrottled(spoken, ANNOUNCE_EVERY_MS);
  const hostRef = useRef<HTMLDivElement>(null);
  const plotRef = useRef<uPlot | null>(null);
  const formatRef = useRef(formatValue);
  const markersRef = useRef<readonly ChartMarker[]>(markers ?? []);
  const withDateRef = useRef(withDate);
  const onZoomRef = useRef(onZoom);
  const hintId = useId();
  const zoomable = onZoom !== undefined;

  useEffect(() => {
    formatRef.current = formatValue;
    markersRef.current = markers ?? [];
    withDateRef.current = withDate;
    onZoomRef.current = onZoom;
  });

  const data = useMemo<uPlot.AlignedData>(
    () => [Array.from(timestamps), ...series.map((s) => Array.from(s.values))],
    [timestamps, series],
  );
  const labels = series.map((s) => s.label).join("\u0000");
  const markersKey = (markers ?? []).map((m) => `${String(m.at)}|${m.state}|${m.label}`).join("\u0000");
  const from = zoom?.[0] ?? timestamps[0];
  const to = zoom?.[1] ?? timestamps.at(-1);
  const visibleMarkers = markers !== undefined && from !== undefined && to !== undefined ? markersInRange(markers, from, to) : [];
  const bounds = visibleBounds(timestamps, zoom);

  // Each value keeps the width of the widest it can show, so the readout does not jostle the
  // series beside it as the cursor moves.
  const valueWidths = useMemo(
    () => series.map((s) => s.values.reduce<number>((widest, v) => (v === null ? widest : Math.max(widest, formatValue(v).length)), 1)),
    [series, formatValue],
  );

  useEffect(() => {
    const host = hostRef.current;
    if (!host || asTable) return;
    let palette = readPalette(host);
    const options: uPlot.Options = {
      width: Math.max(host.clientWidth, 240),
      height,
      legend: { show: false },
      cursor: {
        drag: { x: zoomable, y: false, setScale: false },
        points: { size: 6 },
        bind: {
          // uPlot's own double-click resets the scale behind React's back; here it resets the
          // zoom the enlarged chart holds, and does nothing where there is none.
          dblclick: () => () => {
            onZoomRef.current?.(null);
            return null;
          },
        },
      },
      scales: {
        x: { time: true },
        y: yRange
          ? { range: [yRange[0], yRange[1]] }
          : { range: (_u, _min, max) => [0, max > 0 ? max * 1.15 : 1] },
      },
      axes: [
        {
          stroke: () => palette.axis,
          font: AXIS_FONT,
          grid: { show: false },
          ticks: { stroke: () => palette.grid, width: 1, size: 4 },
          size: 24,
          gap: 4,
          // A date+time label is much wider than a clock: fewer, well-spaced ticks so
          // neither collides at a narrow width, rather than uPlot's fixed default.
          space: () => (withDateRef.current ? 92 : 56),
          values: (_u, splits) => splits.map((s) => formatChartTime(s, withDateRef.current)),
        },
        {
          stroke: () => palette.axis,
          font: AXIS_FONT,
          grid: { stroke: () => palette.grid, width: 1 },
          ticks: { show: false },
          // As wide as the widest label: a fixed width clipped "771 KB/s" to "77 KB/s".
          size: (u, values) => valueAxisSize(u, values),
          gap: 6,
          values: (_u, splits) => splits.map((s) => formatRef.current(s)),
        },
      ],
      series: [
        {},
        ...labels.split("\u0000").map((label, index) => ({
          label,
          stroke: () => palette.series[index] ?? palette.axis,
          width: 1.5,
          points: { show: false },
          // uPlot passes dashes straight to the canvas, which counts device pixels.
          ...(DASHES[index] ? { dash: DASHES[index].map((v) => v * window.devicePixelRatio) } : {}),
          ...(index === 0 ? { fill: () => palette.fill } : {}),
        })),
      ],
      hooks: {
        // Fires after every redraw uPlot does on its own - construction, setData, setSize,
        // setScale - so this alone keeps the hairlines and the marker positions in sync with
        // what is on the canvas, zoomed or not.
        draw: [
          (u: uPlot) => {
            const min = u.scales["x"]?.min;
            const max = u.scales["x"]?.max;
            if (min === undefined || max === undefined) {
              setPositions([]);
              return;
            }
            const visible = markersInRange(markersRef.current, min, max);
            drawMarkerLines(u, visible, (state) => palette.tone[STATUS[state].tone]);
            setPositions(positionMarkers(u, visible, uPlot.pxRatio || 1));
          },
        ],
        // The readout follows uPlot's own cursor: the nearest sample, or none once it leaves.
        setCursor: [
          (u: uPlot) => {
            setCursor(u.cursor.idx ?? null);
          },
        ],
        setSelect: [
          (u: uPlot) => {
            const { left, width } = u.select;
            if (width < 2) return;
            u.setSelect({ left: 0, top: 0, width: 0, height: 0 }, false);
            const start = u.posToVal(left, "x");
            const end = u.posToVal(left + width, "x");
            // A stretch with fewer than two samples is not a line: leave the zoom as it is.
            const inside = timestampsInside(u.data[0], start, end);
            if (inside < 2) return;
            onZoomRef.current?.([start, end]);
          },
        ],
      },
    };
    const plot = new uPlot(options, data, host);
    plotRef.current = plot;

    const resize = new ResizeObserver(() => {
      plot.setSize({ width: Math.max(host.clientWidth, 240), height });
    });
    resize.observe(host);

    // Canvas pixels do not follow CSS: repaint when the theme changes.
    const repaint = (): void => {
      palette = readPalette(host);
      plot.redraw(false);
    };
    const themeObserver = new MutationObserver(repaint);
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    scheme.addEventListener("change", repaint);

    return () => {
      resize.disconnect();
      themeObserver.disconnect();
      scheme.removeEventListener("change", repaint);
      plot.destroy();
      plotRef.current = null;
      setCursor(null);
    };
    // Data, marker and zoom changes are applied below without rebuilding the plot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asTable, height, labels, zoomable, yRange?.[0], yRange?.[1]]);

  const zoomFrom = zoom?.[0];
  const zoomTo = zoom?.[1];
  useEffect(() => {
    const plot = plotRef.current;
    if (!plot) return;
    // markersKey is not read here, but setData re-runs the draw hook above, so a marker that
    // arrived without a new reading (a deploy just this second) still gets drawn.
    plot.setData(data);
    if (zoomFrom !== undefined && zoomTo !== undefined) plot.setScale("x", { min: zoomFrom, max: zoomTo });
  }, [data, markersKey, zoomFrom, zoomTo, asTable]);

  /** Moves the cursor to a sample (null hides it) and says so, from the keyboard. */
  const moveCursor = (index: number | null): void => {
    setCursor(index);
    const plot = plotRef.current;
    if (index === null) {
      setSpoken("");
      plot?.setCursor({ left: -10, top: -10 });
      return;
    }
    const at = timestamps[index];
    if (at === undefined) return;
    setSpoken(readoutWords(at, withDate, series, index, formatValue));
    if (plot) {
      const value = series[0]?.values[index];
      const top = value === null || value === undefined ? plot.bbox.height / (2 * (uPlot.pxRatio || 1)) : plot.valToPos(value, "y");
      plot.setCursor({ left: plot.valToPos(at, "x"), top });
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (bounds === null) return;
    const [lo, hi] = bounds;
    const current = cursor !== null && cursor >= lo && cursor <= hi ? cursor : null;
    let next: number | null;
    switch (event.key) {
      case "ArrowLeft":
        next = current === null ? hi : Math.max(lo, current - 1);
        break;
      case "ArrowRight":
        next = current === null ? hi : Math.min(hi, current + 1);
        break;
      case "Home":
        next = lo;
        break;
      case "End":
        next = hi;
        break;
      case "Escape":
        // With nothing to clear, Escape is the enclosing dialog's to close.
        if (cursor === null) return;
        event.stopPropagation();
        next = null;
        break;
      default:
        return;
    }
    event.preventDefault();
    moveCursor(next);
  };

  const shown = cursor !== null && cursor < timestamps.length ? cursor : null;
  const shownAt = shown === null ? undefined : timestamps[shown];
  const latest = series.map((s) => s.values.findLast((v) => v !== null) ?? null);
  const readout = series.map((s, index) => (shown === null ? latest[index] : s.values[shown]) ?? null);

  return (
    <>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-12">
        {/* The moment the values below were read: the hovered sample's, or the newest. */}
        <span className="mono shrink-0 text-fg-muted" style={{ minWidth: withDate ? "12ch" : "6ch" }} data-readout-time="">
          {shownAt === undefined ? (
            "Latest"
          ) : (
            <time dateTime={new Date(shownAt * 1000).toISOString()}>
              <span aria-hidden="true">{formatChartTime(shownAt, withDate)}</span>
              <span className="sr-only">{absoluteTime(shownAt)}</span>
            </time>
          )}
        </span>
        <ul className="flex flex-wrap gap-x-4 gap-y-1" aria-label="Series">
          {series.map((s, index) => {
            const value = readout[index];
            return (
              <li key={s.label} className="flex items-center gap-1.5 text-fg-muted">
                <SeriesSwatch index={index} />
                <span>{s.label}</span>
                <span className="mono text-right text-fg" style={{ minWidth: `${String(valueWidths[index] ?? 1)}ch` }}>
                  {value !== null && value !== undefined ? formatValue(value) : "-"}
                </span>
              </li>
            );
          })}
        </ul>
      </div>
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
        {announcement}
      </div>

      {asTable ? (
        <div className="rounded-control border border-border">
          <div
            id={tableId}
            role="region"
            aria-label={`${title} data`}
            tabIndex={0}
            className="overflow-auto scroll-thin focus-visible:outline-2 focus-visible:outline-focus"
            style={{ maxHeight: height + 40 }}
          >
            <table className="w-full text-left text-12">
              <caption className="sr-only">{`${title}, newest first`}</caption>
              <thead className="sticky top-0 bg-bg-sunken">
                <tr>
                  <th scope="col" className="px-3 py-1.5 font-medium text-fg-muted">
                    Time
                  </th>
                  {series.map((s) => (
                    <th key={s.label} scope="col" className="px-3 py-1.5 text-right font-medium text-fg-muted">
                      {s.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {timestamps
                  .map((t, i) => ({ t, i }))
                  .filter(({ i }) => bounds !== null && i >= bounds[0] && i <= bounds[1])
                  .reverse()
                  .map(({ t, i }) => (
                    <tr key={t} className="border-t border-border">
                      <td className="mono px-3 py-1 text-fg-muted">{formatChartTime(t, withDate)}</td>
                      {series.map((s) => {
                        const v = s.values[i];
                        return (
                          <td key={s.label} className="mono px-3 py-1 text-right text-fg">
                            {v === null || v === undefined ? "-" : formatValue(v)}
                          </td>
                        );
                      })}
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
          {/* Outside the table's own scrolling region, so a mouse or trackpad user sees
              these without having to scroll a small box first; still reachable by keyboard
              and by a screen reader's virtual cursor either way, so they never vanish. */}
          {visibleMarkers.length > 0 ? (
            <div className="border-t border-border p-3">
              {/* Not a heading: it would land at an arbitrary level inside whatever page
                  section holds this chart, skipping levels and failing axe's heading-order
                  rule. A caption reads the same to a screen reader without that risk. */}
              <p className="mb-2 text-12 font-medium text-fg-muted">Markers</p>
              <ul className="flex flex-wrap gap-2">
                {visibleMarkers.map((marker) => (
                  <li key={`${String(marker.at)}-${marker.label}`}>
                    {markerAffordance(
                      marker,
                      <>
                        <StatusGlyph state={marker.state} size={10} />
                        <span className="text-fg">{marker.label}</span>
                      </>,
                      MARKER_CHIP_CLASS,
                    )}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : (
        <div className="relative min-w-0">
          {/* The keyboard's way into the readout. An application, not the image itself: a
              screen reader in browse mode keeps the arrow keys for itself on anything else,
              and they are what steps from sample to sample here. jsx-a11y does not count
              "application" as interactive, though it is exactly the role for this. */}
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
          <div
            role="application"
            aria-roledescription="chart"
            aria-label={title}
            aria-describedby={hintId}
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
            tabIndex={0}
            onKeyDown={onKeyDown}
            onBlur={() => {
              if (spoken !== "") moveCursor(null);
            }}
            className="rounded-control focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          >
            <div ref={hostRef} role="img" aria-label={summary} className="min-w-0" style={{ height }} />
          </div>
          <span id={hintId} className="sr-only">
            {`Left and right arrow keys step through the samples, Home and End go to the first and last, Escape clears.${zoomable ? " Drag across the chart with a pointer to zoom, or use the zoom buttons." : ""}`}
          </span>
          {positions.length > 0 ? (
            <div className="pointer-events-none absolute inset-0 z-10">
              {positions.map(({ marker, left, top }) => (
                <div
                  key={`${String(marker.at)}-${marker.label}`}
                  // Centred on the hairline and on the plot's top edge. A translate, not a
                  // negative offset: the inline left/top would override an offset class.
                  className="pointer-events-auto absolute -translate-x-1/2 -translate-y-1/2"
                  style={{ left, top }}
                >
                  <Tooltip content={marker.label}>
                    {markerAffordance(marker, <StatusGlyph state={marker.state} size={10} />, MARKER_ICON_CLASS)}
                  </Tooltip>
                </div>
              ))}
            </div>
          ) : null}
        </div>
      )}
    </>
  );
}

/** How many of `timestamps` fall within [start, end]. */
function timestampsInside(timestamps: ArrayLike<number>, start: number, end: number): number {
  let count = 0;
  for (const t of Array.from(timestamps)) {
    if (t >= start && t <= end) count += 1;
  }
  return count;
}

/**
 * The window after zooming in (half the span) or out (twice it) around the middle of what is
 * shown, kept within the data; null once it covers all of it again. Zooming in stops at a
 * handful of samples, where there is nothing more to see.
 */
export function zoomStep(timestamps: readonly number[], current: ChartWindow | null, direction: "in" | "out"): ChartWindow | null {
  const first = timestamps[0];
  const last = timestamps.at(-1);
  if (first === undefined || last === undefined || last <= first) return current;
  const [start, end] = current ?? [first, last];
  const middle = (start + end) / 2;
  const span = direction === "in" ? (end - start) / 2 : (end - start) * 2;
  if (span >= last - first) return null;
  let next: [number, number] = [middle - span / 2, middle + span / 2];
  if (next[0] < first) next = [first, first + span];
  if (next[1] > last) next = [last - span, last];
  if (direction === "in" && timestampsInside(timestamps, next[0], next[1]) < 4) return current;
  return next;
}

/** A dialog's chart fills what the viewport leaves under the dialog's header and controls. */
function expandedHeight(): number {
  return Math.round(Math.min(560, Math.max(240, window.innerHeight * 0.88 - 300)));
}

interface ChartDialogProps extends Omit<ChartBodyProps, "asTable" | "tableId" | "zoom" | "onZoom" | "height"> {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  description: string | undefined;
  rangeSelector: ChartRangeSelector | undefined;
}

/** The chart enlarged: the page's range, zoom on the time axis, the readout and the table. */
function ChartDialog({ open, onOpenChange, description, rangeSelector, ...body }: ChartDialogProps) {
  const [asTable, setAsTable] = useState(false);
  // The zoom belongs to the range it was made in: a new range starts whole.
  const [zoom, setZoom] = useState<{ range: string; window: ChartWindow } | null>(null);
  const tableId = useId();
  const rangeKey = rangeSelector?.value ?? "";
  const active = zoom !== null && zoom.range === rangeKey ? zoom.window : null;
  const [height] = useState(expandedHeight);

  const apply = (window: ChartWindow | null): void => {
    setZoom(window === null ? null : { range: rangeKey, window });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={body.title} description={description} size="xl">
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>{rangeSelector?.control}</div>
          <div className="flex flex-wrap items-center gap-1">
            <IconButton
              label="Zoom in"
              icon={<ZoomIn />}
              size="sm"
              disabled={asTable}
              onClick={() => {
                apply(zoomStep(body.timestamps, active, "in"));
              }}
            />
            <IconButton
              label="Zoom out"
              icon={<ZoomOut />}
              size="sm"
              disabled={asTable || active === null}
              onClick={() => {
                apply(zoomStep(body.timestamps, active, "out"));
              }}
            />
            <Button
              size="sm"
              variant="ghost"
              disabled={active === null}
              onClick={() => {
                apply(null);
              }}
            >
              Reset zoom
            </Button>
            <Button
              size="sm"
              variant="ghost"
              aria-pressed={asTable}
              aria-controls={asTable ? tableId : undefined}
              onClick={() => {
                setAsTable((v) => !v);
              }}
            >
              View as table
            </Button>
          </div>
        </div>
        <ChartBody {...body} height={height} asTable={asTable} tableId={tableId} zoom={active} onZoom={apply} />
        <p className="text-12 text-pretty text-fg-faint">
          {active === null
            ? "Drag across the chart to zoom into a stretch of time."
            : `Showing ${formatChartTime(active[0], body.withDate)} to ${formatChartTime(active[1], body.withDate)}. Double-click the chart or reset the zoom to see all of it.`}
        </p>
      </div>
    </Dialog>
  );
}

/**
 * A time series drawn with uPlot. The canvas is an image with a written summary; the same
 * numbers are one press away as a table, for screen readers and for anyone who wants them.
 * Hovering (or stepping with the arrow keys) reads out one sample; Expand opens it large, with
 * the page's range and zoom. Markers (deploys, typically) are drawn by the chart itself: a
 * hairline on the canvas and a focusable state glyph positioned from uPlot's own geometry.
 */
export function Chart({
  title,
  description,
  timestamps,
  series,
  markers,
  formatValue = defaultFormat,
  yRange,
  timeFormat,
  height = 160,
  rangeSelector,
  className,
}: ChartProps) {
  const [asTable, setAsTable] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const tableId = useId();

  const withDate = timeFormat ? timeFormat === "date" : needsDateFormat(timestamps);
  const from = timestamps[0];
  const to = timestamps.at(-1);
  const visibleMarkers = markers !== undefined && from !== undefined && to !== undefined ? markersInRange(markers, from, to) : [];
  const summary = summarise(title, description, series, formatValue, markers !== undefined ? visibleMarkers.length : undefined);
  const shared = { title, summary, timestamps, series, markers, formatValue, yRange, withDate };

  return (
    <figure className={cx("flex min-w-0 flex-col gap-3", className)}>
      <figcaption className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-13 font-medium text-fg">{title}</div>
          {description !== undefined ? <div className="text-12 text-fg-faint">{description}</div> : null}
        </div>
        <div className="-mr-2 flex shrink-0 items-center gap-0.5">
          <IconButton
            label={`Expand ${title}`}
            icon={<Maximize2 />}
            size="sm"
            onClick={() => {
              setExpanded(true);
            }}
          />
          <Button
            size="sm"
            variant="ghost"
            aria-pressed={asTable}
            aria-controls={asTable ? tableId : undefined}
            onClick={() => {
              setAsTable((v) => !v);
            }}
          >
            View as table
          </Button>
        </div>
      </figcaption>

      <ChartBody {...shared} height={height} asTable={asTable} tableId={tableId} zoom={null} />

      {expanded ? (
        <ChartDialog
          {...shared}
          open={expanded}
          onOpenChange={setExpanded}
          description={description}
          rangeSelector={rangeSelector}
        />
      ) : null}
    </figure>
  );
}
