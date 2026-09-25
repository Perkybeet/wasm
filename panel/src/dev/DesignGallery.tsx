import { Monitor, Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

import { Logo } from "../components/ui";
import { cx } from "../lib/cx";
import { Components } from "./Components";
import { Foundations } from "./Foundations";

type ThemeChoice = "system" | "light" | "dark";

const NAV: { title: string; links: [string, string][] }[] = [
  {
    title: "Foundations",
    links: [
      ["colour", "Colour"],
      ["state", "State language"],
      ["type", "Type"],
      ["space", "Space and shape"],
      ["elevation", "Elevation"],
    ],
  },
  {
    title: "Components",
    links: [
      ["button", "Button"],
      ["icon-button", "Icon button"],
      ["field", "Fields"],
      ["overlay", "Dialogs and drawers"],
      ["tabs", "Tabs and menus"],
      ["status", "Status and feedback"],
      ["badge", "Badges and values"],
      ["card", "Card and empty state"],
      ["table", "Data table"],
      ["chart", "Chart"],
      ["logs", "Log viewer"],
      ["logo", "Logo"],
    ],
  },
];

function initialTheme(): ThemeChoice {
  const param = new URLSearchParams(window.location.search).get("theme");
  return param === "light" || param === "dark" ? param : "system";
}

function ThemeSwitch({ value, onChange }: { value: ThemeChoice; onChange: (theme: ThemeChoice) => void }) {
  const options: [ThemeChoice, string, typeof Sun][] = [
    ["system", "System", Monitor],
    ["light", "Light", Sun],
    ["dark", "Dark", Moon],
  ];
  return (
    <div role="group" aria-label="Theme" className="flex rounded-control border border-border bg-bg-sunken p-0.5">
      {options.map(([choice, label, Icon]) => (
        <button
          key={choice}
          type="button"
          aria-pressed={value === choice}
          onClick={() => onChange(choice)}
          className={cx(
            "flex h-7 cursor-pointer items-center gap-1.5 rounded-[4px] px-2.5 text-12 font-medium text-fg-muted",
            "hover:text-fg focus-visible:outline-2 focus-visible:outline-focus",
            "aria-pressed:bg-surface aria-pressed:text-fg aria-pressed:shadow-raised",
          )}
        >
          <Icon aria-hidden="true" className="size-3.5" />
          <span className="max-sm:sr-only">{label}</span>
        </button>
      ))}
    </div>
  );
}

/**
 * Development-only review surface: every foundation and every component in its states. This
 * is where the design is judged, in both themes, before any page uses it.
 */
export function DesignGallery() {
  const [theme, setTheme] = useState<ThemeChoice>(initialTheme);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset["theme"];
    else root.dataset["theme"] = theme;
    return () => {
      delete root.dataset["theme"];
    };
  }, [theme]);

  return (
    <div className="min-h-dvh bg-bg text-fg">
      <header className="sticky top-0 z-30 border-b border-border bg-bg/85 backdrop-blur-md">
        <div className="mx-auto flex h-14 max-w-[1320px] items-center justify-between gap-4 px-6 max-sm:px-4">
          <div className="flex items-center gap-3">
            <Logo size="sm" />
            <span aria-hidden="true" className="h-4 w-px bg-border" />
            <span className="text-13 font-medium text-fg-muted">Design system</span>
          </div>
          <ThemeSwitch value={theme} onChange={setTheme} />
        </div>
      </header>

      <div className="mx-auto grid max-w-[1320px] gap-10 px-6 lg:grid-cols-[12rem_1fr] max-sm:px-4">
        <nav aria-label="Design system" className="sticky top-14 hidden max-h-[calc(100dvh-3.5rem)] self-start overflow-y-auto py-10 scroll-thin lg:block">
          {NAV.map((group) => (
            <div key={group.title} className="mb-6">
              <p className="mb-2 text-12 font-medium text-fg-faint">{group.title}</p>
              <ul className="flex flex-col">
                {group.links.map(([id, label]) => (
                  <li key={id}>
                    <a
                      href={`#${id}`}
                      className="-mx-2 block rounded-control px-2 py-1 text-13 text-fg-muted hover:bg-surface-hover hover:text-fg focus-visible:outline-2 focus-visible:outline-focus"
                    >
                      {label}
                    </a>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </nav>

        <main className="min-w-0 py-10">
          <div className="max-w-[62ch] pb-12">
            <h1 className="display text-32 text-fg">A precision instrument for one machine</h1>
            <p className="mt-3 text-16 text-pretty text-fg-muted">
              The console is dense where the operator works and generous where they decide. Surfaces are achromatic;
              colour is spent only on state and on what can be acted on.
            </p>
          </div>
          <Foundations />
          <Components />
        </main>
      </div>
    </div>
  );
}
