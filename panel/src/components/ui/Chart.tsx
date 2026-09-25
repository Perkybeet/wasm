import { useEffect, useId, useMemo, useRef, useState } from "react";
import uPlot from "uplot";

import { cx } from "../../lib/cx";
import { Button } from "./Button";

export interface ChartSeries {
  label: string;
  values: readonly (number | null)[];
}

export interface ChartProps {
  title: string;
  /** Window of the data in words, e.g. "Last 30 minutes". Part of the summary. */
  description?: string;
  /** Unix seconds, ascending, one per value of every series. */
  timestamps: readonly number[];
  /** Up to three series. The first is drawn in the accent; the others dashed and dotted. */
  series: readonly ChartSeries[];
  /** Formats a value for axis, legend, summary and table. */
  formatValue?: (value: number) => string;
  /** Fixes the value axis, e.g. [0, 100] for percentages. */
  yRange?: readonly [number, number];
  height?: number;
  className?: string;
}

interface Palette {
  series: string[];
  fill: string;
  grid: string;
  axis: string;
}

const DASHES: (number[] | undefined)[] = [undefined, [4, 3], [1.5, 3]];
const SERIES_TOKENS = ["--accent", "--text-muted", "--text-faint"];
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
  probe.remove();
  const accent = series[0] ?? "currentColor";
  const fill = accent.startsWith("rgb(") ? accent.replace("rgb(", "rgba(").replace(")", ", 0.08)") : "transparent";
  return { series, fill, grid, axis };
}

function formatTime(seconds: number): string {
  // 24-hour clock: shorter on the axis and the way server logs print time.
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
}

function summarise(
  title: string,
  description: string | undefined,
  series: readonly ChartSeries[],
  format: (value: number) => string,
): string {
  const parts = series.map((s) => {
    const values = s.values.filter((v): v is number => v !== null);
    if (values.length === 0) return `${s.label}: no data`;
    const latest = values.at(-1) ?? 0;
    return `${s.label}: latest ${format(latest)}, low ${format(Math.min(...values))}, high ${format(Math.max(...values))}`;
  });
  return `${title}${description ? `, ${description.toLowerCase()}` : ""}. ${parts.join("; ")}.`;
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

/**
 * A time series drawn with uPlot. The canvas is an image with a written summary; the same
 * numbers are one press away as a table, for screen readers and for anyone who wants them.
 */
export function Chart({
  title,
  description,
  timestamps,
  series,
  formatValue = (v) => v.toLocaleString(undefined, { maximumFractionDigits: 1 }),
  yRange,
  height = 160,
  className,
}: ChartProps) {
  const [asTable, setAsTable] = useState(false);
  const hostRef = useRef<HTMLDivElement>(null);
  const plotRef = useRef<uPlot | null>(null);
  const formatRef = useRef(formatValue);
  const tableId = useId();

  useEffect(() => {
    formatRef.current = formatValue;
  });

  const data = useMemo<uPlot.AlignedData>(
    () => [Array.from(timestamps), ...series.map((s) => Array.from(s.values))],
    [timestamps, series],
  );
  const labels = series.map((s) => s.label).join("\u0000");
  const summary = summarise(title, description, series, formatValue);

  useEffect(() => {
    const host = hostRef.current;
    if (!host || asTable) return;
    let palette = readPalette(host);
    const options: uPlot.Options = {
      width: Math.max(host.clientWidth, 240),
      height,
      legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { size: 6 } },
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
          values: (_u, splits) => splits.map((s) => formatTime(s)),
        },
        {
          stroke: () => palette.axis,
          font: AXIS_FONT,
          grid: { stroke: () => palette.grid, width: 1 },
          ticks: { show: false },
          size: 52,
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
    };
    // Data changes are applied below without rebuilding the plot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asTable, height, labels, yRange?.[0], yRange?.[1]]);

  useEffect(() => {
    plotRef.current?.setData(data);
  }, [data]);

  const latest = series.map((s) => s.values.findLast((v) => v !== null) ?? null);

  return (
    <figure className={cx("flex min-w-0 flex-col gap-3", className)}>
      <figcaption className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-13 font-medium text-fg">{title}</div>
          {description !== undefined ? <div className="text-12 text-fg-faint">{description}</div> : null}
        </div>
        <Button
          size="sm"
          variant="ghost"
          aria-pressed={asTable}
          aria-controls={asTable ? tableId : undefined}
          onClick={() => setAsTable((v) => !v)}
          className="-mr-2"
        >
          View as table
        </Button>
      </figcaption>

      <ul className="flex flex-wrap gap-x-4 gap-y-1" aria-label="Series">
        {series.map((s, index) => (
          <li key={s.label} className="flex items-center gap-1.5 text-12 text-fg-muted">
            <SeriesSwatch index={index} />
            <span>{s.label}</span>
            <span className="mono text-fg">{latest[index] !== null && latest[index] !== undefined ? formatValue(latest[index]) : "-"}</span>
          </li>
        ))}
      </ul>

      {asTable ? (
        <div
          id={tableId}
          role="region"
          aria-label={`${title} data`}
          tabIndex={0}
          className="overflow-auto rounded-control border border-border scroll-thin focus-visible:outline-2 focus-visible:outline-focus"
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
                .reverse()
                .map(({ t, i }) => (
                  <tr key={t} className="border-t border-border">
                    <td className="mono px-3 py-1 text-fg-muted">{formatTime(t)}</td>
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
      ) : (
        <div ref={hostRef} role="img" aria-label={summary} className="min-w-0" style={{ height }} />
      )}
    </figure>
  );
}
