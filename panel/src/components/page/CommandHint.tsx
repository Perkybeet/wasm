import { cx } from "../../lib/cx";
import { CopyButton } from "../ui/CopyButton";

export interface CommandHintProps {
  /** The exact command, without the prompt: `wasm status example.com`. */
  command: string;
  /** Leads into the command for context: "From a terminal". Omit where the page already says it. */
  label?: string;
  className?: string;
}

/**
 * The CLI command that does what this part of the console does, for operators who live in a
 * terminal: set in mono behind a prompt the copy button leaves out.
 */
export function CommandHint({ command, label, className }: CommandHintProps) {
  return (
    <div className={cx("flex max-w-full min-w-0 flex-wrap items-center gap-x-2 gap-y-1", className)}>
      {label !== undefined ? <span className="text-12 text-fg-faint">{label}</span> : null}
      <div className="flex max-w-full min-w-0 items-center gap-1 rounded-control border border-border bg-bg-sunken py-0.5 pr-0.5 pl-2.5">
        <code translate="no" className="truncate text-12 text-fg-muted" title={command}>
          <span aria-hidden="true" className="text-fg-faint select-none">
            ${" "}
          </span>
          {command}
        </code>
        <CopyButton value={command} label="Copy command" />
      </div>
    </div>
  );
}
