import { useState } from "react";

import { ApiError } from "../api/errors";
import {
  AppStatePill,
  CommandHint,
  DangerAction,
  DangerZone,
  DeployStatePill,
  ErrorBlock,
  KeyValueList,
  KeyValueListSkeleton,
  QueryState,
  RelativeTime,
  ResourceMeter,
  Section as PageSection,
  SegmentedControl,
  StatTile,
} from "../components/page";
import type { QueryLike } from "../components/page";
import { Badge, Button, ConfirmDialog, StatusPill } from "../components/ui";
import { formatBytes, formatBytesRate, formatCount, formatDuration, formatPercent } from "../lib/format";
import { Item, Row, Section, Stage } from "./gallery";
import { SAMPLE_APPS } from "./sample";

const MB = 1024 * 1024;

/** Moments relative to when the gallery opened, so the live labels have something to count. */
const OPENED = Date.now();
const minutesAgo = (minutes: number) => new Date(OPENED - minutes * 60_000);

function Sections() {
  return (
    <Section
      id="page-section"
      title="Section"
      description="The unit of every page: a heading, an optional line of description and the section's actions, then the content 16px below. Sections sit 32px apart. Each is a region named by its heading."
    >
      <Stage plain>
        <div className="flex flex-col gap-8">
          <PageSection
            level={3}
            title="Needs attention"
            badge={<Badge tone="fail">2</Badge>}
            actions={<Button size="sm">Refresh</Button>}
          >
            <div className="h-16 rounded-card border border-dashed border-border" />
          </PageSection>
          <PageSection level={3} title="Machine" description="CPU, memory, network and disk over time.">
            <div className="h-16 rounded-card border border-dashed border-border" />
          </PageSection>
        </div>
      </Stage>
    </Section>
  );
}

function Facts() {
  return (
    <Section
      id="key-value"
      title="Key-value list"
      description="Facts about one thing, one per row. System values are mono, cut to one line with the full value on hover, and copyable: the copy button appears on hover and keyboard focus, and always on touch screens."
    >
      <Stage plain>
        <div className="grid gap-6 md:grid-cols-2">
          <div className="rounded-card border border-border bg-surface px-4 py-1 shadow-raised">
            <KeyValueList
              items={[
                { label: "Port", value: 3001 },
                { label: "Main PID", value: 41002 },
                { label: "Starts at boot", value: "Yes", mono: false, copy: false },
                { label: "Layout", value: "Releases", mono: false, copy: false, hint: "Each deploy is a release; rollback is instant" },
                { label: "Directory", value: "/var/www/apps/shop.arenna.dev/releases/20260925-143012-a1b2c3d" },
                { label: "Unit user", value: null },
              ]}
            />
          </div>
          <div className="rounded-card border border-border bg-surface px-4 py-1 shadow-raised">
            <KeyValueListSkeleton rows={6} />
          </div>
        </div>
      </Stage>
    </Section>
  );
}

function Times() {
  return (
    <Section
      id="time"
      title="Relative time"
      description="When something happened, kept current while on screen, with the exact moment on hover. A timestamp the console cannot place (systemd in a named zone) is shown verbatim, never guessed."
    >
      <Stage>
        <Row>
          <Item label="Seconds, ticking">
            <RelativeTime value={new Date(OPENED - 20_000)} className="text-14" />
          </Item>
          <Item label="Minutes">
            <RelativeTime value={minutesAgo(12)} className="text-14" />
          </Item>
          <Item label="Hours">
            <RelativeTime value={minutesAgo(300)} className="text-14" />
          </Item>
          <Item label="Weeks">
            <RelativeTime value={minutesAgo(60 * 24 * 20)} className="text-14" />
          </Item>
          <Item label="Unplaceable">
            <RelativeTime value="Fri 2026-09-25 13:06:35 CEST" className="text-14" />
          </Item>
          <Item label="Missing">
            <RelativeTime value={null} fallback="Never deployed" className="text-14" />
          </Item>
        </Row>
      </Stage>
      <Stage>
        <Row>
          <Item label="formatBytes">
            <span className="mono text-13">{`${formatBytes(1536)} / ${formatBytes(96 * MB)} / ${formatBytes(6_613_762_048)}`}</span>
          </Item>
          <Item label="formatBytesRate">
            <span className="mono text-13">{formatBytesRate(1_258_291)}</span>
          </Item>
          <Item label="formatPercent">
            <span className="mono text-13">{`${formatPercent(5.7)} / ${formatPercent(31.5)}`}</span>
          </Item>
          <Item label="formatDuration">
            <span className="mono text-13">{`${formatDuration(0.004)} / ${formatDuration(125)} / ${formatDuration(273_600)}`}</span>
          </Item>
          <Item label="formatCount">
            <span className="mono text-13">{`${formatCount(1284)} / ${formatCount(12_900)}`}</span>
          </Item>
        </Row>
      </Stage>
    </Section>
  );
}

type Shown = "loading" | "error" | "empty" | "loaded";

const FAILURE = new ApiError(
  502,
  "internal",
  "certbot: error: unable to reach https://acme-v02.api.letsencrypt.org/directory: Connection timed out",
  "Check that this machine can reach the internet on port 443, then try again.",
);

