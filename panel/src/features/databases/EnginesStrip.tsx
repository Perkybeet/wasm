import { useQuery } from "@tanstack/react-query";
import { Download, Play, RotateCw, Square } from "lucide-react";
import type { ReactNode } from "react";

import type { Engine } from "../../api/queries/databases";
import { enginesQuery } from "../../api/queries/databases";
import type { Job } from "../../api/queries/jobs";
import { QueryState } from "../../components/page/QueryState";
import { Section } from "../../components/page/Section";
import { Button } from "../../components/ui/Button";
import { Skeleton } from "../../components/ui/Skeleton";
import { StatusGlyph } from "../../components/ui/StatusPill";
import type { Status } from "../../components/ui/StatusPill";
import { cx } from "../../lib/cx";
import { useEngineActions, useEngineJob } from "./useEngineActions";

interface EngineView {
  state: Status;
  label: string;
  tone: string;
}

function viewOf(engine: Engine, installing: boolean): EngineView {
  if (installing) return { state: "deploying", label: "Installing", tone: "text-warn" };
  if (!engine.installed) return { state: "unknown", label: "Not installed", tone: "text-fg-faint" };
  return engine.running
    ? { state: "running", label: "Running", tone: "text-ok" }
    : { state: "stopped", label: "Stopped", tone: "text-idle" };
}

function EngineTile({ engine }: { engine: Engine }) {
  const { install, start, stop, restart } = useEngineActions();
  const job = useEngineJob(engine.name);
  const installing = job.running !== null;
  const view = viewOf(engine, installing);

  let actions: ReactNode;
  if (installing) {
    actions = null;
  } else if (!engine.installed) {
    actions = (
      <Button
        size="sm"
        icon={<Download aria-hidden="true" />}
        loading={install.isPending}
        onClick={() =>
          install.mutate(engine.name, {
            onSuccess: (result) => {
              job.track(result.job as Job);
            },
          })
        }
      >
        Install
      </Button>
    );
  } else if (engine.running) {
    actions = (
      <>
        <Button size="sm" icon={<Square aria-hidden="true" />} loading={stop.isPending} onClick={() => stop.mutate(engine.name)}>
          Stop
        </Button>
        <Button size="sm" icon={<RotateCw aria-hidden="true" />} loading={restart.isPending} onClick={() => restart.mutate(engine.name)}>
          Restart
        </Button>
      </>
    );
  } else {
    actions = (
      <Button size="sm" icon={<Play aria-hidden="true" />} loading={start.isPending} onClick={() => start.mutate(engine.name)}>
        Start
      </Button>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-3 rounded-card border border-border bg-surface px-4 py-3.5 shadow-raised">
      <div className="min-w-0">
        <p className="truncate text-14 font-medium text-fg">{engine.display_name}</p>
        <p className={cx("mt-1 flex items-center gap-1.5 text-12", view.tone)}>
          <StatusGlyph state={view.state} size={10} />
          {view.label}
        </p>
      </div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-12">
        <div className="flex flex-col gap-0.5">
          <dt className="text-fg-faint">Version</dt>
          <dd translate="no" className="mono truncate text-fg">
            {engine.installed ? (engine.version ?? "Unknown") : "-"}
          </dd>
        </div>
        <div className="flex flex-col gap-0.5">
          <dt className="text-fg-faint">Port</dt>
          <dd translate="no" className="mono text-fg">
            {engine.port}
          </dd>
        </div>
      </dl>
      <div className="mt-auto flex flex-wrap items-center gap-2 pt-1 empty:hidden">{actions}</div>
    </div>
  );
}

function EnginesSkeleton() {
  return (
    <div aria-hidden="true" className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="flex flex-col gap-3 rounded-card border border-border bg-surface px-4 py-3.5">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-3 w-16" />
          <Skeleton className="h-7 w-20" />
        </div>
      ))}
    </div>
  );
}

/** Every engine WASM can manage, installed or not, with what its unit can be told to do. */
export function EnginesStrip() {
  const engines = useQuery(enginesQuery());
  return (
    <Section title="Engines" description="Database servers WASM can install and control on this machine.">
      <QueryState query={engines} label="engines" skeleton={<EnginesSkeleton />}>
        {(data) => (
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {data.engines.map((engine) => (
              <EngineTile key={engine.name} engine={engine} />
            ))}
          </div>
        )}
      </QueryState>
    </Section>
  );
}
