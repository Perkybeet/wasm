import { Outlet, createFileRoute } from "@tanstack/react-router";

import { LinkTabs } from "../../../app/LinkTabs";
import { APP_TABS } from "../../../app/nav";
import { PageHeader } from "../../../app/PageHeader";

/** One application: its header and the tabs that divide it. Each tab is a URL. */
export const Route = createFileRoute("/_console/apps/$domain")({
  component: AppLayout,
});

function AppLayout() {
  const { domain } = Route.useParams();
  return (
    <>
      <PageHeader title={domain} breadcrumbs={[{ label: "Applications", to: "/apps" }]} />
      <LinkTabs
        label="Application sections"
        tabs={APP_TABS.map((tab) => ({ ...tab, params: { domain } }))}
        className="-mt-4 mb-8"
      />
      <Outlet />
    </>
  );
}
