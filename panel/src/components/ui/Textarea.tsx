import { Field as BaseField } from "@base-ui/react/field";
import type { TextareaHTMLAttributes } from "react";

import { cx } from "../../lib/cx";
import { CONTROL_FRAME } from "./Input";

export interface TextareaProps extends Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "className"> {
  /** Environment files, unit files and config are edited in mono. */
  mono?: boolean;
  className?: string;
}

/** A multi-line text control. Wrap it in Field for a label, help and error. */
export function Textarea({ mono = false, rows = 4, className, ...rest }: TextareaProps) {
  return (
    <div className={cx("flex min-w-0", CONTROL_FRAME, className)}>
      <BaseField.Control
        render={
          <textarea
            {...rest}
            rows={rows}
            spellCheck={mono ? false : rest.spellCheck}
            className={cx(
              "block min-h-16 w-full resize-y bg-transparent px-2.5 py-1.5 text-13 outline-none placeholder:text-fg-faint disabled:cursor-not-allowed",
              mono && "mono leading-5",
            )}
          />
        }
      />
    </div>
  );
}
