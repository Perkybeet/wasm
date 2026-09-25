import { TriangleAlert } from "lucide-react";

import type { MigrationPlan } from "../../../api/queries/apps";
import { KeyValueList } from "../../../components/page/KeyValueList";
import type { KeyValueItem } from "../../../components/page/KeyValueList";
import { formatBytes, formatCount } from "../../../lib/format";

/** How the paths kept in shared/ were chosen, as the plan's `persistent_source` says. */
const PERSISTENT_SOURCE: Readonly<Record<string, string>> = {
  git: "Everything git does not track in the tree, minus build output",
  explicit: "The paths named for this migration",
  common: "The usual upload directories found in the tree",
};

function plural(count: number, one: string, many: string): string {
  return `${formatCount(count)} ${count === 1 ? one : many}`;
}

/** The rows of a plan, in the order the migration does them. */
export function planItems(plan: MigrationPlan): KeyValueItem[] {
  const items: KeyValueItem[] = [
    {
      label: "First release",
      value: plan.release_id,
      hint: "Named when the migration runs; this is the name it would get now",
    },
    { label: "Commit", value: plan.commit ?? "Not a git checkout", mono: plan.commit !== null && plan.commit !== undefined, copy: false },
    {
      label: "Kept in shared/",
      value: plan.persistent.length > 0 ? plan.persistent.join(", ") : "Nothing",
      mono: plan.persistent.length > 0,
      copy: false,
      hint: PERSISTENT_SOURCE[plan.persistent_source] ?? plan.persistent_source,
    },
    {
      label: "Environment",
      value: plan.env_files.length > 0 ? plan.env_files.join(", ") : "No environment file",
      mono: plan.env_files.length > 0,
      copy: false,
      hint: plan.env_files.length > 0 ? "Moved to shared/ and linked into every release" : undefined,
    },
    {
      label: "Unit",
      value: plan.unit ? `${plan.unit}${plan.unit_rewrite ? ", rewritten to run from current" : ", unchanged"}` : "None, the web server serves it",
      mono: false,
      copy: false,
    },
    { label: "Site", value: plan.site_rewrite ? "Rewritten to serve current" : "Unchanged", mono: false, copy: false },
    {
      label: "Files",
      value: `${plural(plan.files, "file", "files")}, ${formatBytes(plan.bytes)}`,
      mono: false,
      copy: false,
      hint: "All kept: moved into place, never copied or deleted",
    },
  ];
  if (plan.untracked_files.length > 0) {
    items.push({
      label: "Only in the first release",
      value: plural(plan.untracked_files.length, "untracked file", "untracked files"),
      mono: false,
      copy: false,
      hint: plan.untracked_files.slice(0, 5).join(", ") + (plan.untracked_files.length > 5 ? ", and more" : ""),
    });
  }
  return items;
}

/** One of the plan's warnings, verbatim: what to read before going ahead. */
export function PlanWarning({ children }: { children: string }) {
  return (
    <div className="flex items-start gap-2.5 rounded-control border border-warn/40 bg-warn-soft px-3 py-2.5">
      <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warn" />
      <p className="min-w-0 text-13 text-pretty break-words text-fg">{children}</p>
    </div>
  );
}

/**
 * What migrating an in-place app to releases would do, read from the disk: the warnings first,
 * then each change in the order it happens.
 */
export function MigrationPlanView({ plan }: { plan: MigrationPlan }) {
  return (
    <div className="flex min-w-0 flex-col gap-3">
      {plan.warnings.length > 0 ? (
        <div className="flex flex-col gap-2">
          <h4 className="sr-only">Warnings</h4>
          {plan.warnings.map((warning) => (
            <PlanWarning key={warning}>{warning}</PlanWarning>
          ))}
        </div>
      ) : null}
      <KeyValueList items={planItems(plan)} />
    </div>
  );
}
