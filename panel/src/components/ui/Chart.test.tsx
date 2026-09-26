import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { expectNoAxeViolations } from "../../test/axe";
import type { ChartMarker, MarkerPlot } from "./Chart";
import {
  Chart,
  continuing,
  formatChartTime,
  markersInRange,
  needsDateFormat,
  positionMarkers,
  readoutWords,
  valueAxisSize,
  visibleBounds,
  zoomStep,
} from "./Chart";

// uPlot draws on a canvas, which jsdom does not implement; the wrapper's contract is the
// accessible summary, the readout, the table, the marker overlay and the lifecycle of the
// plot, all testable without pixels. The fake plot below implements just enough of uPlot's
// own geometry (`bbox`, `valToPos`, `posToVal`, the x scale) and hooks (`draw`, `setCursor`,
// `setSelect`) for the chart to position markers, read the cursor and zoom for real.
interface FakePlot {
  options: Record<string, unknown>;
  data: unknown;
  scales: { x: { min?: number; max?: number } };
  cursor: { idx: number | null };
  select: { left: number; top: number; width: number; height: number };
  bbox: { left: number; top: number; width: number; height: number };
  destroy: () => void;
  setData: (d: unknown) => void;
  setScale: ReturnType<typeof vi.fn>;
  setCursor: ReturnType<typeof vi.fn>;
  setSelect: ReturnType<typeof vi.fn>;
  valToPos: (val: number, scale?: string) => number;
  /** Fires one of the plot's hooks, as uPlot would. */
  fire: (hook: "draw" | "setCursor" | "setSelect") => void;
}

const plots = vi.hoisted(() => [] as FakePlot[]);

vi.mock("uplot", () => {
  const bbox = { left: 40, top: 8, width: 400, height: 144 };

  function build(options: Record<string, unknown>, initialData: unknown) {
    let current = initialData as number[][];
    const hooks = (options["hooks"] ?? {}) as Record<string, ((u: unknown) => void)[] | undefined>;
    const scales = { x: { min: current[0]?.[0], max: current[0]?.at(-1) } };
    const span = () => {
      const min = scales.x.min ?? 0;
      const max = scales.x.max ?? min + 1;
      return { min, max };
    };
    const valToPos = (val: number): number => {
      const { min, max } = span();
      const frac = max > min ? (val - min) / (max - min) : 0;
      return bbox.left + frac * bbox.width;
    };
    const posToVal = (pos: number): number => {
      const { min, max } = span();
      return min + ((pos - bbox.left) / bbox.width) * (max - min);
    };
    const ctx = {
      save: vi.fn(),
      restore: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      stroke: vi.fn(),
      setLineDash: vi.fn(),
    };
    const fire = (hook: string): void => {
      (hooks[hook] ?? []).forEach((fn) => {
        fn(plot);
      });
    };
    const plot = {
      options,
      data: current,
      bbox,
      ctx,
      scales,
      cursor: { idx: null as number | null },
      select: { left: 0, top: 0, width: 0, height: 0 },
      valToPos: (val: number) => valToPos(val),
      posToVal: (pos: number) => posToVal(pos),
      destroy: vi.fn(),
      setSize: vi.fn(),
      redraw: vi.fn(),
      setCursor: vi.fn(),
      setSelect: vi.fn(),
      setScale: vi.fn((_key: string, next: { min: number; max: number }) => {
        scales.x = { min: next.min, max: next.max };
        fire("draw");
      }),
      setData: vi.fn((next: unknown) => {
        current = next as number[][];
        plot.data = current;
        scales.x = { min: current[0]?.[0], max: current[0]?.at(-1) };
        fire("draw");
      }),
      fire,
    };
    plots.push(plot as unknown as FakePlot);
    fire("draw");
    return plot;
  }

  const ctor = vi.fn(function (this: unknown, options: Record<string, unknown>, data: unknown) {
    return build(options, data);
  }) as unknown as { new (...args: unknown[]): unknown; pxRatio: number };
  ctor.pxRatio = 1;
  return { default: ctor };
});

const T0 = Date.UTC(2026, 8, 25, 14, 0) / 1000;
const TIMES = [T0, T0 + 60, T0 + 120];

beforeEach(() => {
  plots.length = 0;
});

