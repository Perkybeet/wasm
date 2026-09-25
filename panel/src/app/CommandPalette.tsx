import { Dialog as BaseDialog } from "@base-ui/react/dialog";
import { Search } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode, RefObject } from "react";

import { BACKDROP, MODAL_POPUP } from "../components/ui/Dialog";
import { Kbd } from "../components/ui/Kbd";
import { StatusPill } from "../components/ui/StatusPill";
import type { Status } from "../components/ui/StatusPill";
import { cx } from "../lib/cx";

export type CommandGroup = "Pages" | "Applications" | "Actions";

export interface Command {
  /** Unique across the palette. */
  id: string;
  group: CommandGroup;
  label: string;
  icon?: ReactNode;
  /** Other words it should be found by. */
  keywords?: string;
  /** Keys of the shortcut that does the same, shown at the end of the row. */
  shortcut?: readonly string[];
  /** For applications: their state, shown as a pill. */
  status?: Status;
  /**
   * `navigate` commands open a page, which then takes focus; `action` commands leave the
   * operator where they were, so focus returns to what opened the palette.
   */
  kind: "navigate" | "action";
  run: () => void;
}

export interface CommandResults {
  group: CommandGroup;
  items: Command[];
}

const GROUP_ORDER: readonly CommandGroup[] = ["Pages", "Applications", "Actions"];

/** With nothing typed, a long list of applications would push actions off screen. */
const BROWSE_LIMIT: Partial<Record<CommandGroup, number>> = { Applications: 6 };

function isSubsequence(needle: string, haystack: string): boolean {
  let at = 0;
  for (const char of haystack) {
    if (char === needle[at]) at += 1;
    if (at === needle.length) return true;
  }
  return needle.length === 0;
}

/** How well a command matches what was typed; 0 is no match. */
export function scoreCommand(command: Command, query: string): number {
  const q = query.trim().toLowerCase();
  if (q === "") return 1;
  const label = command.label.toLowerCase();
  if (label === q) return 100;
  if (label.startsWith(q)) return 80;
  if (label.split(/[\s./-]+/).some((word) => word.startsWith(q))) return 60;
  if (label.includes(q)) return 40;
  const haystack = `${label} ${command.keywords ?? ""} ${command.group}`.toLowerCase();
  if (q.split(/\s+/).every((token) => haystack.includes(token))) return 20;
  // Scattered letters only mean something once there are a few of them.
  if (q.length >= 3 && isSubsequence(q, label)) return 10;
  return 0;
}

/** Matching commands grouped, best group first once something is typed. */
export function filterCommands(commands: readonly Command[], query: string): CommandResults[] {
  const browsing = query.trim() === "";
  const groups = GROUP_ORDER.map((group) => {
    const scored = commands
      .filter((command) => command.group === group)
      .map((command, order) => ({ command, order, score: scoreCommand(command, query) }))
      .filter((entry) => entry.score > 0)
      .sort((a, b) => b.score - a.score || a.order - b.order);
    const limit = browsing ? BROWSE_LIMIT[group] : undefined;
    const kept = limit === undefined ? scored : scored.slice(0, limit);
    return { group, best: scored[0]?.score ?? 0, items: kept.map((entry) => entry.command) };
  }).filter((group) => group.items.length > 0);
  if (!browsing) groups.sort((a, b) => b.best - a.best);
  return groups.map(({ group, items }) => ({ group, items }));
}

