import { useQuery } from "@tanstack/react-query";

import { appQuery } from "../../../api/queries/apps";
import { useDocumentTitle } from "../../../app/documentTitle";
import { KeyValueListSkeleton } from "../../../components/page/KeyValueList";
import { Sections } from "../../../components/page/Section";
import { Skeleton } from "../../../components/ui/Skeleton";
import { DangerSection } from "./DangerSection";
import { LimitsSection } from "./LimitsSection";
import { PANEL } from "./panel";
import { ReleasesSection } from "./ReleasesSection";
import { SourceSection } from "./SourceSection";
import { WebhookSection } from "./WebhookSection";

function SettingsSkeleton() {
  return (
    <div aria-busy="true" className="flex max-w-6xl flex-col gap-8">
      <span className="sr-only">Loading the settings</span>
      <div aria-hidden="true" className="grid gap-8 lg:grid-cols-2">
        {[5, 3].map((rows) => (
          <div key={rows} className="flex flex-col gap-4">
            <Skeleton className="h-4 w-36" />
            <div className={`${PANEL} px-4 py-1`}>
              <KeyValueListSkeleton rows={rows} />
            </div>
          </div>
        ))}
      </div>
      <div aria-hidden="true" className="flex flex-col gap-4">
        <Skeleton className="h-4 w-32" />
        <Skeleton className="h-40 w-full rounded-card" />
      </div>
    </div>
  );
}

/**
 * What can be changed about one app, and what can only be read: how it is built and run, its
 * releases, the limits its unit runs under, the deploy webhook, and deleting it.
 */
export function SettingsTab({ domain }: { domain: string }) {
  useDocumentTitle(`Settings - ${domain}`, 1);
  const app = useQuery(appQuery(domain));

  // The layout owns the load failure and the not-found page; this shows the shape meanwhile.
  if (app.data === undefined) return app.isError ? null : <SettingsSkeleton />;

  // A form: it keeps the measure it was designed at instead of stretching its fields and
  // pushing its buttons away across a wide screen.
  return (
    <Sections className="max-w-6xl">
      <div className="grid gap-8 lg:grid-cols-2">
        <SourceSection app={app.data} />
        <ReleasesSection app={app.data} />
      </div>
      <LimitsSection app={app.data} />
      <WebhookSection app={app.data} />
      <DangerSection app={app.data} />
    </Sections>
  );
}
