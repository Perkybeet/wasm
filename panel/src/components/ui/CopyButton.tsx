import { Check, Copy } from "lucide-react";
import { useEffect, useState } from "react";

import { copyText } from "../../lib/clipboard";
import { IconButton } from "./IconButton";

export interface CopyButtonProps {
  /** The exact text placed on the clipboard, or a function that produces it on press. */
  value: string | (() => string);
  /** What is being copied, completing "Copy ..." for the accessible name. */
  label?: string;
  size?: "sm" | "md";
  className?: string;
}

type CopyState = "idle" | "copied" | "failed";

/** Copies a value and confirms it in place: the icon becomes a check and the change is announced. */
export function CopyButton({ value, label = "Copy", size = "sm", className }: CopyButtonProps) {
  const [state, setState] = useState<CopyState>("idle");

  useEffect(() => {
    if (state === "idle") return;
    const timer = setTimeout(() => {
      setState("idle");
    }, 1600);
    return () => {
      clearTimeout(timer);
    };
  }, [state]);

  const onCopy = async (): Promise<void> => {
    try {
      await copyText(typeof value === "function" ? value() : value);
      setState("copied");
    } catch {
      // The failure is shown and announced below; there is nothing else to recover.
      setState("failed");
    }
  };

  return (
    <>
      <IconButton
        label={label}
        size={size}
        icon={state === "copied" ? <Check className="text-ok" /> : <Copy />}
        onClick={() => void onCopy()}
        {...(className ? { className } : {})}
      />
      <span role="status" className="sr-only">
        {state === "copied" ? "Copied to clipboard" : state === "failed" ? "Copy failed" : ""}
      </span>
    </>
  );
}