function domId(base: string, id: string): string {
  return `${base}-${id.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
}

function PaletteBody({
  commands,
  onRun,
  inputRef,
}: {
  commands: readonly Command[];
  onRun: (command: Command) => void;
  inputRef: RefObject<HTMLInputElement | null>;
}) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const base = useId().replace(/[^a-zA-Z0-9_-]/g, "");
  const listId = `${base}-results`;

  const results = useMemo(() => filterCommands(commands, query), [commands, query]);
  const flat = useMemo(() => results.flatMap((group) => group.items), [results]);
  const positions = useMemo(() => new Map(flat.map((command, position) => [command, position])), [flat]);
  const activeIndex = flat.length === 0 ? -1 : Math.min(active, flat.length - 1);
  const activeCommand = activeIndex >= 0 ? flat[activeIndex] : undefined;
  const activeId = activeCommand ? domId(base, activeCommand.id) : undefined;

  useEffect(() => {
    if (activeId === undefined) return;
    document.getElementById(activeId)?.scrollIntoView({ block: "nearest" });
  }, [activeId]);

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>): void => {
    if (flat.length === 0) return;
    if (event.key === "ArrowDown" || (event.key === "n" && event.ctrlKey)) {
      event.preventDefault();
      setActive((activeIndex + 1) % flat.length);
    } else if (event.key === "ArrowUp" || (event.key === "p" && event.ctrlKey)) {
      event.preventDefault();
      setActive((activeIndex - 1 + flat.length) % flat.length);
    } else if (event.key === "Enter" && activeCommand) {
      event.preventDefault();
      onRun(activeCommand);
    }
  };

  return (
    <>
      <div className="flex h-12 shrink-0 items-center gap-2.5 border-b border-border px-4">
        <Search aria-hidden="true" className="size-4 shrink-0 text-fg-faint" />
        <input
          ref={inputRef}
          type="text"
          role="combobox"
          aria-label="Search pages, applications and actions"
          aria-expanded={flat.length > 0}
          aria-controls={flat.length > 0 ? listId : undefined}
          aria-activedescendant={activeId}
          aria-autocomplete="list"
          autoComplete="off"
          autoCapitalize="off"
          spellCheck={false}
          placeholder="Search pages, applications and actions"
          value={query}
          onChange={(event) => {
            setQuery(event.target.value);
            setActive(0);
          }}
          onKeyDown={onKeyDown}
          className="h-full min-w-0 flex-1 bg-transparent text-14 text-fg outline-none placeholder:text-fg-faint"
        />
        <Kbd className="max-sm:hidden">Esc</Kbd>
      </div>

      {flat.length > 0 ? (
        <div
          id={listId}
          role="listbox"
          aria-label="Results"
          className="max-h-[min(60vh,26rem)] min-h-0 overflow-y-auto p-1.5 scroll-thin"
        >
          {results.map((result) => (
            <div key={result.group} role="group" aria-labelledby={`${base}-${result.group}`} className="pb-1">
              <div id={`${base}-${result.group}`} aria-hidden="true" className="px-2 pt-2 pb-1 text-12 font-medium text-fg-faint">
                {result.group}
              </div>
              {result.items.map((command) => {
                const position = positions.get(command) ?? 0;
                const selected = command === activeCommand;
                return (
                  // Keyboard selection lives on the input (aria-activedescendant); the row only
                  // answers the pointer.
                  // eslint-disable-next-line jsx-a11y/click-events-have-key-events
                  <div
                    key={command.id}
                    id={domId(base, command.id)}
                    role="option"
                    aria-selected={selected}
                    tabIndex={-1}
                    onMouseMove={() => {
                      if (!selected) setActive(position);
                    }}
                    onMouseDown={(event) => {
                      event.preventDefault();
                    }}
                    onClick={() => {
                      onRun(command);
                    }}
                    className={cx(
                      "flex h-9 cursor-pointer items-center gap-2.5 rounded-control px-2 text-13 text-fg select-none",
                      selected && "bg-surface-active",
                    )}
                  >
                    <span aria-hidden="true" className="flex size-4 shrink-0 text-fg-muted [&_svg]:size-4">
                      {command.icon}
                    </span>
                    <span translate={command.group === "Applications" ? "no" : undefined} className="min-w-0 flex-1 truncate">
                      {command.label}
                    </span>
                    {command.status !== undefined ? (
                      <StatusPill state={command.status} appearance="inline" size="sm" />
                    ) : null}
                    {command.shortcut ? (
                      <span aria-hidden="true" className="flex shrink-0 items-center gap-0.5">
                        {command.shortcut.map((key) => (
                          <Kbd key={key}>{key}</Kbd>
                        ))}
                      </span>
                    ) : null}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      ) : (
        <p role="status" className="px-4 py-10 text-center text-13 text-fg-muted">
          {`Nothing matches "${query.trim()}".`}
        </p>
      )}

      <div aria-hidden="true" className="flex shrink-0 items-center gap-4 border-t border-border bg-bg-sunken px-4 py-2 text-12 text-fg-faint max-sm:hidden">
        <span className="flex items-center gap-1.5">
          <Kbd>↑</Kbd>
          <Kbd>↓</Kbd>
          to move
        </span>
        <span className="flex items-center gap-1.5">
          <Kbd>↵</Kbd>
          to open
        </span>
        <span className="flex items-center gap-1.5">
          <Kbd>Esc</Kbd>
          to close
        </span>
      </div>
    </>
  );
}

export interface CommandPaletteProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  commands: readonly Command[];
  /** Where focus returns when the palette closes without opening a page. */
  returnFocus?: () => HTMLElement | null;
}

/**
 * Jump anywhere, by name: pages, applications and actions (`Ctrl+K` / `⌘K`). An ARIA 1.2
 * combobox: focus stays in the input while the arrow keys move a highlighted option, which
 * is announced through aria-activedescendant.
 */
export function CommandPalette({ open, onOpenChange, commands, returnFocus }: CommandPaletteProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const lastRun = useRef<Command["kind"] | null>(null);

  const run = (command: Command): void => {
    lastRun.current = command.kind;
    onOpenChange(false);
    command.run();
  };

  return (
    <BaseDialog.Root
      open={open}
      onOpenChange={(next: boolean) => {
        if (next) lastRun.current = null;
        onOpenChange(next);
      }}
    >
      <BaseDialog.Portal>
        <BaseDialog.Backdrop className={BACKDROP} />
        <BaseDialog.Viewport className="fixed inset-0 z-50 flex items-start justify-center p-4 pt-[10vh] sm:pt-[14vh]">
          <BaseDialog.Popup
            initialFocus={inputRef}
            finalFocus={() => {
              // A page was opened: it takes focus itself (its h1), nothing to hand back.
              if (lastRun.current === "navigate") return false;
              return returnFocus?.() ?? true;
            }}
            className={cx(MODAL_POPUP, "sm:max-w-[640px]")}
          >
            <BaseDialog.Title className="sr-only">Search the console</BaseDialog.Title>
            <PaletteBody commands={commands} onRun={run} inputRef={inputRef} />
          </BaseDialog.Popup>
        </BaseDialog.Viewport>
      </BaseDialog.Portal>
    </BaseDialog.Root>
  );
}
