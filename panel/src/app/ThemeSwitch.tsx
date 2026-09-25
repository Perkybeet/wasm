import { Monitor, Moon, Sun } from "lucide-react";
import type { LucideIcon } from "lucide-react";

import { cx } from "../lib/cx";
import { THEME_CHOICES } from "./theme";
import type { ThemeChoice } from "./theme";

const ICONS: Record<ThemeChoice, LucideIcon> = { system: Monitor, light: Sun, dark: Moon };

export interface ThemeSwitchProps {
  value: ThemeChoice;
  onChange: (theme: ThemeChoice) => void;
  /** Hide the words on narrow layouts; the icons and accessible names remain. */
  compact?: boolean;
  className?: string;
}

/** System, light or dark, as three pressed-or-not buttons. */
export function ThemeSwitch({ value, onChange, compact = false, className }: ThemeSwitchProps) {
  return (
    <div role="group" aria-label="Theme" className={cx("flex rounded-control border border-border bg-bg-sunken p-0.5", className)}>
      {THEME_CHOICES.map(({ value: choice, label }) => {
        const Icon = ICONS[choice];
        return (
          <button
            key={choice}
            type="button"
            aria-pressed={value === choice}
            onClick={() => {
              onChange(choice);
            }}
            className={cx(
              "flex h-7 flex-1 cursor-pointer items-center justify-center gap-1.5 rounded-[4px] px-2.5 text-12 font-medium text-fg-muted",
              "hover:text-fg focus-visible:outline-2 focus-visible:outline-focus",
              "aria-pressed:bg-surface aria-pressed:text-fg aria-pressed:shadow-raised",
            )}
          >
            <Icon aria-hidden="true" className="size-3.5" />
            <span className={cx(compact && "max-sm:sr-only")}>{label}</span>
          </button>
        );
      })}
    </div>
  );
}
