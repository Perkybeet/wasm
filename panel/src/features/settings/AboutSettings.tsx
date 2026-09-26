import { useQuery } from "@tanstack/react-query";
import { CircleArrowUp, CircleCheck, CircleHelp, ExternalLink, RotateCw } from "lucide-react";
import type { ReactNode } from "react";

import { sessionQuery } from "../../api/queries/auth";
import { configQuery } from "../../api/queries/config";
import { versionQuery } from "../../api/queries/system";
import type { ResponseOf } from "../../api/client";
import { useDocumentTitle } from "../../app/documentTitle";
import { CommandHint } from "../../components/page/CommandHint";
import { KeyValueList } from "../../components/page/KeyValueList";
import { ErrorBlock } from "../../components/page/QueryState";
import { Section, Sections } from "../../components/page/Section";
import { Button } from "../../components/ui/Button";
import { Skeleton } from "../../components/ui/Skeleton";
import { isHttpUrl } from "../../lib/url";

type UpdateInfo = ResponseOf<"/api/system/version", "get">;

const REPOSITORY = "https://github.com/Perkybeet/wasm";

const LINKS: readonly { label: string; href: string; description: string }[] = [
  { label: "Documentation", href: `${REPOSITORY}#readme`, description: "Installing, deploying and every command." },
  { label: "Release notes", href: `${REPOSITORY}/releases`, description: "What changed in each version." },
  { label: "Report a problem", href: `${REPOSITORY}/issues`, description: "Include the output of wasm health." },
  { label: "License", href: `${REPOSITORY}/blob/main/LICENSE`, description: "GNU AGPL 3.0 or later: free to use, study and change, commercially too; a modified version offered to others as a service must publish its source." },
];

/** What the console does, and the command that does it from a terminal. */
const TERMINAL: readonly { task: string; command: string }[] = [
  { task: "Show the configuration in effect", command: "wasm config show" },
  { task: "Read one setting", command: "wasm config get backup.max_per_app" },
  { task: "Change one setting", command: "wasm config set ssl.email ops@example.com" },
  { task: "Where the configuration file is", command: "wasm config path" },
  { task: "Check this machine", command: "wasm health" },
  { task: "Console status", command: "wasm web status" },
  { task: "Restart the console", command: "wasm web restart" },
  { task: "Issue a new access token", command: "wasm web token --new" },
  { task: "Installed version", command: "wasm --version" },
];

function UpdateState({ info }: { info: UpdateInfo }): ReactNode {
  if (info.has_update && info.latest_version) {
    return (
      <div className="flex min-w-0 flex-col gap-3">
        <p className="flex items-center gap-2 text-14 font-medium text-fg">
          <CircleArrowUp aria-hidden="true" className="size-4 shrink-0" />
          {`Version ${info.latest_version} is available`}
        </p>
        {info.update_command ? <CommandHint label="Update from a terminal" command={info.update_command} /> : null}
        {info.release_url && isHttpUrl(info.release_url) ? (
          <a
            href={info.release_url}
            target="_blank"
            rel="noreferrer"
            className="flex w-fit items-center gap-1 rounded-[4px] text-13 font-medium text-accent-fg hover:underline hover:underline-offset-2 focus-visible:outline-2 focus-visible:outline-focus"
          >
            {`What is new in ${info.latest_version}`}
            <ExternalLink aria-hidden="true" className="size-3.5" />
            <span className="sr-only">(opens in a new tab)</span>
          </a>
        ) : null}
      </div>
    );
  }
  if (info.latest_version) {
    return (
      <p className="flex items-center gap-2 text-14 text-fg">
        <CircleCheck aria-hidden="true" className="size-4 shrink-0 text-ok" />
        {`Up to date. ${info.latest_version} is the latest release.`}
      </p>
    );
  }
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <p className="flex items-center gap-2 text-14 text-fg">
        <CircleHelp aria-hidden="true" className="size-4 shrink-0 text-idle" />
        Could not find out whether a newer version exists.
      </p>
      <p className="text-13 text-fg-muted">
        The server asks GitHub for the latest release. Check that it can reach api.github.com, then check again.
      </p>
    </div>
  );
}

