import type { App } from "../../../api/queries/apps";
import { CommandHint } from "../../../components/page/CommandHint";
import { KeyValueList } from "../../../components/page/KeyValueList";
import type { KeyValueItem } from "../../../components/page/KeyValueList";
import { Section } from "../../../components/page/Section";
import { appStatus } from "../../../components/page/status";
import { PANEL } from "./panel";

/**
 * What the app is and how it runs, read-only: the API reports the type, port, directory and
 * layout, but not the repository, the branch or the build and start commands, so those are
 * pointed to where they can be read and changed today, the terminal.
 */
export function SourceSection({ app }: { app: App }) {
  const staticSite = appStatus(app.status).state === "static";
  const items: KeyValueItem[] = [
    { label: "Type", value: app.app_type ?? null },
    ...(staticSite ? [] : [{ label: "Port", value: app.port ?? null }]),
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
        <p className="text-13 text-pretty text-fg-muted">
          The repository, branch and build and start commands are recorded by WASM but not shown here yet. From a terminal:
        </p>
        <CommandHint command={`wasm status ${app.domain}`} label="Show them" />
        <CommandHint command={`wasm update ${app.domain} --branch <branch>`} label="Deploy another branch" />
      </div>
    </Section>
  );
}
