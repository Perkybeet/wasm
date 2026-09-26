import { useState } from "react";

import { cx } from "../../lib/cx";

export type Status = "running" | "deploying" | "warning" | "failed" | "stopped" | "static" | "unknown";

type Glyph = "dot" | "arc" | "triangle" | "cross" | "ring" | "square" | "question";
type Tone = "ok" | "warn" | "fail" | "idle";

interface StatusSpec {
  label: string;
  tone: Tone;
  glyph: Glyph;
}

/**
 * Every state is told three ways: colour, shape and word. The shapes are distinct in
 * silhouette so the state survives greyscale, colour blindness and a glance.
 */
export const STATUS: Record<Status, StatusSpec> = {
  running: { label: "Running", tone: "ok", glyph: "dot" },
  deploying: { label: "Deploying", tone: "warn", glyph: "arc" },
  // Amber like work in progress, but still: something to look at (an expiring certificate, a
  // health check that warns), which a spinning arc would misread as "busy".
  warning: { label: "Warning", tone: "warn", glyph: "triangle" },
  failed: { label: "Failed", tone: "fail", glyph: "cross" },
  stopped: { label: "Stopped", tone: "idle", glyph: "ring" },
  static: { label: "Static", tone: "ok", glyph: "square" },
  unknown: { label: "Unknown", tone: "idle", glyph: "question" },
};

export const TONE_TEXT: Record<Tone, string> = {
  ok: "text-ok",
  warn: "text-warn",
  fail: "text-fail",
  idle: "text-idle",
};

const TONE_SOFT: Record<Tone, string> = {
  ok: "bg-ok-soft",
  warn: "bg-warn-soft",
  fail: "bg-fail-soft",
  idle: "bg-idle-soft",
};

/** The text colour of a state, for a glyph or word drawn outside a pill. */
export function stateTextClass(state: Status): string {
  return TONE_TEXT[STATUS[state].tone];
}

/** The glyph alone, for places that already print the state as text nearby. */
export function StatusGlyph({ state, size = 12, className }: { state: Status; size?: number; className?: string }) {
  const { glyph } = STATUS[state];
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 12 12"
      fill="none"
      aria-hidden="true"
      data-glyph={glyph}
      className={cx("shrink-0", glyph === "arc" && "animate-spin", className)}
    >
      {glyph === "dot" && <circle cx="6" cy="6" r="3.5" fill="currentColor" />}
      {glyph === "ring" && <circle cx="6" cy="6" r="3.25" stroke="currentColor" strokeWidth="1.5" />}
      {glyph === "square" && <rect x="2.75" y="2.75" width="6.5" height="6.5" rx="1" fill="currentColor" />}
      {glyph === "arc" && (
        <path d="M6 2.25A3.75 3.75 0 1 1 2.25 6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
      )}
      {glyph === "triangle" && (
        <>
          <path d="M6 1.9 10.6 10H1.4Z" stroke="currentColor" strokeWidth="1.3" strokeLinejoin="round" />
          <path d="M6 5.1v2" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" />
          <circle cx="6" cy="8.55" r="0.7" fill="currentColor" />
        </>
      )}
      {glyph === "cross" && (
        <path d="M3.25 3.25l5.5 5.5M8.75 3.25l-5.5 5.5" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" />
      )}
      {glyph === "question" && (
        <>
          <path
            d="M4.4 4.6a1.65 1.65 0 1 1 2.4 1.47c-.5.26-.8.6-.8 1.13v.2"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
          />
          <circle cx="6" cy="9.35" r="0.85" fill="currentColor" />
        </>
      )}
    </svg>
  );
}

export interface StatusPillProps {
  state: Status;
  /** Replaces the default word when the context is more specific ("Building", "Renewing"). */
  label?: string;
  /** `pill` sits on its soft ground; `inline` is glyph and word only, for dense rows. */
  appearance?: "pill" | "inline";
  size?: "sm" | "md";
  className?: string;
}

/** The state of an app, service, deployment or certificate. */
export function StatusPill({ state, label, appearance = "pill", size = "md", className }: StatusPillProps) {
  const spec = STATUS[state];

  // A change of state is the most important event on screen: it pulses once. The first render
  // is not a change, so it does not.
  const [shown, setShown] = useState(state);
  const [changes, setChanges] = useState(0);
  if (shown !== state) {
    setShown(state);
    setChanges((n) => n + 1);
  }

  return (
    <span
      key={changes}
      data-state={state}
      className={cx(
        "inline-flex shrink-0 items-center font-medium whitespace-nowrap",
        TONE_TEXT[spec.tone],
        appearance === "pill" && ["rounded-pill", TONE_SOFT[spec.tone]].join(" "),
        appearance === "pill" && (size === "sm" ? "h-5 gap-1 pr-2 pl-1.5 text-12" : "h-6 gap-1.5 pr-2.5 pl-2 text-12"),
        appearance === "inline" && (size === "sm" ? "gap-1 text-12" : "gap-1.5 text-13"),
        changes > 0 && "animate-pulse-once",
        className,
      )}
    >
      <StatusGlyph state={state} size={size === "sm" ? 10 : 12} />
      {label ?? spec.label}
    </span>
  );
}
