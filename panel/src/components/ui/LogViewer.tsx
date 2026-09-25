import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDown, ArrowDownToLine, ChevronDown, ChevronUp, Download, Search, WrapText } from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";

import type { AnsiSegment, AnsiStyle } from "../../lib/ansi";
import { ansiClassName, parseAnsi } from "../../lib/ansi";
import { downloadText } from "../../lib/clipboard";
import { cx } from "../../lib/cx";
import { Button } from "./Button";
import { CopyButton } from "./CopyButton";
import { IconButton } from "./IconButton";
import { Input } from "./Input";

export interface LogLine {
  id: number;
  text: string;
  level?: "info" | "warn" | "error" | "debug";
  ts?: string;
}

export interface LogViewerProps {
  lines: readonly LogLine[];
  /** Start pinned to the newest line. Scrolling up pauses; reaching the bottom resumes. */
  follow?: boolean;
  /** Pixel height, or "fill" to take the height of a flex parent. */
  height?: number | "fill";
  searchable?: boolean;
  /** Called when the reader scrolls to the top, to prepend older lines. */
  onLoadMore?: () => void;
  /** Accessible name of the output region: "Build log for example.com". */
  label?: string;
  /** File name offered by the download button. */
  filename?: string;
  emptyMessage?: string;
  className?: string;
}

const ROW_HEIGHT = 20;
const BOTTOM_SLACK = 4;

interface ParsedLine {
  segments: AnsiSegment[];
  plain: string;
}

// Lines are immutable once received, so each is parsed once however often the view renders.
const parsedCache = new WeakMap<LogLine, ParsedLine>();

function parsed(line: LogLine): ParsedLine {
  let entry = parsedCache.get(line);
  if (!entry) {
    const segments = parseAnsi(line.text);
    entry = { segments, plain: segments.map((s) => s.text).join("") };
    parsedCache.set(line, entry);
  }
  return entry;
}

interface Match {
  line: number;
  start: number;
  end: number;
}

/** Every case-insensitive occurrence of the needle, in reading order. */
function findMatches(lines: readonly LogLine[], needle: string): Match[] {
  if (needle === "") return [];
  const found: Match[] = [];
  lines.forEach((line, index) => {
    const haystack = parsed(line).plain.toLowerCase();
    let from = 0;
    for (;;) {
      const at = haystack.indexOf(needle, from);
      if (at === -1) break;
      found.push({ line: index, start: at, end: at + needle.length });
      from = at + needle.length;
    }
  });
  return found;
}

interface Range {
  start: number;
  end: number;
  current: boolean;
}

/** A highlight keeps the program's weight and slant but takes the match colours for contrast. */
function withoutColour(style: AnsiStyle): AnsiStyle {
  return {
    ...(style.bold ? { bold: true } : {}),
    ...(style.italic ? { italic: true } : {}),
    ...(style.underline ? { underline: true } : {}),
    ...(style.strike ? { strike: true } : {}),
  };
}

function renderSegments(segments: readonly AnsiSegment[], ranges: readonly Range[]): ReactNode[] {
  const out: ReactNode[] = [];
  let offset = 0;
  segments.forEach((segment, s) => {
    const cls = ansiClassName(segment.style);
    const piece = (key: string, text: string): ReactNode =>
      cls === "" ? text : (
        <span key={key} className={cls}>
          {text}
        </span>
      );
    const start = offset;
    const end = offset + segment.text.length;
    let cursor = start;
    for (const range of ranges) {
      if (range.end <= cursor || range.start >= end) continue;
      const from = Math.max(range.start, cursor);
      const to = Math.min(range.end, end);
      if (from > cursor) out.push(piece(`${String(s)}-${String(cursor)}`, segment.text.slice(cursor - start, from - start)));
      out.push(
        <mark
          key={`${String(s)}-${String(from)}-m`}
          data-current={range.current ? "" : undefined}
          className={cx(
            ansiClassName(withoutColour(segment.style)),
            "rounded-[2px]",
            range.current ? "bg-accent text-on-accent" : "bg-match text-fg",
          )}
        >
          {segment.text.slice(from - start, to - start)}
        </mark>,
      );
      cursor = to;
    }
    if (cursor < end) out.push(piece(`${String(s)}-${String(cursor)}`, segment.text.slice(cursor - start)));
    offset = end;
  });
  return out;
}

type Level = NonNullable<LogLine["level"]>;

// Rows carry an opaque ground so the sticky line-number gutter can inherit it when the
// reader scrolls sideways; the state rail sits on the gutter, which is always in view.
const LEVEL_ROW: Record<Level, string> = {
  info: "bg-bg-sunken",
  debug: "bg-bg-sunken text-fg-muted",
  warn: "bg-warn-soft",
  error: "bg-fail-soft",
};

const LEVEL_RAIL: Record<Level, string> = {
  info: "",
  debug: "",
  warn: "shadow-[inset_2px_0_0_var(--warn)]",
  error: "shadow-[inset_2px_0_0_var(--fail)]",
};

