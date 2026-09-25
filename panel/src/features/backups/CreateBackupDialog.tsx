import { useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import type { ReactElement, SyntheticEvent } from "react";

import { appsQuery } from "../../api/queries/apps";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { Checkbox } from "../../components/ui/Checkbox";
import { Dialog } from "../../components/ui/Dialog";
import { Field } from "../../components/ui/Field";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { useBackupActions } from "./useBackupActions";

export interface CreateBackupDialogProps {
  trigger: ReactElement<Record<string, unknown>>;
  /** Preselects the application; omit to let the operator choose. */
  domain?: string;
}

const REDIS_METHODS = [
  { value: "rdb", label: "RDB snapshot" },
  { value: "aof", label: "AOF log" },
];

/** Creates a backup of an application with the full option set `CreateBackupRequest` takes. */
export function CreateBackupDialog({ trigger, domain: fixedDomain }: CreateBackupDialogProps) {
  const formId = useId();
  const [open, setOpen] = useState(false);
  const apps = useQuery({ ...appsQuery(), enabled: open && fixedDomain === undefined });
  const [domain, setDomain] = useState(fixedDomain ?? "");
  const [description, setDescription] = useState("");
  const [includeEnv, setIncludeEnv] = useState(true);
  const [includeDatabase, setIncludeDatabase] = useState(false);
  const [includeNodeModules, setIncludeNodeModules] = useState(false);
  const [includeBuild, setIncludeBuild] = useState(false);
  const [includeDockerVolumes, setIncludeDockerVolumes] = useState(false);
  const [redisMethod, setRedisMethod] = useState("rdb");
  const [tags, setTags] = useState("");
  const { create } = useBackupActions();

  const reset = (): void => {
    setDomain(fixedDomain ?? "");
    setDescription("");
    setIncludeEnv(true);
    setIncludeDatabase(false);
    setIncludeNodeModules(false);
    setIncludeBuild(false);
    setIncludeDockerVolumes(false);
    setRedisMethod("rdb");
    setTags("");
    create.reset();
  };

  const close = (next: boolean): void => {
    if (!next && create.isPending) return;
    setOpen(next);
    if (!next) reset();
  };

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (domain.trim() === "") return;
    create.mutate(
      {
        domain: domain.trim(),
        description: description.trim(),
        includeEnv,
        includeNodeModules,
        includeBuild,
        includeDatabase,
        includeDockerVolumes,
        redisMethod,
        tags: tags
          .split(",")
          .map((tag) => tag.trim())
          .filter((tag) => tag !== ""),
      },
      { onSuccess: () => close(false) },
    );
  };

  const domainOptions = (apps.data?.apps ?? []).map((app) => ({ value: app.domain, label: app.domain }));

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      trigger={trigger}
      size="lg"
      title="Create backup"
      description="Archives the application's files, and anything else checked below, in one backup."
      footer={
        <>
          <Button disabled={create.isPending} onClick={() => close(false)}>
            Cancel
          </Button>
          <Button type="submit" form={formId} variant="primary" loading={create.isPending} disabled={domain.trim() === ""}>
            Create backup
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} className="flex flex-col gap-4">
        <div className="grid gap-4 sm:grid-cols-2">
          {fixedDomain === undefined ? (
            <Field label="Application" nativeLabel={false}>
              <Select
                aria-label="Application"
                value={domain}
                onValueChange={setDomain}
                placeholder={apps.isPending ? "Loading applications..." : "Choose an application"}
                options={domainOptions}
                disabled={apps.isPending || domainOptions.length === 0}
              />
            </Field>
          ) : null}
          <Field label="Description" optional>
            <Input value={description} onValueChange={setDescription} placeholder="Before the v2 migration" autoComplete="off" />
          </Field>
        </div>

        <fieldset className="flex flex-col gap-2.5">
          <legend className="mb-1 text-13 font-medium text-fg">Include</legend>
          <div className="grid grid-cols-2 gap-x-4 gap-y-2.5">
            <Checkbox checked={includeEnv} onCheckedChange={setIncludeEnv} label=".env files" description="On by default." />
            <Checkbox checked={includeDatabase} onCheckedChange={setIncludeDatabase} label="Databases" description="Every database it uses." />
            <Checkbox checked={includeDockerVolumes} onCheckedChange={setIncludeDockerVolumes} label="Docker volumes" description="Its own named volumes." />
            <Checkbox checked={includeNodeModules} onCheckedChange={setIncludeNodeModules} label="node_modules" description="Large; usually reinstalled." />
            <Checkbox checked={includeBuild} onCheckedChange={setIncludeBuild} label="Build artefacts" description="The compiled output." />
          </div>
        </fieldset>

        <div className="grid gap-4 sm:grid-cols-2">
          {includeDatabase ? (
            <Field label="Redis capture method" nativeLabel={false} description="Only for a Redis database included above.">
              <Select aria-label="Redis capture method" value={redisMethod} onValueChange={setRedisMethod} options={REDIS_METHODS} />
            </Field>
          ) : null}
          <Field label="Tags" optional description="Comma-separated, for filtering later.">
            <Input value={tags} onValueChange={setTags} placeholder="pre-deploy, manual" autoComplete="off" />
          </Field>
        </div>

        {create.isError ? <ErrorBlock live compact error={create.error} title="The backup was not queued" /> : null}
      </form>
    </Dialog>
  );
}
