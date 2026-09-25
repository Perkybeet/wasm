import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { CircleUser, Keyboard, LogOut, Menu, Search } from "lucide-react";
import { useState } from "react";
import type { Ref } from "react";

import { sessionQuery } from "../api/queries/auth";
import { Button } from "../components/ui/Button";
import { IconButton } from "../components/ui/IconButton";
import { Kbd } from "../components/ui/Kbd";
import { LogoMark } from "../components/ui/Logo";
import { Mono } from "../components/ui/Mono";
import { Popover } from "../components/ui/Popover";
import { useSignOut } from "../features/auth/useSignOut";
import { MachineStrip } from "./MachineStrip";
import { modKeyLabel } from "./shortcuts";
import { ThemeSwitch } from "./ThemeSwitch";
import { useTheme } from "./theme";

export interface TopbarProps {
  onOpenPalette: () => void;
  onOpenShortcuts: () => void;
  onOpenNav: () => void;
  /** Receives the search trigger, so the palette can hand focus back to it. */
  searchTriggerRef?: Ref<HTMLButtonElement>;
}

function SessionPanel({ onOpenShortcuts }: { onOpenShortcuts: () => void }) {
  const [theme, setTheme] = useTheme();
  const [open, setOpen] = useState(false);
  const { data: session } = useQuery(sessionQuery());
  const { signOut, pending: signingOut } = useSignOut();

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      align="end"
      trigger={<IconButton label="Session and preferences" icon={<CircleUser />} tooltip={false} />}
      title="Session"
      description={
        session?.hostname !== undefined ? (
          <>
            Signed in to <Mono>{session.hostname}</Mono>
          </>
        ) : undefined
      }
      className="w-72"
    >
      <div className="flex flex-col gap-4">
        <div className="flex flex-col gap-1.5">
          <span className="text-12 font-medium text-fg-muted">Theme</span>
          <ThemeSwitch value={theme} onChange={setTheme} />
        </div>
        <div className="-mx-4 flex flex-col border-t border-border px-2 pt-2">
          <Button
            variant="ghost"
            icon={<Keyboard aria-hidden="true" />}
            trailingIcon={<Kbd className="ml-auto">?</Kbd>}
            className="justify-start"
            onClick={() => {
              setOpen(false);
              onOpenShortcuts();
            }}
          >
            Keyboard shortcuts
          </Button>
          <Button
            variant="ghost"
            icon={<LogOut aria-hidden="true" />}
            className="justify-start"
            loading={signingOut}
            onClick={() => void signOut()}
          >
            Sign out
          </Button>
        </div>
      </div>
    </Popover>
  );
}

/** The bar over every page: the machine strip, search, the session, and the menu on phones. */
export function Topbar({ onOpenPalette, onOpenShortcuts, onOpenNav, searchTriggerRef }: TopbarProps) {
  const mod = modKeyLabel();
  // Left padding of 26px on wide screens: with the hostname link's own 6px, the hostname
  // starts on the same edge as the page title below it.
  return (
    <header className="sticky top-0 z-30 flex h-14 shrink-0 items-center gap-3 border-b border-border bg-bg/85 px-4 backdrop-blur-md sm:px-6 lg:pr-6 lg:pl-6.5">
      <Link
        to="/"
        aria-label="Overview"
        className="-ml-1 flex shrink-0 rounded-control p-1 focus-visible:outline-2 focus-visible:outline-focus lg:hidden"
      >
        <LogoMark size={22} />
      </Link>

      <MachineStrip className="flex-1" />

      <div className="flex shrink-0 items-center gap-1.5">
        <button
          ref={searchTriggerRef}
          type="button"
          onClick={onOpenPalette}
          aria-haspopup="dialog"
          aria-keyshortcuts="Control+K Meta+K"
          className="flex h-8 w-52 cursor-pointer items-center gap-2 rounded-control border border-border bg-surface pr-1.5 pl-2.5 text-13 text-fg-muted shadow-raised transition-colors duration-(--duration-fast) ease-out hover:border-border-strong/60 hover:text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus max-md:hidden lg:w-44 xl:w-60"
        >
          <Search aria-hidden="true" className="size-4 shrink-0" />
          <span className="flex-1 text-left">Search</span>
          <span aria-hidden="true" className="flex gap-0.5">
            <Kbd>{mod}</Kbd>
            <Kbd>K</Kbd>
          </span>
        </button>
        <IconButton label="Search" icon={<Search />} onClick={onOpenPalette} tooltip={false} className="md:hidden" />
        <SessionPanel onOpenShortcuts={onOpenShortcuts} />
        <IconButton label="Open menu" icon={<Menu />} onClick={onOpenNav} tooltip={false} className="lg:hidden" />
      </div>
    </header>
  );
}
