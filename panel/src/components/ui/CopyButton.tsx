import { Check, Copy } from "lucide-react";

import { IconButton } from "./IconButton";
import { useCopyState } from "./useCopyState";

export interface CopyButtonProps {
  /** The exact text placed on the clipboard, or a function that produces it on press. */
  value: string | (() => string);
  /** What is being copied, completing "Copy ..." for the accessible name. */
  label?: string;
  size?: "sm" | "md";
  className?: string;
  /** An element (usually visually hidden) describing what pressing it copies. */
  "aria-describedby"?: string;
}

/** Copies a value and confirms it in place: the icon becomes a check and the change is announced. */
export function CopyButton({ value, label = "Copy", size = "sm", className, ["aria-describedby"]: describedBy }: CopyButtonProps) {
  const { state, copy } = useCopyState();

  return (
    <>
      <IconButton
        label={label}
        size={size}
        icon={state === "copied" ? <Check className="text-ok" /> : <Copy />}
        onClick={() => void copy(typeof value === "function" ? value() : value)}
        {...(className ? { className } : {})}
        {...(describedBy !== undefined ? { "aria-describedby": describedBy } : {})}
      />
      <span role="status" className="sr-only">
        {state === "copied" ? "Copied to clipboard" : state === "failed" ? "Copy failed" : ""}
      </span>
    </>
  );
}
