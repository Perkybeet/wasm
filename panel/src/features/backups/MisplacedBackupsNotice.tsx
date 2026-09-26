import { useQuery } from "@tanstack/react-query";
import { useId } from "react";

import { backupStorageQuery } from "../../api/queries/backups";
import { CommandHint } from "../../components/page/CommandHint";
import { StatusGlyph } from "../../components/ui/StatusPill";
import { formatCount } from "../../lib/format";

/**
 * Backups WASM found outside the configured backup directory - in the old default one, or
 * where an empty `backup.directory` once sent them - which this page does not list and a
 * restore cannot reach until they are imported. Each place comes with the exact command that
 * moves them in; nothing is shown when there are none.
 */
export function MisplacedBackupsNotice() {
  const storage = useQuery(backupStorageQuery());
  const headingId = useId();
  const misplaced = storage.data?.misplaced ?? [];
  if (storage.data === undefined || misplaced.length === 0) return null;
  const total = misplaced.reduce((sum, found) => sum + found.count, 0);

  return (
    <section aria-labelledby={headingId} className="flex min-w-0 flex-col gap-3 rounded-card border border-warn/40 bg-warn-soft/40 px-4 py-3.5">
      <div className="flex items-start gap-2">
        <StatusGlyph state="warning" className="mt-1 text-warn" />
        <div className="flex min-w-0 flex-col gap-1">
          <h2 id={headingId} className="text-14 font-semibold text-fg">
            {`${formatCount(total)} ${total === 1 ? "backup is" : "backups are"} outside the backup directory`}
          </h2>
          <p className="max-w-[72ch] text-13 text-pretty text-fg-muted">
            This page lists only the backups in{" "}
            <span translate="no" className="mono text-12 text-fg">
              {storage.data.path}
            </span>
            , so these cannot be restored from here until they are imported. Importing moves them into it.
          </p>
        </div>
      </div>
      <ul className="flex min-w-0 flex-col gap-3 pl-6">
        {misplaced.map((found) => (
          <li key={found.directory} className="flex min-w-0 flex-col gap-1.5">
            <p className="text-13 text-fg">
              {`${formatCount(found.count)} ${found.count === 1 ? "backup" : "backups"} in `}
              <span translate="no" className="mono text-12">
                {found.directory}
              </span>
            </p>
            <CommandHint command={found.command} label="Import them" />
          </li>
        ))}
      </ul>
      <p className="pl-6 text-12 text-fg-muted">
        Add{" "}
        <code translate="no" className="mono text-fg">
          --dry-run
        </code>{" "}
        to the command to see what would move first, without moving anything.
      </p>
    </section>
  );
}
