import { useImperativeHandle, useRef } from "react";
import type { Ref } from "react";

import { CONTROL_FRAME } from "../../components/ui/Input";
import { cx } from "../../lib/cx";
import { lineOffset } from "./configErrors";

/** Line height of the editor, in px: the gutter and the text share it (leading-5). */
const LINE_PX = 20;

export interface ConfigEditorHandle {
  /** Puts the caret at the start of a line and scrolls it into view. */
  goToLine: (line: number) => void;
}

export interface ConfigEditorProps {
  value: string;
  onChange: (value: string) => void;
  /** Names the editor for assistive technology. */
  label: string;
  /** Ids of the text that describes it: the file's path, the last test's outcome. */
  describedBy?: string;
  /** A line the web server objected to, marked in the gutter. */
  errorLine?: number | null;
  disabled?: boolean;
  ref?: Ref<ConfigEditorHandle>;
}

/**
 * A configuration file, edited as text: mono, no wrapping, no spellcheck, with line numbers
 * so a web server's "in site.conf:57" can be found. Tab keeps its usual meaning (the next
 * control); nothing here traps the keyboard.
 */
export function ConfigEditor({ value, onChange, label, describedBy, errorLine = null, disabled = false, ref }: ConfigEditorProps) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  const gutter = useRef<HTMLDivElement>(null);
  const lines = value.split("\n").length;

  useImperativeHandle(ref, () => ({
    goToLine: (line: number) => {
      const element = textarea.current;
      if (!element) return;
      const offset = lineOffset(element.value, line);
      element.focus();
      element.setSelectionRange(offset, offset);
      element.scrollTop = Math.max(0, (line - 4) * LINE_PX);
    },
  }));

  return (
    // The frame has the height and is what resizes; the gutter and the text fill it, and the
    // gutter follows the text's scroll, so a number always sits beside its line.
    <div className={cx("flex h-[26rem] min-h-40 min-w-0 resize-y overflow-hidden sm:h-[34rem]", CONTROL_FRAME)}>
      <div
        ref={gutter}
        aria-hidden="true"
        className="mono shrink-0 overflow-hidden border-r border-border bg-bg-sunken py-2 text-right text-12 leading-5 text-fg-faint select-none"
      >
        {Array.from({ length: lines }, (_, index) => (
          <div
            key={index}
            className={cx("px-2.5", index + 1 === errorLine && "bg-fail-soft font-medium text-fail")}
          >
            {index + 1}
          </div>
        ))}
      </div>
      <textarea
        ref={textarea}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        onScroll={(event) => {
          if (gutter.current) gutter.current.scrollTop = event.currentTarget.scrollTop;
        }}
        aria-label={label}
        {...(describedBy !== undefined ? { "aria-describedby": describedBy } : {})}
        {...(errorLine !== null ? { "aria-invalid": true } : {})}
        disabled={disabled}
        wrap="off"
        spellCheck={false}
        autoCapitalize="off"
        autoComplete="off"
        autoCorrect="off"
        translate="no"
        className="mono block h-full w-full min-w-0 resize-none overflow-auto bg-transparent px-3 py-2 text-12 leading-5 whitespace-pre text-fg outline-none scroll-thin disabled:cursor-not-allowed"
      />
    </div>
  );
}
