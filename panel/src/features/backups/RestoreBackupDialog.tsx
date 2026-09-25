import { AlertDialog } from "@base-ui/react/alert-dialog";
import { useRef, useState } from "react";
import type { SyntheticEvent } from "react";

import type { Backup } from "../../api/queries/backups";
import { ErrorBlock } from "../../components/page/QueryState";
import { Button } from "../../components/ui/Button";
import { Checkbox } from "../../components/ui/Checkbox";
import { BACKDROP, DialogFrame, MODAL_POPUP } from "../../components/ui/Dialog";
import { Input } from "../../components/ui/Input";
import { cx } from "../../lib/cx";
import { useBackupActions } from "./useBackupActions";

export interface RestoreBackupDialogProps {
  backup: Backup;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Restores an application from one of its backups. Typing the target domain is the confirmation
 * (D5-style irreversible-action pattern, `ConfirmDialog`'s own rule): it also doubles as where
 * to restore into, since a backup may be replayed onto a different domain than it came from.
 */
export function RestoreBackupDialog({ backup, open, onOpenChange }: RestoreBackupDialogProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [targetDomain, setTargetDomain] = useState(backup.domain);
  const [typed, setTyped] = useState("");
  const [restoreEnv, setRestoreEnv] = useState(true);
  const [verifyFirst, setVerifyFirst] = useState(true);
  const { restore } = useBackupActions();

  const matches = typed === targetDomain && targetDomain.trim() !== "";

  const close = (next: boolean): void => {
    if (!next && restore.isPending) return;
    onOpenChange(next);
    if (!next) {
      setTargetDomain(backup.domain);
      setTyped("");
      setRestoreEnv(true);
      setVerifyFirst(true);
      restore.reset();
    }
  };

  const submit = (event: SyntheticEvent<HTMLFormElement>): void => {
    event.preventDefault();
    if (!matches) return;
    restore.mutate(
      {
        backupId: backup.backup_id,
        targetDomain: targetDomain === backup.domain ? undefined : targetDomain,
        restoreEnv,
        verify: verifyFirst,
      },
      { onSuccess: () => close(false) },
    );
  };

  return (
    <AlertDialog.Root open={open} onOpenChange={(next: boolean) => close(next)}>
      <AlertDialog.Portal>
        <AlertDialog.Backdrop className={BACKDROP} />
        <AlertDialog.Viewport className="fixed inset-0 z-50 flex items-center justify-center p-4 sm:items-start sm:pt-[12vh]">
          <AlertDialog.Popup className={cx(MODAL_POPUP, "sm:max-w-[480px]")}>
            <form onSubmit={submit} className="contents">
              <DialogFrame
                title={`Restore ${backup.backup_id}`}
                description="Replaces the application's files (and its databases, if this backup includes them) with what this backup holds. Anything written since is lost."
                Title={AlertDialog.Title}
                Description={AlertDialog.Description}
                footer={
                  <>
                    <AlertDialog.Close render={<Button disabled={restore.isPending}>Cancel</Button>} />
                    <Button type="submit" variant="danger" disabled={!matches} loading={restore.isPending}>
                      Restore
                    </Button>
                  </>
                }
              >
                <div className="flex flex-col gap-4">
                  <div className="flex flex-col gap-1.5">
                    <label htmlFor="restore-target-domain" className="text-13 font-medium text-fg">
                      Restore into
                    </label>
                    <Input
                      id="restore-target-domain"
                      mono
                      value={targetDomain}
                      onValueChange={setTargetDomain}
                      autoComplete="off"
                      autoCapitalize="off"
                      spellCheck={false}
                      disabled={restore.isPending}
                    />
                  </div>
                  <Checkbox
                    checked={restoreEnv}
                    onCheckedChange={setRestoreEnv}
                    label="Restore .env files"
                    description="From the backup archive, replacing what is there now."
                  />
                  <Checkbox
                    checked={verifyFirst}
                    onCheckedChange={setVerifyFirst}
                    label="Verify the checksum first"
                    description="Refuses to restore a corrupted or tampered archive."
                  />
                  <div className="flex flex-col gap-1.5">
                    <label htmlFor="restore-confirm" className="text-13 text-fg-muted">
                      Type{" "}
                      <span translate="no" className="mono rounded-[4px] bg-bg-sunken px-1 py-0.5 text-fg select-all">
                        {targetDomain || "the domain"}
                      </span>{" "}
                      to confirm
                    </label>
                    <Input
                      id="restore-confirm"
                      ref={inputRef}
                      mono
                      value={typed}
                      onValueChange={setTyped}
                      autoComplete="off"
                      autoCapitalize="off"
                      spellCheck={false}
                      disabled={restore.isPending}
                    />
                  </div>
                  {restore.isError ? <ErrorBlock live compact error={restore.error} title="The restore did not start" /> : null}
                </div>
              </DialogFrame>
            </form>
          </AlertDialog.Popup>
        </AlertDialog.Viewport>
      </AlertDialog.Portal>
    </AlertDialog.Root>
  );
}
