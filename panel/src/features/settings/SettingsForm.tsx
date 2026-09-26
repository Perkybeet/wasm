import { useId } from "react";
import type { ReactNode, SyntheticEvent } from "react";

import { CommandHint } from "../../components/page/CommandHint";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { Skeleton } from "../../components/ui/Skeleton";
import { cx } from "../../lib/cx";

export interface SettingsSectionProps {
  title: string;
  description: ReactNode;
  /**
   * The same thing from a terminal: the `wasm config` commands for the change being made, or
   * for reading the setting when nothing has changed.
   */
  commands?: readonly string[];
  children: ReactNode;
  className?: string;
}

/**
 * One subject of the settings: on a wide screen, what it is and its terminal form on the left
 * and the controls on the right; stacked on a phone. A landmark named by its heading.
 */
export function SettingsSection({ title, description, commands = [], children, className }: SettingsSectionProps) {
  const headingId = useId();
  return (
    <section
      aria-labelledby={headingId}
      className={cx("grid min-w-0 gap-x-10 gap-y-4 lg:grid-cols-[minmax(0,5fr)_minmax(0,9fr)]", className)}
    >
      <header className="flex min-w-0 flex-col gap-1 lg:pt-1">
        <h2 id={headingId} className="title text-16 text-fg">
          {title}
        </h2>
        <p className="max-w-[52ch] text-13 text-pretty text-fg-muted">{description}</p>
        {commands.length > 0 ? (
          <div className="mt-2 flex min-w-0 flex-col gap-1.5">
            <span className="text-12 text-fg-faint">From a terminal</span>
            {commands.map((command) => (
              <CommandHint key={command} command={command} />
            ))}
          </div>
        ) : null}
      </header>
      <div className="min-w-0">{children}</div>
    </section>
  );
}

export interface SettingsFormCardProps {
  children: ReactNode;
  dirty: boolean;
  pending: boolean;
  /** A failure that is not about one field: shown above the fields, verbatim, with its fix. */
  formError?: unknown;
  /** What failed, for that block: "Could not save the backup settings". */
  errorTitle: string;
  onSubmit: (event: SyntheticEvent<HTMLFormElement>) => void;
  onDiscard: () => void;
  /** The verb on the button. */
  saveLabel?: string;
}

/**
 * The controls of one section as a form on a surface, with its footer: whether anything is
 * unsaved, Discard, and Save - enabled only when something changed. The browser's own
 * validation is off on purpose: the server rules on every value and its words are shown.
 */
export function SettingsFormCard({
  children,
  dirty,
  pending,
  formError,
  errorTitle,
  onSubmit,
  onDiscard,
  saveLabel = "Save changes",
}: SettingsFormCardProps) {
  return (
    <form
      noValidate
      onSubmit={onSubmit}
      className="flex min-w-0 flex-col rounded-card border border-border bg-surface shadow-raised"
    >
      <div className="flex min-w-0 flex-col gap-5 p-5">
        {formError !== null && formError !== undefined ? <ErrorBlock live compact error={formError} title={errorTitle} /> : null}
        {children}
      </div>
      <footer className="flex flex-wrap items-center justify-end gap-2 rounded-b-card border-t border-border bg-bg-sunken px-5 py-3">
        <p role="status" className="mr-auto flex items-center gap-2 text-13 text-fg-muted">
          {dirty ? (
            <>
              <span aria-hidden="true" className="size-1.5 rounded-pill bg-warn" />
              Unsaved changes
            </>
          ) : null}
        </p>
        {dirty && !pending ? (
          <Button variant="ghost" onClick={onDiscard}>
            Discard
          </Button>
        ) : null}
        {/* Quiet until there is something to save: a disabled accent button on every section
            would put colour on screen that means nothing. */}
        <Button type="submit" variant={dirty ? "primary" : "secondary"} disabled={!dirty} loading={pending}>
          {saveLabel}
        </Button>
      </footer>
    </form>
  );
}

/** One field of a form still loading: its label, its control, and its description's lines. */
export interface SkeletonField {
  /** Lines of description under the control, as the loaded field wraps them on a desktop. */
  description?: number;
  /** Rows of a textarea; an input or a select when absent. */
  rows?: number;
  /** Fields sharing one row, side by side (a host and its port). */
  inline?: number;
}

/**
 * A SettingsFormCard still loading, line for line: each field's label, control and description
 * at the heights Field, Input and Textarea give them, and the footer with its button, so the
 * sections below do not move when the settings arrive.
 */
export function SettingsFormSkeleton({ fields }: { fields: readonly SkeletonField[] }) {
  return (
    <div aria-hidden="true" className="flex min-w-0 flex-col rounded-card border border-border bg-surface shadow-raised">
      <div className="flex min-w-0 flex-col gap-5 p-5">
        {fields.map((field, index) => (
          <div key={index} className="flex min-w-0 gap-4">
            {Array.from({ length: field.inline ?? 1 }, (_, column) => (
              <div key={column} className="flex min-w-0 flex-1 flex-col gap-1.5">
                <div className="flex h-5 items-center">
                  <Skeleton className="h-3 w-32" />
                </div>
                {field.rows !== undefined ? (
                  // Textarea: 20px a row, its vertical padding and its border.
                  <div className="w-full max-w-md" style={{ height: 14 + 20 * field.rows }}>
                    <Skeleton className="h-full" />
                  </div>
                ) : (
                  <Skeleton className="h-8 w-full max-w-md" />
                )}
                {Array.from({ length: field.description ?? 0 }, (_, line) => (
                  <div key={line} className="flex h-4 items-center">
                    <Skeleton className="h-2.5 w-64 max-w-full" />
                  </div>
                ))}
              </div>
            ))}
          </div>
        ))}
      </div>
      <div className="flex justify-end rounded-b-card border-t border-border bg-bg-sunken px-5 py-3">
        <Skeleton className="h-8 w-28" />
      </div>
    </div>
  );
}
