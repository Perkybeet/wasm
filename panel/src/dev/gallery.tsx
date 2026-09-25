import type { ReactNode } from "react";

import { cx } from "../lib/cx";

/** One subject of the gallery: a heading, a sentence on intent, then specimens. */
export function Section({
  id,
  title,
  description,
  children,
}: {
  id: string;
  title: string;
  description: ReactNode;
  children: ReactNode;
}) {
  return (
    <section
      id={id}
      data-gallery-section={id}
      aria-labelledby={`${id}-title`}
      className="scroll-mt-20 border-t border-border py-12 first:border-t-0 first:pt-2"
    >
      <header className="mb-6 max-w-[62ch]">
        <h2 id={`${id}-title`} className="title text-18 text-fg">
          {title}
        </h2>
        <p className="mt-1.5 text-14 text-pretty text-fg-muted">{description}</p>
      </header>
      <div className="flex flex-col gap-6">{children}</div>
    </section>
  );
}

/** A framed stage for components, on the page ground with a faint measuring grid. */
export function Stage({
  children,
  className,
  plain = false,
  flush = false,
}: {
  children: ReactNode;
  className?: string;
  plain?: boolean;
  /** No inner padding, for content that draws its own rows. */
  flush?: boolean;
}) {
  return (
    <div
      className={cx(
        "min-w-0 rounded-card border border-border",
        flush ? "overflow-hidden" : "p-6 max-sm:p-4",
        plain
          ? "bg-bg"
          : "bg-bg bg-[radial-gradient(var(--border)_1px,transparent_1px)] bg-size-[16px_16px] bg-position-[-8px_-8px]",
        className,
      )}
    >
      {children}
    </div>
  );
}

/** A specimen with the name of the state it shows. */
export function Item({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className={cx("flex min-w-0 flex-col items-start gap-2.5", className)}>
      {children}
      <span className="text-12 text-fg-faint">{label}</span>
    </div>
  );
}

export function Row({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cx("flex flex-wrap items-end gap-x-8 gap-y-6", className)}>{children}</div>;
}
