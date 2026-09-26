import type { App } from "../../../api/queries/apps";
import { CommandHint } from "../../../components/page/CommandHint";
import { KeyValueList } from "../../../components/page/KeyValueList";
import type { KeyValueItem } from "../../../components/page/KeyValueList";
import { Section } from "../../../components/page/Section";
import { appStatus } from "../../../components/page/status";
import { sourceLink } from "../SourceLink";
import { PANEL } from "./panel";

/**
 * What the app is and how it runs: the type, the repository and branch it was deployed from,
 * the build and start commands its deployer runs, and where and as what it runs.
 */
export function SourceSection({ app }: { app: App }) {
  const staticSite = appStatus(app.status).state === "static";
  const argv = app.build_command ?? [];
  const buildCommand = argv.length > 0 ? argv.join(" ") : "None";
  const items: KeyValueItem[] = [
    { label: "Type", value: app.app_type ?? null },
    ...(staticSite ? [] : [{ label: "Port", value: app.port ?? null }]),
    { label: "Source", value: sourceLink(app.source ?? null), mono: true, copy: app.source ?? false },
    { label: "Branch", value: app.branch ?? null },
    { label: "Build command", value: buildCommand, mono: buildCommand !== "None", copy: buildCommand === "None" ? false : buildCommand },
    ...(staticSite ? [] : [{ label: "Start command", value: app.start_command ?? null }]),
    { label: "Directory", value: app.path ?? null },
    {
      label: "Layout",
      value: app.layout === "releases" ? "Releases" : "In place",
      mono: false,
      copy: false,
      hint: app.layout === "releases" ? "Each deploy is a release; rollback is instant" : "Updated in its directory",
    },
    ...(staticSite
      ? [{ label: "Served by", value: "The web server, no process", mono: false, copy: false as const }]
      : [
          { label: "Unit", value: app.unit ?? null },
          { label: "Runs as", value: app.run_as ?? null },
          { label: "Starts at boot", value: app.enabled ? "Yes" : "No", mono: false, copy: false as const },
        ]),
  ];

  return (
    <Section title="Source and runtime" description="What the app is and how it runs.">
      <div className={`${PANEL} px-4 py-1`}>
        <KeyValueList empty="Not recorded" items={items} />
      </div>
      <div className="flex min-w-0 flex-col gap-2">
        <p className="text-13 text-pretty text-fg-muted">Deploying a different branch is not offered here yet.</p>
        <CommandHint command={`wasm update ${app.domain} --branch <branch>`} label="From a terminal" />
      </div>
    </Section>
  );
}