function States() {
  const [shown, setShown] = useState<Shown>("loading");
  const query: QueryLike<string[]> = {
    data: shown === "loaded" ? SAMPLE_APPS.map((app) => app.domain) : shown === "empty" ? [] : undefined,
    error: shown === "error" ? FAILURE : null,
    isPending: shown === "loading",
    isError: shown === "error",
    refetch: () => undefined,
  };
  return (
    <Section
      id="query-state"
      title="Query state"
      description="One wrapper for the four states of anything loaded: a skeleton shaped like the content, the failure with its fix above and the system's words verbatim below, the empty state, the content."
    >
      <Stage plain>
        <div className="flex flex-col gap-4">
          <SegmentedControl
            label="State shown"
            value={shown}
            onValueChange={setShown}
            options={[
              { value: "loading", label: "Loading" },
              { value: "error", label: "Error" },
              { value: "empty", label: "Empty" },
              { value: "loaded", label: "Loaded" },
            ]}
            className="self-start"
          />
          <QueryState
            query={query}
            label="applications"
            skeleton={
              <div className="rounded-card border border-border bg-surface px-4 py-1">
                <KeyValueListSkeleton rows={3} />
              </div>
            }
            isEmpty={(rows) => rows.length === 0}
            empty={<p className="rounded-card border border-dashed border-border px-4 py-6 text-center text-13 text-fg-muted">No applications yet.</p>}
          >
            {(rows) => (
              <ul className="divide-y divide-border rounded-card border border-border bg-surface text-13">
                {rows.slice(0, 3).map((row) => (
                  <li key={row} className="px-4 py-2.5">
                    {row}
                  </li>
                ))}
              </ul>
            )}
          </QueryState>
          <ErrorBlock live compact error={FAILURE} title="Renewal of shop.arenna.dev failed" />
        </div>
      </Stage>
    </Section>
  );
}

function AppStates() {
  return (
    <Section
      id="app-state"
      title="App and deploy state"
      description="The backend says an app's state in three vocabularies (the API, the store, `wasm list`); AppStatePill draws each word in the one state language, and shows an unknown word verbatim. DeployStatePill does the same for deployments and jobs."
    >
      <Stage>
        <Row>
          {["running", "deploying", "failed", "stopped", "static", "Restarting", "No answer", "unknown", "degraded"].map((status) => (
            <Item key={status} label={status}>
              <AppStatePill status={status} />
            </Item>
          ))}
        </Row>
      </Stage>
      <Stage>
        <Row>
          {["queued", "running", "success", "failed", "rolled_back", "cancelled"].map((status) => (
            <Item key={status} label={status}>
              <DeployStatePill status={status} appearance="inline" />
            </Item>
          ))}
        </Row>
      </Stage>
    </Section>
  );
}

function Readings() {
  return (
    <Section
      id="resources"
      title="Stat tiles and resource meters"
      description="Tiles answer 'how is this doing' before the details do: a label, a value, one line of context. A resource meter measures use against the unit's limit, amber then red as it nears; without a limit the reading stands alone and says so."
    >
      <Stage plain>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatTile label="Current release" value="a1b2c3d" mono detail="main, deployed 12m ago" />
          <StatTile label="State" value={<StatusPill state="running" />} detail="Up 5h 14m" />
          <StatTile label="Uptime" value="12d 4h" detail="Since 2026-09-13 09:12:40" />
          <StatTile label="Certificate" value="Expired" detail="Valid until 2026-09-22" />
        </div>
      </Stage>
      <Stage plain>
        <div className="grid max-w-md gap-5 rounded-card border border-border bg-surface p-4">
          <ResourceMeter label="Memory" value={180 * MB} limit={512 * MB} format={formatBytes} />
          <ResourceMeter label="Memory, near its limit" value={420 * MB} limit={512 * MB} format={formatBytes} />
          <ResourceMeter label="CPU, at its quota" value={48} limit={50} format={formatPercent} />
          <ResourceMeter label="CPU, no limit" value={4.2} format={formatPercent} />
          <ResourceMeter label="Memory, stopped" value={null} limit={512 * MB} format={formatBytes} missing="Not running" />
        </div>
      </Stage>
    </Section>
  );
}

function Terminal() {
  return (
    <Section
      id="command"
      title="Command hint"
      description="The CLI command that does what this part of the console does, for operators who live in a terminal. The prompt is not copied."
    >
      <Stage>
        <div className="flex flex-col gap-4">
          <CommandHint command="wasm status shop.arenna.dev" label="From a terminal" />
          <CommandHint command="wasm create -d shop.arenna.dev -s git@github.com:arenna/shop.git -t nextjs --branch main" />
        </div>
      </Stage>
    </Section>
  );
}

function Danger() {
  return (
    <Section
      id="danger"
      title="Danger zone"
      description="Actions that cannot be undone, apart and last on the page, each saying what it destroys and what it keeps before its button does it."
    >
      <Stage plain>
        <DangerZone level={3} description="Nothing here can be undone.">
          <DangerAction
            level={4}
            title="Delete this application"
            description="Stops and removes the service, the site, the certificate and the files. Backups are kept."
            action={
              <ConfirmDialog
                title="Delete shop.arenna.dev"
                description="Stops and removes the service, the site, the certificate and the app's files. Backups are kept."
                confirmText="shop.arenna.dev"
                actionLabel="Delete application"
                onConfirm={() => new Promise((resolve) => setTimeout(resolve, 800))}
                trigger={<Button variant="danger">Delete application</Button>}
              />
            }
          />
          <DangerAction
            level={4}
            title="Remove the webhook"
            description="Pushes to the repository stop deploying. The secret is discarded; a new one is needed to turn it back on."
            action={<Button variant="danger">Remove webhook</Button>}
          />
        </DangerZone>
      </Stage>
    </Section>
  );
}

/** The page kit (src/components/page): what every page is built from, above the primitives. */
export function PageKit() {
  return (
    <>
      <Sections />
      <Facts />
      <Times />
      <States />
      <AppStates />
      <Readings />
      <Terminal />
      <Danger />
    </>
  );
}
