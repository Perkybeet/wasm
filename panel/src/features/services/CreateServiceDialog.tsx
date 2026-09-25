import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { request } from "../../api/client";
import { serviceKeys } from "../../api/queries/services";
import { SegmentedControl } from "../../components/page/SegmentedControl";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Field } from "../../components/ui/Field";
import { IconButton } from "../../components/ui/IconButton";
import { Input } from "../../components/ui/Input";
import { Select } from "../../components/ui/Select";
import { Textarea } from "../../components/ui/Textarea";
import { toast } from "../../components/ui/toast";

type Mode = "simple" | "advanced";

const RESTART_OPTIONS = ["always", "on-failure", "on-abnormal", "on-abort", "on-watchdog", "on-success", "no"] as const;

interface EnvRow {
  id: number;
  key: string;
  value: string;
}

let nextRowId = 0;

function emptyRow(): EnvRow {
  nextRowId += 1;
  return { id: nextRowId, key: "", value: "" };
}

const RAW_TEMPLATE = `[Unit]
Description=

[Service]
Type=simple
User=
WorkingDirectory=
ExecStart=
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
`;

export interface CreateServiceDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Creates a systemd unit: simple mode fills in a template from a command, a directory, a user
 * and environment; advanced mode writes the unit file directly. Both go through `POST
 * /api/services`, which enables the unit once it is written.
 */
export function CreateServiceDialog({ open, onOpenChange }: CreateServiceDialogProps) {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<Mode>("simple");
  const [name, setName] = useState("");
  const [command, setCommand] = useState("");
  const [directory, setDirectory] = useState("/var/www");
  const [user, setUser] = useState("");
  const [restart, setRestart] = useState<(typeof RESTART_OPTIONS)[number]>("always");
  const [env, setEnv] = useState<EnvRow[]>([]);
  const [raw, setRaw] = useState(RAW_TEMPLATE);

  const create = useMutation({
    mutationFn: () =>
      mode === "simple"
        ? request("post", "/api/services", {
            body: {
              name,
              command,
              working_directory: directory,
              restart,
              ...(user.trim() !== "" ? { user: user.trim() } : {}),
              environment: Object.fromEntries(
                env.filter((row) => row.key.trim() !== "").map((row) => [row.key.trim(), row.value]),
              ),
            },
          })
        : request("post", "/api/services", {
            // working_directory and restart are unused by the backend in raw mode (it writes
            // raw_content verbatim) but the generated type still requires them in the body.
            body: { name, raw_content: raw, working_directory: "/var/www", restart: "always" },
          }),
    onSuccess: (result) => {
      toast.success(`Created ${result.service}`, { description: "The service was enabled to start at boot." });
      void queryClient.invalidateQueries({ queryKey: serviceKeys.all });
      // Not close(false): that guards on create.isPending, which this closure still reads as
      // true (nothing has re-rendered between the mutation resolving and this callback), so
      // the guard meant for Cancel/backdrop-dismiss during a submit would also swallow the
      // deliberate close after a successful one. onOpenChange is unguarded on purpose.
      reset();
      onOpenChange(false);
    },
  });

  const reset = (): void => {
    setMode("simple");
    setName("");
    setCommand("");
    setDirectory("/var/www");
    setUser("");
    setRestart("always");
    setEnv([]);
    setRaw(RAW_TEMPLATE);
    create.reset();
  };

  const close = (next: boolean): void => {
    if (!next && create.isPending) return;
    onOpenChange(next);
    if (!next) reset();
  };

  const valid = name.trim() !== "" && (mode === "advanced" ? raw.trim() !== "" : command.trim() !== "");

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      size="lg"
      title="New service"
      description="A systemd unit run under this machine's service user, restarted automatically and started at boot."
      footer={
        <>
          <Button disabled={create.isPending} onClick={() => close(false)}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!valid} loading={create.isPending} onClick={() => create.mutate()}>
            Create service
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="flex items-center justify-between gap-3">
          <Field label="Name" name="name" className="max-w-64 flex-1">
            <Input value={name} onValueChange={setName} mono placeholder="my-worker" autoComplete="off" spellCheck={false} />
          </Field>
          <SegmentedControl
            label="Mode"
            value={mode}
            onValueChange={setMode}
            options={[
              { value: "simple", label: "Simple" },
              { value: "advanced", label: "Advanced" },
            ]}
          />
        </div>

        {mode === "simple" ? (
          <>
            <Field label="Command" name="command" description="Run as an argv, without a shell.">
              <Input
                value={command}
                onValueChange={setCommand}
                mono
                placeholder="/usr/bin/node /var/www/worker/index.js"
                autoComplete="off"
                spellCheck={false}
              />
            </Field>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Working directory" name="directory">
                <Input value={directory} onValueChange={setDirectory} mono autoComplete="off" spellCheck={false} />
              </Field>
              <Field label="User" name="user" optional description="Defaults to the configured service user.">
                <Input value={user} onValueChange={setUser} mono autoComplete="off" spellCheck={false} />
              </Field>
            </div>
            <Field label="Restart policy" name="restart" nativeLabel={false}>
              <Select
                aria-label="Restart policy"
                value={restart}
                onValueChange={setRestart}
                mono
                options={RESTART_OPTIONS.map((value) => ({ value, label: value }))}
              />
            </Field>
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <span className="text-13 font-medium text-fg">Environment</span>
                <Button size="sm" icon={<Plus aria-hidden="true" />} onClick={() => setEnv((rows) => [...rows, emptyRow()])}>
                  Add variable
                </Button>
              </div>
              {env.length === 0 ? (
                <p className="text-13 text-fg-muted">No environment variables.</p>
              ) : (
                <div className="flex flex-col gap-2">
                  {env.map((row) => (
                    <div key={row.id} className="flex items-center gap-2">
                      <Input
                        aria-label="Variable name"
                        value={row.key}
                        onValueChange={(value) =>
                          setEnv((rows) => rows.map((r) => (r.id === row.id ? { ...r, key: value } : r)))
                        }
                        mono
                        placeholder="NAME"
                        className="w-40"
                        autoComplete="off"
                        spellCheck={false}
                      />
                      <Input
                        aria-label="Variable value"
                        value={row.value}
                        onValueChange={(value) =>
                          setEnv((rows) => rows.map((r) => (r.id === row.id ? { ...r, value } : r)))
                        }
                        mono
                        placeholder="value"
                        className="flex-1"
                        autoComplete="off"
                        spellCheck={false}
                      />
                      <IconButton
                        label={`Remove ${row.key || "variable"}`}
                        icon={<Trash2 />}
                        size="sm"
                        onClick={() => setEnv((rows) => rows.filter((r) => r.id !== row.id))}
                      />
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        ) : (
          <Field label="Unit file" name="raw" description="Written to /etc/systemd/system/{name}.service verbatim.">
            <Textarea mono rows={14} value={raw} onChange={(event) => setRaw(event.target.value)} spellCheck={false} />
          </Field>
        )}

        {create.isError ? <ErrorBlock live compact error={create.error} title="The service was not created" /> : null}
      </div>
    </Dialog>
  );
}
