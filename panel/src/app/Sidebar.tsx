import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";

import { sessionQuery } from "../api/queries/auth";
import { machineQuery } from "../api/queries/system";
import { Logo } from "../components/ui/Logo";
import { StatusGlyph } from "../components/ui/StatusPill";
import { cx } from "../lib/cx";
import { NAV_GROUPS, SETTINGS_ITEM } from "./nav";
import type { ConsolePath, NavItem } from "./nav";

/** Failures worth seeing from anywhere: a red count next to the place that has them. */
function useFailureCounts(): Partial<Record<ConsolePath, number>> {
  const { data } = useQuery(machineQuery());
  if (!data) return {};
  return { "/apps": data.apps.failed, "/services": data.units.failed };
}

function NavLink({ item, failed, onNavigate }: { item: NavItem; failed?: number | undefined; onNavigate?: (() => void) | undefined }) {
  const Icon = item.icon;
  return (
    <Link
      to={item.to}
      activeOptions={{ exact: item.to === "/", includeSearch: false }}
      {...(onNavigate ? { onClick: onNavigate } : {})}
      className={cx(
        "group flex h-8 items-center gap-2.5 rounded-control px-2 text-13 text-fg-muted",
        "transition-colors duration-(--duration-fast) ease-out",
        "hover:bg-surface-hover hover:text-fg",
        "focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus",
        "data-[status=active]:bg-surface-active data-[status=active]:font-medium data-[status=active]:text-fg",
      )}
    >
      <Icon aria-hidden="true" className="size-4 shrink-0 text-fg-faint group-hover:text-fg-muted group-data-[status=active]:text-fg" />
      <span className="min-w-0 flex-1 truncate">{item.label}</span>
      {/* Spaces between the parts keep the accessible name "Services 1 failed". */}
      {failed !== undefined && failed > 0 ? " " : null}
      {failed !== undefined && failed > 0 ? (
        <span className="inline-flex items-center gap-1 text-12 font-medium text-fail">
          <StatusGlyph state="failed" size={10} />
          <span className="mono">{failed}</span> <span className="sr-only">failed</span>
        </span>
      ) : null}
    </Link>
  );
}

/** The list of destinations, shared by the sidebar and the mobile menu. */
export function SidebarNav({ onNavigate, className }: { onNavigate?: () => void; className?: string }) {
  const failures = useFailureCounts();
  return (
    <nav aria-label="Main" className={cx("flex flex-col gap-5", className)}>
      {NAV_GROUPS.map((group) => (
        <ul key={group[0]?.to} className="flex flex-col gap-px">
          {group.map((item) => (
            <li key={item.to}>
              <NavLink item={item} failed={failures[item.to]} onNavigate={onNavigate} />
            </li>
          ))}
        </ul>
      ))}
    </nav>
  );
}

/** Settings and the installed version, pinned under the destinations. */
export function SidebarFooter({ onNavigate }: { onNavigate?: () => void }) {
  const { data: version } = useQuery({ ...sessionQuery(), select: (session) => session.version });
  return (
    <div className="flex flex-col gap-2">
      <ul>
        <li>
          <NavLink item={SETTINGS_ITEM} onNavigate={onNavigate} />
        </li>
      </ul>
      {version !== undefined ? (
        <p className="px-2 text-12 text-fg-faint">
          Version <span className="mono">{version}</span>
        </p>
      ) : null}
    </div>
  );
}

/** The permanent sidebar on wide screens. Narrow screens get the same lists in a drawer. */
export function Sidebar() {
  return (
    <aside className="sticky top-0 hidden h-dvh w-60 shrink-0 flex-col border-r border-border lg:flex">
      <div className="flex h-14 shrink-0 items-center border-b border-border px-5">
        <Link
          to="/"
          aria-label="WASM Console, overview"
          className="-mx-1.5 rounded-control px-1.5 py-1 focus-visible:outline-2 focus-visible:outline-focus"
        >
          <Logo size="sm" product="Console" />
        </Link>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-4 scroll-thin">
        <SidebarNav />
      </div>
      <div className="shrink-0 border-t border-border px-3 py-3">
        <SidebarFooter />
      </div>
    </aside>
  );
}
