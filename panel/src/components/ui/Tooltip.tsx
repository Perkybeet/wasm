import { Tooltip as BaseTooltip } from "@base-ui/react/tooltip";
import type { ReactElement, ReactNode } from "react";

import { Kbd } from "./Kbd";

export interface TooltipProps {
  /** Supplementary text. Never the only place important information lives. */
  content: ReactNode;
  /** Keys of the shortcut that performs the same action, shown after the text. */
  shortcut?: readonly string[];
  side?: "top" | "bottom" | "left" | "right";
  /** The trigger: a single focusable element. */
  children: ReactElement<Record<string, unknown>>;
  disabled?: boolean;
}

export const POPUP_MOTION =
  "origin-(--transform-origin) transition-[opacity,scale] duration-(--duration-fast) ease-out " +
  "data-starting-style:scale-[0.97] data-starting-style:opacity-0 data-ending-style:opacity-0";

/** Groups tooltips so moving between triggers opens the next one without a delay. */
export function TooltipProvider({ children }: { children: ReactNode }) {
  return (
    <BaseTooltip.Provider delay={500} closeDelay={0}>
      {children}
    </BaseTooltip.Provider>
  );
}

/** A small label for a control, on hover and keyboard focus. */
export function Tooltip({ content, shortcut, side = "top", children, disabled = false }: TooltipProps) {
  return (
    <BaseTooltip.Root disabled={disabled}>
      <BaseTooltip.Trigger render={children} />
      <BaseTooltip.Portal>
        <BaseTooltip.Positioner side={side} sideOffset={6} className="z-50">
          <BaseTooltip.Popup
            className={`flex max-w-72 items-center gap-2 rounded-control bg-fg px-2 py-1 text-12 font-medium text-bg shadow-overlay ${POPUP_MOTION}`}
          >
            {content}
            {shortcut && shortcut.length > 0 ? (
              <span className="flex gap-0.5">
                {shortcut.map((key) => (
                  <Kbd key={key} tone="inverse">
                    {key}
                  </Kbd>
                ))}
              </span>
            ) : null}
          </BaseTooltip.Popup>
        </BaseTooltip.Positioner>
      </BaseTooltip.Portal>
    </BaseTooltip.Root>
  );
}
