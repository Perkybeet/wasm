import { Outlet, createFileRoute } from "@tanstack/react-router";

import { LinkTabs } from "../../app/LinkTabs";
import { SETTINGS_TABS } from "../../app/nav";
import { PageHeader } from "../../app/PageHeader";

/** Settings, one section per URL. */
export const Route = createFileRoute("/_console/settings")({
  component: SettingsLayout,
});

function SettingsLayout() {
  return (
    <>
      <PageHeader title="Settings" description="How WASM runs on this machine and who can reach the console." />
      <LinkTabs label="Settings sections" tabs={SETTINGS_TABS} className="-mt-4 mb-8" />
      {/* Every settings page is a form: it keeps the measure it was designed at rather than
          stretching its fields across a wide screen. */}
      <div className="max-w-6xl">
        <Outlet />
      </div>
    </>
  );
}
