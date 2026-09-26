import { CircleCheck, FolderOpen, GitBranch, GitCommitHorizontal, TriangleAlert } from "lucide-react";

import { Badge } from "../../components/ui/Badge";
import { Suggestion } from "./Suggestion";
import { sourceKind, typeName } from "./wizard";
import type { AppTypeOption, Inspection } from "./wizard";

function Command({ step, argv, none }: { step: string; argv: string | null; none: string }) {
  return (
    <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-baseline gap-x-3 py-1 sm:grid-cols-[5rem_minmax(0,1fr)]">
      <dt className="text-12 text-fg-muted">{step}</dt>
      <dd className="min-w-0">
        {argv === null || argv === "" ? (
          <span className="text-12 text-fg-faint">{none}</span>
        ) : (
          <code translate="no" className="block truncate text-12 text-fg" title={argv}>
            <span aria-hidden="true" className="text-fg-faint select-none">
              ${" "}
            </span>
            {argv}
          </code>
        )}
      </dd>
    </div>
  );
}

/**
 * Whether this server can deploy the source as it is, in the backend's words: its verdict, and
 * when something has to be done first, what. Only an explicit `compatible: false` warns; an
 * inspection that did not say (an older backend) shows nothing rather than a guess.
 */
function Verdict({ inspection }: { inspection: Inspection }) {
  const verdict = inspection.verdict ?? null;
  const suggestion = inspection.suggestion ?? null;
  if (verdict === null && suggestion === null) return null;
  const blocked = inspection.compatible === false;
  return (
    <div
      className={
        blocked
          ? "flex items-start gap-2.5 rounded-control border border-warn/40 bg-warn-soft px-3 py-2.5"
          : "flex items-start gap-2.5"
      }
    >
      {blocked ? (
        <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warn" />
      ) : inspection.compatible === true ? (
        <CircleCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-ok" />
      ) : null}
      <div className="flex min-w-0 flex-col gap-1.5">
        {verdict !== null ? (
          <p className="text-13 text-pretty text-fg">
            {blocked ? <span className="sr-only">Not deployable as it is: </span> : null}
            {verdict}
          </p>
        ) : null}
        {suggestion !== null ? <Suggestion text={suggestion} /> : null}
      </div>
    </div>
  );
}

/**
 * What the inspection found, before any choice: the source and revision, and the commands the
 * detected type's deployer would run, exactly as it would run them.
 */
export function InspectionReadout({ inspection, types, source }: { inspection: Inspection; types: readonly AppTypeOption[]; source: string }) {
  const local = sourceKind(source) === "local";
  const keys = inspection.env_keys;
  const required = keys.filter((key) => key.required).length;
  const secrets = keys.filter((key) => key.secret).length;
  const others = inspection.detected_types.slice(1);

  return (
    <section aria-label="What WASM found" className="overflow-hidden rounded-card border border-border bg-surface shadow-raised">
      <header className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border px-4 py-3">
        <span className="flex min-w-0 flex-1 basis-64 items-center gap-2">
          {local ? (
            <FolderOpen aria-hidden="true" className="size-4 shrink-0 text-fg-muted" />
          ) : (
            <GitBranch aria-hidden="true" className="size-4 shrink-0 text-fg-muted" />
          )}
          <code translate="no" className="truncate text-12 text-fg" title={source}>
            {source}
          </code>
        </span>
        <span className="flex flex-wrap items-center gap-1.5">
          {inspection.branch ? (
            <Badge mono>
              <GitBranch aria-hidden="true" className="size-3" />
              <span className="sr-only">Branch </span>
              {inspection.branch}
            </Badge>
          ) : null}
          {inspection.commit ? (
            <Badge mono>
              <GitCommitHorizontal aria-hidden="true" className="size-3" />
              <span className="sr-only">Commit </span>
              {inspection.commit}
            </Badge>
          ) : inspection.detected_types.length > 0 ? (
            <span className="text-12 text-fg-faint">Not a Git checkout</span>
          ) : null}
        </span>
      </header>
      {inspection.detected_types.length === 0 ? (
        <p className="px-4 py-3 text-13 text-pretty text-fg">
          No type recognised this source. Choose one below: its deployer decides the install, build and start
          commands when it runs.
        </p>
      ) : (
        <div className="flex flex-col gap-3 px-4 py-3">
          <Verdict inspection={inspection} />
          <p className="text-13 text-pretty text-fg">
            {`Looks like a ${typeName(types, inspection.app_type)} app`}
            {inspection.package_manager ? (
              <>
                {" using "}
                <code className="text-12">{inspection.package_manager}</code>
              </>
            ) : null}
            {/* The verdict names the other types itself, with what choosing one would mean. */}
            {others.length > 0 && !inspection.verdict ? (
              <span className="text-fg-muted">{`. It also matches ${others.map((type) => typeName(types, type)).join(", ")}.`}</span>
            ) : (
              "."
            )}
          </p>
          <dl className="flex flex-col rounded-control border border-border bg-bg-sunken px-3 py-1.5">
            <Command step="Install" argv={inspection.install_command.join(" ")} none="Nothing to install" />
            <Command step="Build" argv={inspection.build_command.join(" ")} none="Nothing to build" />
            <Command step="Start" argv={inspection.start_command} none="No process: the web server serves the files" />
          </dl>
          <p className="text-12 text-fg-muted">
            {keys.length === 0
              ? "No .env.example, so no variables were proposed."
              : `${String(keys.length)} ${keys.length === 1 ? "variable" : "variables"} in .env.example, ${String(required)} without a default, ${String(secrets)} that look secret.`}
          </p>
        </div>
      )}
    </section>
  );
}
