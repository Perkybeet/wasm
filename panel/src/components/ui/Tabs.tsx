import { Tabs as BaseTabs } from "@base-ui/react/tabs";
import type { ReactNode } from "react";

import { cx } from "../../lib/cx";

export interface TabsProps<V extends string = string> {
  value?: V;
  defaultValue?: V;
  onValueChange?: (value: V) => void;
  children: ReactNode;
  className?: string;
}

/** Sibling views of one subject: an app's overview, deployments, logs. */
export function Tabs<V extends string = string>({ value, defaultValue, onValueChange, children, className }: TabsProps<V>) {
  return (
    <BaseTabs.Root
      {...(value !== undefined ? { value } : {})}
      {...(defaultValue !== undefined ? { defaultValue } : {})}
      {...(onValueChange ? { onValueChange: (next: unknown) => onValueChange(next as V) } : {})}
      className={cx("flex min-w-0 flex-col", className)}
    >
      {children}
    </BaseTabs.Root>
  );
}

export interface TabListProps {
  /** Names the set of tabs for assistive technology: "Application sections". */
  "aria-label": string;
  children: ReactNode;
  className?: string;
}

/** The row of tabs, with the selection marked by an accent rule under the active tab. */
export function TabList({ "aria-label": ariaLabel, children, className }: TabListProps) {
  return (
    <BaseTabs.List
      aria-label={ariaLabel}
      className={cx(
        "relative z-0 flex items-stretch gap-1 overflow-x-auto border-b border-border scroll-thin [scrollbar-width:none]",
        className,
      )}
    >
      {children}
      <BaseTabs.Indicator
        className={cx(
          "absolute bottom-0 left-0 z-10 h-0.5 w-(--active-tab-width) translate-x-(--active-tab-left) rounded-pill bg-accent-fg",
          "transition-[translate,width] duration-(--duration-base) ease-out",
        )}
      />
    </BaseTabs.List>
  );
}

export interface TabProps {
  value: string;
  children: ReactNode;
  /** A count shown after the label, such as the number of deployments. */
  count?: number;
  disabled?: boolean;
}

export function Tab({ value, children, count, disabled = false }: TabProps) {
  return (
    <BaseTabs.Tab
      value={value}
      disabled={disabled}
      className={cx(
        "group relative flex h-10 shrink-0 cursor-pointer items-center gap-1.5 rounded-control px-2.5 text-13 font-medium whitespace-nowrap text-fg-muted outline-none select-none",
        "hover:not-data-disabled:text-fg data-active:text-fg",
        "focus-visible:after:absolute focus-visible:after:inset-x-0 focus-visible:after:inset-y-1.5 focus-visible:after:rounded-control focus-visible:after:outline-2 focus-visible:after:outline-focus",
        "data-disabled:cursor-not-allowed data-disabled:opacity-50",
      )}
    >
      {children}
      {/* A space keeps the accessible name "Deployments 12", not "Deployments12". */}
      {count !== undefined ? " " : null}
      {count !== undefined ? (
        <span className="mono rounded-[4px] bg-bg-sunken px-1 text-12 text-fg-muted group-data-active:text-fg">{count}</span>
      ) : null}
    </BaseTabs.Tab>
  );
}

export interface TabPanelProps {
  value: string;
  children: ReactNode;
  className?: string;
}

export function TabPanel({ value, children, className }: TabPanelProps) {
  return (
    <BaseTabs.Panel
      value={value}
      className={cx("min-w-0 pt-5 outline-none focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-focus", className)}
    >
      {children}
    </BaseTabs.Panel>
  );
}
