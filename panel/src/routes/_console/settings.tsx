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
      <Outlet />
    </>
  );
}