/**
 * Terminal output as real text: selectable, searchable and readable by assistive technology.
 * Only the rows in view are rendered, so a build log of a hundred thousand lines scrolls like
 * one of a hundred. The output region is deliberately not a live region: announcing every
 * line would drown a screen reader. State changes are announced elsewhere.
 */
export function LogViewer({
  lines,
  follow = true,
  height = 360,
  searchable = true,
  onLoadMore,
  label = "Log output",
  filename = "wasm.log",
  emptyMessage = "No output yet.",
  className,
}: LogViewerProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const lastTop = useRef(0);
  const loadRequested = useRef(false);
  const firstId = useRef<number | undefined>(lines[0]?.id);

  const [following, setFollowing] = useState(follow);
  const [atBottom, setAtBottom] = useState(true);
  const [pausedAt, setPausedAt] = useState<number | null>(null);
  const [wrap, setWrap] = useState(false);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);

  // TanStack Virtual returns fresh functions each render; this component does not use the
  // React Compiler, so the library's memoisation caveat does not apply.
  // eslint-disable-next-line react-hooks/incompatible-library
  const virtualizer = useVirtualizer({
    count: lines.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 24,
    getItemKey: (index) => lines[index]?.id ?? index,
  });

  useEffect(() => {
    virtualizer.measure();
  }, [wrap, virtualizer]);

  const needle = query.trim().toLowerCase();
  const matches = useMemo(() => findMatches(lines, needle), [lines, needle]);

  const current = matches.length > 0 ? Math.min(active, matches.length - 1) : -1;

  const rangesByLine = useMemo(() => {
    const map = new Map<number, Range[]>();
    matches.forEach((match, index) => {
      const list = map.get(match.line) ?? [];
      list.push({ start: match.start, end: match.end, current: index === current });
      map.set(match.line, list);
    });
    return map;
  }, [matches, current]);

  // Pinned to the newest line while following.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (!el || !following) return;
    el.scrollTop = el.scrollHeight;
    lastTop.current = el.scrollTop;
  }, [lines.length, following, wrap]);

  // Older lines prepended: keep the reader's place instead of jumping.
  useLayoutEffect(() => {
    const previous = firstId.current;
    firstId.current = lines[0]?.id;
    loadRequested.current = false;
    const el = scrollRef.current;
    if (!el || previous === undefined || following) return;
    const shift = lines.findIndex((line) => line.id === previous);
    if (shift > 0) {
      el.scrollTop += shift * ROW_HEIGHT;
      lastTop.current = el.scrollTop;
    }
  }, [lines, following]);

  const pause = (): void => {
    setFollowing(false);
    setPausedAt(lines.length);
  };

  const onScroll = (): void => {
    const el = scrollRef.current;
    if (!el) return;
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK;
    setAtBottom(bottom);
    if (bottom) {
      if (!following) {
        setFollowing(true);
        setPausedAt(null);
      }
    } else if (el.scrollTop < lastTop.current && following) {
      pause();
    }
    lastTop.current = el.scrollTop;
    if (onLoadMore && el.scrollTop < ROW_HEIGHT * 2 && !loadRequested.current) {
      loadRequested.current = true;
      onLoadMore();
    }
  };

  const jumpToLatest = (): void => {
    setFollowing(true);
    setPausedAt(null);
    setAtBottom(true);
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  };

  const reveal = (match: Match): void => {
    // Reading a match is reading back through the output: following stops.
    if (following) pause();
    virtualizer.scrollToIndex(match.line, { align: "center" });
  };

  const goTo = (index: number): void => {
    if (matches.length === 0) return;
    const next = (index + matches.length) % matches.length;
    setActive(next);
    const match = matches[next];
    if (match) reveal(match);
  };

  // Like a browser's find: typing jumps to the first match straight away.
  const onQueryChange = (value: string): void => {
    setQuery(value);
    setActive(0);
    const first = findMatches(lines, value.trim().toLowerCase())[0];
    if (first) reveal(first);
  };

  const onSearchKey = (event: KeyboardEvent<HTMLInputElement>): void => {
    if (event.key === "Enter") {
      event.preventDefault();
      goTo(event.shiftKey ? current - 1 : current + 1);
    } else if (event.key === "Escape" && query !== "") {
      event.preventDefault();
      event.stopPropagation();
      setQuery("");
      setActive(0);
    }
  };

  const onRootKey = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (searchable && event.key.toLowerCase() === "f" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      searchRef.current?.focus();
      searchRef.current?.select();
    }
  };

  const plainText = (): string => lines.map((line) => parsed(line).plain).join("\n");
  const newLines = pausedAt !== null ? Math.max(0, lines.length - pausedAt) : 0;
  const showJump = !following && !atBottom && lines.length > 0;

  return (
    // The key handler only redirects Ctrl/Cmd+F to the search box inside this viewer.
    // eslint-disable-next-line jsx-a11y/no-static-element-interactions
    <div
      onKeyDown={onRootKey}
      className={cx(
        "flex min-h-0 min-w-0 flex-col overflow-hidden rounded-card border border-border bg-bg-sunken",
        height === "fill" && "h-full",
        className,
      )}
      style={height === "fill" ? undefined : { height }}
    >
      <div className="flex shrink-0 items-center gap-2 border-b border-border bg-surface px-2 py-1.5">
        {searchable ? (
          <div className="flex min-w-0 flex-1 items-center gap-2">
            <Input
              ref={searchRef}
              size="sm"
              type="search"
              aria-label="Search output"
              placeholder="Search output"
              icon={<Search />}
              value={query}
              onValueChange={onQueryChange}
              onKeyDown={onSearchKey}
              className="w-full max-w-64 min-w-28"
            />
            <span role="status" className="mono shrink-0 text-12 whitespace-nowrap text-fg-muted">
              {needle === "" ? "" : matches.length === 0 ? "No matches" : `${String(current + 1)} of ${String(matches.length)}`}
            </span>
            {needle !== "" ? (
              <span className="flex shrink-0">
                <IconButton
                  size="sm"
                  label="Previous match"
                  icon={<ChevronUp />}
                  shortcut={["Shift", "Enter"]}
                  disabled={matches.length === 0}
                  onClick={() => goTo(current - 1)}
                />
                <IconButton
                  size="sm"
                  label="Next match"
                  icon={<ChevronDown />}
                  shortcut={["Enter"]}
                  disabled={matches.length === 0}
                  onClick={() => goTo(current + 1)}
                />
              </span>
            ) : null}
          </div>
        ) : (
          <div className="flex-1" />
        )}
        <div className="flex shrink-0 items-center gap-0.5">
          <Button
            size="sm"
            variant="ghost"
            icon={<ArrowDownToLine />}
            aria-pressed={following}
            onClick={() => (following ? pause() : jumpToLatest())}
            className="mr-1 aria-pressed:bg-surface-active aria-pressed:text-fg"
          >
            <span className="max-sm:sr-only">Follow</span>
          </Button>
          <IconButton size="sm" label="Wrap lines" icon={<WrapText />} pressed={wrap} onClick={() => setWrap((v) => !v)} />
          <CopyButton value={plainText} label="Copy output" />
          <IconButton
            size="sm"
            label="Download output"
            icon={<Download />}
            disabled={lines.length === 0}
            onClick={() => downloadText(filename, `${plainText()}\n`)}
          />
        </div>
      </div>

      <div className="relative min-h-0 flex-1">
        <div
          ref={scrollRef}
          role="region"
          aria-label={label}
          tabIndex={0}
          onScroll={onScroll}
          className="mono h-full overflow-auto py-1.5 text-12 leading-5 text-fg scroll-thin focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus"
        >
          {lines.length === 0 ? (
            <p className="px-4 py-3 font-sans text-13 text-fg-faint">{emptyMessage}</p>
          ) : (
            <div className="relative w-full" style={{ height: virtualizer.getTotalSize() }}>
              {virtualizer.getVirtualItems().map((item) => {
                const line = lines[item.index];
                if (!line) return null;
                const { segments } = parsed(line);
                return (
                  <div
                    key={item.key}
                    data-index={item.index}
                    ref={wrap ? virtualizer.measureElement : undefined}
                    className={cx(
                      "absolute top-0 left-0 flex min-h-5 min-w-full pr-4",
                      wrap ? "w-full" : "w-max",
                      LEVEL_ROW[line.level ?? "info"],
                    )}
                    style={{ transform: `translateY(${String(item.start)}px)` }}
                  >
                    <span
                      aria-hidden="true"
                      className={cx(
                        "sticky left-0 w-12 shrink-0 bg-inherit pr-3 text-right text-fg-faint select-none",
                        LEVEL_RAIL[line.level ?? "info"],
                      )}
                    >
                      {item.index + 1}
                    </span>
                    {line.ts !== undefined ? (
                      <span className="shrink-0 pr-3 text-fg-faint">{line.ts}</span>
                    ) : null}
                    <span className={wrap ? "min-w-0 flex-1 break-all whitespace-pre-wrap" : "whitespace-pre"}>
                      {renderSegments(segments, rangesByLine.get(item.index) ?? [])}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {showJump ? (
          <button
            type="button"
            onClick={jumpToLatest}
            className={cx(
              "absolute bottom-3 left-1/2 flex h-8 -translate-x-1/2 cursor-pointer items-center gap-1.5 rounded-pill border border-border bg-surface-raised pr-3.5 pl-3 text-13 font-medium text-fg shadow-overlay",
              "hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
            )}
          >
            <ArrowDown aria-hidden="true" className="size-3.5" />
            Jump to latest
            {newLines > 0 ? (
              <span className="mono text-12 text-accent-fg">
                +{newLines}
                <span className="sr-only"> new lines</span>
              </span>
            ) : null}
          </button>
        ) : null}
      </div>
    </div>
  );
}