function VersionSection() {
  const version = useQuery(versionQuery());
  const { data: session } = useQuery(sessionQuery());
  const installed = version.data?.current_version ?? session?.version;
  return (
    <Section
      title="Version and updates"
      description="The installed version of WASM, compared with the latest release. The server keeps the answer for a few minutes."
      actions={
        <Button
          size="sm"
          icon={<RotateCw aria-hidden="true" />}
          loading={version.isFetching}
          onClick={() => void version.refetch()}
        >
          Check again
        </Button>
      }
    >
      <div className="grid gap-x-10 gap-y-5 rounded-card border border-border bg-surface p-5 shadow-raised sm:grid-cols-[auto_minmax(0,1fr)]">
        <div className="flex flex-col gap-1">
          <span className="text-13 text-fg-muted">Installed</span>
          {installed === undefined ? (
            <Skeleton className="h-8 w-24" />
          ) : (
            <span translate="no" className="mono text-24 leading-8 text-fg tabular-nums">
              {installed}
            </span>
          )}
        </div>
        <div aria-live="polite" className="flex min-w-0 items-center sm:border-l sm:border-border sm:pl-10">
          {version.data !== undefined ? (
            <UpdateState info={version.data} />
          ) : version.isError ? (
            <ErrorBlock compact error={version.error} title="Could not check for updates" className="w-full" />
          ) : (
            <div aria-busy="true" className="flex flex-col gap-2">
              <span className="sr-only">Checking for updates</span>
              <Skeleton className="h-4 w-56" />
              <Skeleton className="h-3 w-40" />
            </div>
          )}
        </div>
      </div>
    </Section>
  );
}

function InstallationSection() {
  const { data: session } = useQuery(sessionQuery());
  const config = useQuery(configQuery());
  return (
    <Section title="This installation">
      <div className="rounded-card border border-border bg-surface px-5 py-2 shadow-raised">
        <KeyValueList
          items={[
            { label: "Machine", value: session?.hostname ?? "" },
            { label: "Console address", value: window.location.origin },
            { label: "Configuration file", value: config.data?.path ?? "" },
          ]}
          empty="Loading"
        />
      </div>
    </Section>
  );
}

function LinksSection() {
  return (
    <Section title="Links">
      <ul className="grid gap-3 sm:grid-cols-2">
        {LINKS.map((link) => (
          <li key={link.href}>
            <a
              href={link.href}
              target="_blank"
              rel="noreferrer"
              className="group flex h-full flex-col gap-0.5 rounded-card border border-border bg-surface px-4 py-3 shadow-raised hover:bg-surface-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
            >
              <span className="flex items-center gap-1.5 text-14 font-medium text-fg">
                {link.label}
                <ExternalLink aria-hidden="true" className="size-3.5 text-fg-faint group-hover:text-fg-muted" />
                <span className="sr-only">(opens in a new tab)</span>
              </span>
              <span className="text-13 text-fg-muted">{link.description}</span>
            </a>
          </li>
        ))}
      </ul>
    </Section>
  );
}

function TerminalSection() {
  return (
    <Section title="From a terminal" description="Everything on these pages has a command. These are the ones about WASM itself.">
      <dl className="flex flex-col divide-y divide-border rounded-card border border-border bg-surface px-5 py-1 shadow-raised">
        {TERMINAL.map((row) => (
          <div key={row.command} className="grid min-w-0 items-center gap-x-6 gap-y-1 py-2.5 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]">
            <dt className="text-13 text-fg-muted">{row.task}</dt>
            <dd className="min-w-0">
              <CommandHint command={row.command} />
            </dd>
          </div>
        ))}
      </dl>
    </Section>
  );
}

/** Settings > About: the version, whether a newer one exists, where to read more. */
export function AboutSettings() {
  useDocumentTitle("About", 1);
  return (
    <Sections>
      <VersionSection />
      <InstallationSection />
      <TerminalSection />
      <LinksSection />
    </Sections>
  );
}
