import { Check, Copy } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "../../components/ui/Button";
import type { ButtonSize, ButtonVariant } from "../../components/ui/Button";
import { copyText } from "../../lib/clipboard";

export interface CopyTextButtonProps {
  /** The exact text placed on the clipboard. */
  value: string;
  /** The button's words: "Copy token". */
  children: string;
  variant?: ButtonVariant;
  size?: ButtonSize;
  className?: string;
}

/**
 * A labelled copy button, for the one value on a screen that matters (a new token, backup
 * codes): the words stay, the icon confirms, and the outcome is announced.
 */
export function CopyTextButton({ value, children, variant = "secondary", size = "md", className }: CopyTextButtonProps) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const timer = setTimeout(() => {
      setState("idle");
    }, 2000);
    return () => {
      clearTimeout(timer);
    };
  }, [state]);
  return (
    <>
      <Button
        variant={variant}
        size={size}
        icon={state === "copied" ? <Check aria-hidden="true" className="text-ok" /> : <Copy aria-hidden="true" />}
        onClick={() => {
          copyText(value).then(
            () => {
              setState("copied");
            },
            () => {
              setState("failed");
            },
          );
        }}
        {...(className !== undefined ? { className } : {})}
      >
        {children}
      </Button>
      <span role="status" className="sr-only">
        {state === "copied" ? "Copied to clipboard" : state === "failed" ? "Copy failed. Select the text and copy it by hand." : ""}
      </span>
    </>
  );
}
