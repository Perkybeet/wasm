import { useId } from "react";
import type { ReactNode, SyntheticEvent } from "react";

import { CommandHint } from "../../components/page/CommandHint";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
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
