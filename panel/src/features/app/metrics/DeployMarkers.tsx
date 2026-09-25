import { Link } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { deployStatus } from "../../../components/page/status";
import { STATUS, StatusGlyph } from "../../../components/ui/StatusPill";
import { Tooltip } from "../../../components/ui/Tooltip";
import { cx } from "../../../lib/cx";

export interface DeployMark {
  id: number;
  /** Unix seconds. */
  at: number;
  status: string;
  /** When, as short as the range allows: "19:42", "Sep 25, 19:42". */
  when: string;
  /** Everything, for assistive technology and the tooltip: "Deploy 25, succeeded, Sep 25, 19:42". */
  label: string;
}

interface Box {
  left: number;
  top: number;
  width: number;
  height: number;
}

const TONE_TEXT = { ok: "text-ok", warn: "text-warn", fail: "text-fail", idle: "text-idle" } as const;

/**
 * A chart with the app's deploys drawn over it: a hairline at each deploy's time and, at the top
 * of the plot, its state glyph as a link to the deploy. Positioned over the plot area the chart
 * draws, measured from the chart itself, so the marks follow its axis width and its resizes.
 *
 * The chart takes no overlays of its own; this reads the plot box uPlot lays out (`.u-over`).
 * When the chart shows its table instead, there is no plot and no marks: the deploys are listed
 * under the charts in words.
 */
export function DeployMarkers({
  children,
  domain,
  marks,
  from,
  to,
}: {
  children: ReactNode;
  domain: string;
  marks: readonly DeployMark[];
  /** The first and last time on the chart's axis, Unix seconds. */
  from: number;
  to: number;
}) {
  const wrap = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState<Box | null>(null);

  useEffect(() => {
    const host = wrap.current;
    if (!host) return;
    let observed: Element | null = null;
    const measure = (): void => {
      const over = host.querySelector(".u-over");
      if (over !== observed) {
        if (observed) sizes.unobserve(observed);
        if (over) sizes.observe(over);
        observed = over;
      }
      if (!over) {
        setBox(null);
        return;
      }
      const outer = host.getBoundingClientRect();
      const inner = over.getBoundingClientRect();
      setBox({ left: inner.left - outer.left, top: inner.top - outer.top, width: inner.width, height: inner.height });
    };
    // Both fire once when they start observing, which takes the first measure.
    const sizes = new ResizeObserver(measure);
    sizes.observe(host);
    // The plot is created after the first paint and replaced by a table on request.
    const changes = new MutationObserver(measure);
    changes.observe(host, { childList: true, subtree: true });
    return () => {
      sizes.disconnect();
      changes.disconnect();
    };
  }, []);

  const span = to - from;
  const visible = box !== null && span > 0 ? marks.filter((mark) => mark.at >= from && mark.at <= to) : [];

  return (
    <div ref={wrap} className="relative min-w-0">
      {children}
      {box !== null && visible.length > 0 ? (
        <div
          className="pointer-events-none absolute"
          style={{ left: box.left, top: box.top, width: box.width, height: box.height }}
        >
          {visible.map((mark) => {
            const view = deployStatus(mark.status);
            const x = ((mark.at - from) / span) * box.width;
            return (
              <div key={mark.id} className="absolute top-0 bottom-0" style={{ left: x }}>
                <span aria-hidden="true" className="absolute top-3 bottom-0 left-0 border-l border-dashed border-border-strong" />
                <Tooltip content={mark.label}>
                  <Link
                    to="/apps/$domain/deployments/$id"
                    params={{ domain, id: String(mark.id) }}
                    aria-label={mark.label}
                    className={cx(
                      "pointer-events-auto absolute -top-3 -left-3 flex size-6 items-center justify-center rounded-pill bg-surface",
                      "hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-focus",
                      TONE_TEXT[STATUS[view.state].tone],
                    )}
                  >
                    <StatusGlyph state={view.state} size={10} />
                  </Link>
                </Tooltip>
              </div>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