function Example() {
  return (
    <Chart
      title="CPU"
      description="Last 3 minutes"
      timestamps={TIMES}
      series={[{ label: "shop.example.com", values: [12, 48, 30] }]}
      formatValue={(v) => `${String(v)}%`}
      yRange={[0, 100]}
    />
  );
}

describe("Chart", () => {
  it("is an image with a written summary of every series", () => {
    render(<Example />);
    const image = screen.getByRole("img");
    expect(image).toHaveAccessibleName(
      "CPU, last 3 minutes. shop.example.com: latest 30%, low 12%, high 48%.",
    );
  });

  it("draws the data with uPlot and destroys the plot when it goes", () => {
    const { unmount } = render(<Example />);
    expect(plots.length).toBeGreaterThan(0);
    const plot = pagePlot();
    expect(plot.data).toEqual([TIMES, [12, 48, 30]]);
    unmount();
    expect(plot.destroy).toHaveBeenCalled();
  });

  it("shows the latest value of each series in the legend", () => {
    render(<Example />);
    expect(within(screen.getByRole("list", { name: "Series" })).getByText("30%")).toBeInTheDocument();
  });

  it("turns into a table of the same numbers, newest first", async () => {
    render(<Example />);
    const toggle = screen.getByRole("button", { name: "View as table" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    const table = screen.getByRole("table", { name: "CPU, newest first" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[1]?.textContent)).toEqual(["30%", "48%", "12%"]);
    await userEvent.click(toggle);
    expect(screen.getByRole("img")).toBeInTheDocument();
  });

  it("has no accessibility violations as a chart or as a table", async () => {
    const { container } = render(<Example />);
    await expectNoAxeViolations(container);
    await userEvent.click(screen.getByRole("button", { name: "View as table" }));
    await expectNoAxeViolations(container);
  });
});

describe("Chart markers", () => {
  const inRange: ChartMarker = {
    at: T0 + 60,
    label: "Deploy 25, succeeded, 14:01",
    state: "running",
    href: "https://wasm.example.com/deploys/25",
  };
  const outOfRange: ChartMarker = {
    at: T0 - 600,
    label: "Deploy 9, failed, 13:50",
    state: "failed",
    href: "https://wasm.example.com/deploys/9",
  };

  function WithMarkers({ markers }: { markers: readonly ChartMarker[] }) {
    return (
      <Chart
        title="CPU"
        timestamps={TIMES}
        series={[{ label: "shop.example.com", values: [12, 48, 30] }]}
        formatValue={(v) => `${String(v)}%`}
        markers={markers}
      />
    );
  }

  it("draws an in-range marker as a focusable link with its full accessible name", () => {
    render(<WithMarkers markers={[inRange]} />);
    const link = screen.getByRole("link", { name: inRange.label });
    expect(link).toHaveAttribute("href", "https://wasm.example.com/deploys/25");
  });

  it("does not draw a marker outside the time range", () => {
    render(<WithMarkers markers={[outOfRange]} />);
    expect(screen.queryByRole("link", { name: outOfRange.label })).not.toBeInTheDocument();
  });

  it("mentions how many markers are in view in the accessible summary", () => {
    render(<WithMarkers markers={[inRange, outOfRange]} />);
    expect(screen.getByRole("img")).toHaveAccessibleName(/1 marker in view\.$/);
  });

  it("says zero markers when none of them fall in the range", () => {
    render(<WithMarkers markers={[outOfRange]} />);
    expect(screen.getByRole("img")).toHaveAccessibleName(/0 markers in view\.$/);
  });

  it("says nothing about markers when the prop is not used", () => {
    render(<Example />);
    expect(screen.getByRole("img")).not.toHaveAccessibleName(/marker/);
  });

  it("uses renderMarker to wrap the affordance, keeping Chart free of a router", () => {
    const marker: ChartMarker = {
      at: T0 + 60,
      label: "Deploy 25, succeeded, 14:01",
      state: "running",
      renderMarker: (m, children, linkProps) => (
        <button type="button" data-testid="custom-marker" aria-label={m.label} className={linkProps.className} style={linkProps.style}>
          {children}
        </button>
      ),
    };
    render(<WithMarkers markers={[marker]} />);
    expect(screen.getByTestId("custom-marker")).toHaveAccessibleName(marker.label);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("falls back to a real, natively focusable control with neither href nor renderMarker", () => {
    const marker: ChartMarker = { at: T0 + 60, label: "Release 9, unlinked", state: "stopped" };
    render(<WithMarkers markers={[marker]} />);
    expect(screen.getByRole("button", { name: "Release 9, unlinked" })).toBeInTheDocument();
  });

  it("falls back to an aria-disabled control rather than a link for an href that is not http(s)", () => {
    const marker: ChartMarker = {
      at: T0 + 60,
      label: "Deploy 25, succeeded, 14:01",
      state: "running",
      href: "javascript:alert(1)",
    };
    render(<WithMarkers markers={[marker]} />);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    const button = screen.getByRole("button", { name: marker.label });
    expect(button).toHaveAttribute("aria-disabled", "true");
  });

  it("keeps markers in the table view so they do not vanish for screen reader users", async () => {
    render(<WithMarkers markers={[inRange, outOfRange]} />);
    await userEvent.click(screen.getByRole("button", { name: "View as table" }));
    // Listed below the table itself (not inside its scrolling region, so a sighted user
    // never has to scroll a small box to find them) but still reachable in the document.
    expect(screen.getByRole("region", { name: "CPU data" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: inRange.label })).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: outOfRange.label })).not.toBeInTheDocument();
  });

  it("has no accessibility violations with markers, as a chart or as a table", async () => {
    const { container } = render(<WithMarkers markers={[inRange, outOfRange]} />);
    await expectNoAxeViolations(container);
    await userEvent.click(screen.getByRole("button", { name: "View as table" }));
    await expectNoAxeViolations(container);
  });
});

/** The plot on the page: the first one built (the enlarged chart builds its own). */
function pagePlot(): FakePlot {
  const plot = plots[0];
  if (plot === undefined) throw new Error("no plot was built");
  return plot;
}

/** Puts uPlot's cursor on a sample (null: the pointer left) and fires its hook. */
function hover(plot: FakePlot, idx: number | null): void {
  act(() => {
    plot.cursor.idx = idx;
    plot.fire("setCursor");
  });
}

function readoutTime(): HTMLElement {
  const time = document.querySelector<HTMLElement>("[data-readout-time]");
  if (time === null) throw new Error("the readout has no time");
  return time;
}

describe("Chart readout", () => {
  it("shows the latest values when nothing is hovered", () => {
    render(<Example />);
    expect(readoutTime()).toHaveTextContent("Latest");
    expect(within(screen.getByRole("list", { name: "Series" })).getByText("30%")).toBeInTheDocument();
  });

  it("follows uPlot's cursor: the hovered sample's time and every series' value", () => {
    render(<Example />);
    hover(pagePlot(), 1);
    const series = screen.getByRole("list", { name: "Series" });
    expect(within(series).getByText("48%")).toBeInTheDocument();
    expect(within(series).queryByText("30%")).not.toBeInTheDocument();
    // The clock on screen, the full moment for assistive technology, the ISO one in markup.
    const time = readoutTime().querySelector("time");
    expect(time).toHaveAttribute("dateTime", new Date((T0 + 60) * 1000).toISOString());
    expect(time?.querySelector('[aria-hidden="true"]')).toHaveTextContent(formatChartTime(T0 + 60, false));
    expect(time?.querySelector(".sr-only")?.textContent).toMatch(/\d{4}/);
  });

  it("goes back to the latest value when the cursor leaves", () => {
    render(<Example />);
    hover(pagePlot(), 0);
    expect(within(screen.getByRole("list", { name: "Series" })).getByText("12%")).toBeInTheDocument();
    hover(pagePlot(), null);
    expect(readoutTime()).toHaveTextContent("Latest");
    expect(within(screen.getByRole("list", { name: "Series" })).getByText("30%")).toBeInTheDocument();
  });

  it("formats every series with the chart's formatter, and a gap as a dash", () => {
    render(
      <Chart
        title="Network"
        timestamps={TIMES}
        series={[
          { label: "In", values: [1, null, 3] },
          { label: "Out", values: [4, 5, 6] },
        ]}
        formatValue={(v) => `${String(v)} KB/s`}
      />,
    );
    hover(pagePlot(), 1);
    const items = within(screen.getByRole("list", { name: "Series" })).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual(["In-", "Out5 KB/s"]);
  });
});

describe("Chart keyboard", () => {
  it("is one tab stop, named by the title and described by its keys", () => {
    render(<Example />);
    const chart = screen.getByRole("application", { name: "CPU" });
    expect(chart).toHaveAttribute("tabindex", "0");
    expect(chart).toHaveAccessibleDescription(/Left and right arrow keys/);
  });

  it("steps sample by sample, jumps to the ends and clears with Escape", async () => {
    const user = userEvent.setup();
    render(<Example />);
    const series = () => within(screen.getByRole("list", { name: "Series" }));
    screen.getByRole("application", { name: "CPU" }).focus();

    await user.keyboard("{ArrowLeft}");
    // From nothing, the first step lands on the newest sample, the one already shown.
    expect(readoutTime()).toHaveTextContent(formatChartTime(T0 + 120, false));
    await user.keyboard("{ArrowLeft}");
    expect(series().getByText("48%")).toBeInTheDocument();
    await user.keyboard("{Home}");
    expect(series().getByText("12%")).toBeInTheDocument();
    await user.keyboard("{ArrowLeft}");
    expect(series().getByText("12%")).toBeInTheDocument();
    await user.keyboard("{ArrowRight}");
    expect(series().getByText("48%")).toBeInTheDocument();
    await user.keyboard("{End}");
    expect(series().getByText("30%")).toBeInTheDocument();
    expect(readoutTime()).not.toHaveTextContent("Latest");
    await user.keyboard("{Escape}");
    expect(readoutTime()).toHaveTextContent("Latest");
  });

  it("moves uPlot's own cursor onto the sample", async () => {
    const user = userEvent.setup();
    render(<Example />);
    screen.getByRole("application", { name: "CPU" }).focus();
    await user.keyboard("{Home}");
    const plot = pagePlot();
    expect(plot.setCursor).toHaveBeenLastCalledWith(expect.objectContaining({ left: plot.valToPos(T0, "x") }));
    await user.keyboard("{Escape}");
    expect(plot.setCursor).toHaveBeenLastCalledWith({ left: -10, top: -10 });
  });

  it("says the sample in a polite live region, in the chart's own words", async () => {
    const user = userEvent.setup();
    render(<Example />);
    screen.getByRole("application", { name: "CPU" }).focus();
    await user.keyboard("{Home}");
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    await waitFor(() => {
      expect(status).toHaveTextContent(`${formatChartTime(T0, false)}, shop.example.com 12%`);
    });
  });

  it("throttles the announcements: a held key says the newest sample, not every one", () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    try {
      render(<Example />);
      const chart = screen.getByRole("application", { name: "CPU" });
      const status = screen.getByRole("status");
      act(() => {
        chart.focus();
      });
      const press = (key: string) => {
        act(() => {
          chart.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
        });
      };
      press("Home");
      act(() => {
        vi.advanceTimersByTime(0);
      });
      expect(status).toHaveTextContent(/12%$/);
      press("ArrowRight");
      press("ArrowRight");
      // Inside the window: nothing new yet.
      act(() => {
        vi.advanceTimersByTime(100);
      });
      expect(status).toHaveTextContent(/12%$/);
      act(() => {
        vi.advanceTimersByTime(400);
      });
      expect(status).toHaveTextContent(/30%$/);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("Chart expanded", () => {
  function Expandable({ onRange }: { onRange?: (value: string) => void }) {
    return (
      <Chart
        title="CPU"
        description="Last 3 minutes"
        timestamps={TIMES}
        series={[{ label: "shop.example.com", values: [12, 48, 30] }]}
        formatValue={(v) => `${String(v)}%`}
        yRange={[0, 100]}
        rangeSelector={{
          value: "1h",
          control: (
            <button type="button" onClick={() => onRange?.("24h")}>
              Range stand-in
            </button>
          ),
        }}
      />
    );
  }

  async function expand() {
    const user = userEvent.setup();
    render(<Expandable />);
    await user.click(screen.getByRole("button", { name: "Expand CPU" }));
    const dialog = await screen.findByRole("dialog", { name: "CPU" });
    const plot = plots.at(-1);
    if (plot === undefined || plot === plots[0]) throw new Error("the enlarged chart built no plot of its own");
    return { user, dialog, plot };
  }

  it("opens a large dialog with the same series, the page's range and the readout", async () => {
    const { dialog, plot } = await expand();
    expect(dialog).toHaveAccessibleDescription("Last 3 minutes");
    expect(within(dialog).getByRole("button", { name: "Range stand-in" })).toBeInTheDocument();
    expect(plot.data).toEqual([TIMES, [12, 48, 30]]);
    expect(within(dialog).getByRole("img")).toHaveAccessibleName("CPU, last 3 minutes. shop.example.com: latest 30%, low 12%, high 48%.");
    hover(plot, 0);
    expect(within(within(dialog).getByRole("list", { name: "Series" })).getByText("12%")).toBeInTheDocument();
  });

  it("zooms to a dragged stretch of the time axis and resets", async () => {
    const { user, dialog, plot } = await expand();
    const reset = within(dialog).getByRole("button", { name: "Reset zoom" });
    expect(reset).toBeDisabled();
    // Drag from the first sample to the second: two samples, enough for a line.
    act(() => {
      plot.select = { left: plot.valToPos(T0), top: 0, width: plot.valToPos(T0 + 60) - plot.valToPos(T0), height: 100 };
      plot.fire("setSelect");
    });
    await waitFor(() => {
      expect(plot.setScale).toHaveBeenLastCalledWith("x", { min: T0, max: T0 + 60 });
    });
    expect(plot.setSelect).toHaveBeenCalledWith({ left: 0, top: 0, width: 0, height: 0 }, false);
    expect(reset).toBeEnabled();
    await user.click(reset);
    expect(reset).toBeDisabled();
    expect(plot.scales.x).toEqual({ min: T0, max: T0 + 120 });
  });

  it("ignores a drag that covers fewer than two samples", async () => {
    const { dialog, plot } = await expand();
    act(() => {
      plot.select = { left: plot.valToPos(T0 + 10), top: 0, width: 20, height: 100 };
      plot.fire("setSelect");
    });
    expect(plot.setScale).not.toHaveBeenCalled();
    expect(within(dialog).getByRole("button", { name: "Reset zoom" })).toBeDisabled();
  });

  it("shows the enlarged chart as a table too", async () => {
    const { user, dialog } = await expand();
    await user.click(within(dialog).getByRole("button", { name: "View as table" }));
    const table = within(dialog).getByRole("table", { name: "CPU, newest first" });
    expect(within(table).getAllByRole("row")).toHaveLength(4);
  });

  it("closes with Escape and gives focus back to Expand", async () => {
    const { user } = await expand();
    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(screen.getByRole("button", { name: "Expand CPU" })).toHaveFocus();
  });

  it("has no accessibility violations, as a chart and as a table", async () => {
    const { user, dialog } = await expand();
    await expectNoAxeViolations(dialog);
    await user.click(within(dialog).getByRole("button", { name: "View as table" }));
    await expectNoAxeViolations(dialog);
  });
});

describe("readoutWords", () => {
  it("says the moment, then each series with its formatted value", () => {
    const local = new Date(2026, 8, 25, 14, 32).getTime() / 1000;
    const words = readoutWords(local, false, [{ label: "CPU", values: [12.4] }], 0, (v) => `${String(v)}%`);
    expect(words).toBe("14:32, CPU 12.4%");
  });

  it("says a gap in words", () => {
    expect(readoutWords(T0, false, [{ label: "In", values: [null] }], 0, String)).toMatch(/In no reading$/);
  });
});

describe("visibleBounds", () => {
  it("is every index without a window, and the indices inside one with it", () => {
    expect(visibleBounds(TIMES, null)).toEqual([0, 2]);
    expect(visibleBounds(TIMES, [T0 + 30, T0 + 120])).toEqual([1, 2]);
    expect(visibleBounds(TIMES, [T0 + 200, T0 + 300])).toBeNull();
    expect(visibleBounds([], null)).toBeNull();
  });
});

describe("zoomStep", () => {
  const minutes = Array.from({ length: 61 }, (_, i) => T0 + i * 60);

  it("halves the span around the middle, then doubles it back to all of it", () => {
    const first = zoomStep(minutes, null, "in");
    expect(first).toEqual([T0 + 900, T0 + 2700]);
    expect(zoomStep(minutes, first, "out")).toBeNull();
  });

  it("stays inside the data when zooming out near an end", () => {
    expect(zoomStep(minutes, [T0, T0 + 600], "out")).toEqual([T0, T0 + 1200]);
  });

  it("stops zooming in at a handful of samples", () => {
    const narrow = [T0, T0 + 180] as const;
    expect(zoomStep(minutes, narrow, "in")).toBe(narrow);
  });
});

describe("valueAxisSize", () => {
  // A canvas that measures 6.6px per character, like 11px JetBrains Mono.
  const ctx = { font: "", measureText: (text: string) => ({ width: text.length * 6.6 }) } as unknown as CanvasRenderingContext2D;

  it("fits the widest label, so a long unit is never clipped", () => {
    expect(valueAxisSize({ ctx }, ["0 B/s", "386 KB/s", "771 KB/s"])).toBe(Math.ceil(8 * 6.6 + 12));
    expect(ctx.font).toContain("JetBrains Mono");
  });

  it("keeps a minimum width for short labels and before the first draw", () => {
    expect(valueAxisSize({ ctx }, ["0%", "50%"])).toBe(40);
    expect(valueAxisSize({ ctx }, null)).toBe(40);
  });
});

describe("needsDateFormat", () => {
  const DAY = 86_400;

  it("stays a clock under about two days", () => {
    expect(needsDateFormat([T0, T0 + 3_600, T0 + 2 * DAY - 60])).toBe(false);
  });

  it("wants a date once the span passes about two days", () => {
    expect(needsDateFormat([T0, T0 + 7 * DAY])).toBe(true);
  });

  it("wants a date for a short span that crosses local midnight", () => {
    const before = new Date(2026, 8, 25, 23, 30).getTime() / 1000;
    const after = new Date(2026, 8, 26, 0, 30).getTime() / 1000;
    expect(needsDateFormat([before, after])).toBe(true);
  });

  it("stays a clock with fewer than two points", () => {
    expect(needsDateFormat([T0])).toBe(false);
    expect(needsDateFormat([])).toBe(false);
  });
});

describe("formatChartTime", () => {
  // Built from local Date fields, like formatChartTime itself: the string it produces is
  // then independent of the machine's own time zone.
  const local = new Date(2026, 8, 25, 14, 0).getTime() / 1000;

  it("prints a bare 24-hour clock without a date", () => {
    expect(formatChartTime(local, false)).toBe("14:00");
  });

  it("prints the date and the clock with a date", () => {
    expect(formatChartTime(local, true)).toBe("Sep 25 14:00");
  });

  it("prints the bare date at a tick that lands exactly on local midnight", () => {
    const midnight = new Date(2026, 8, 25, 0, 0).getTime() / 1000;
    expect(formatChartTime(midnight, true)).toBe("Sep 25");
  });
});

describe("markersInRange", () => {
  const markers: ChartMarker[] = [
    { at: 100, label: "a", state: "running" },
    { at: 200, label: "b", state: "running" },
    { at: 300, label: "c", state: "running" },
  ];

  it("keeps markers within the bounds, inclusive", () => {
    expect(markersInRange(markers, 100, 200).map((m) => m.label)).toEqual(["a", "b"]);
  });

  it("drops everything when the range is empty or inverted", () => {
    expect(markersInRange(markers, 500, 600)).toEqual([]);
    expect(markersInRange(markers, 300, 100)).toEqual([]);
  });
});

describe("positionMarkers", () => {
  // canvasPixels doubles the value (a stand-in for a pxRatio of 2); CSS pixels pass it through.
  const plot: MarkerPlot = {
    bbox: { left: 80, top: 20, width: 200, height: 100 },
    valToPos: (value, _scale, canvasPixels) => (canvasPixels ? value * 2 : value),
  };

  it("places each marker at its x, offset by the plot area's own left and top", () => {
    const markers: ChartMarker[] = [{ at: 50, label: "mid", state: "running" }];
    const [position] = positionMarkers(plot, markers, 2);
    // bbox is in canvas pixels; a pxRatio of 2 halves it to the CSS-pixel offset (40, 10).
    expect(position).toEqual({ marker: markers[0], left: 40 + 50, top: 10 });
  });

  it("returns one position per marker, in order", () => {
    const markers: ChartMarker[] = [
      { at: 0, label: "start", state: "running" },
      { at: 100, label: "end", state: "running" },
    ];
    expect(positionMarkers(plot, markers, 2).map((p) => p.marker.label)).toEqual(["start", "end"]);
  });
});

describe("continuing", () => {
  it("reads a description on after the title without shouting or doubling the full stop", () => {
    expect(continuing("Last 24 hours. Percent of one CPU.")).toBe("last 24 hours. Percent of one CPU");
    expect(continuing("Last hour, of 20 GB")).toBe("last hour, of 20 GB");
    expect(continuing("CPU of the unit")).toBe("CPU of the unit");
  });
});
