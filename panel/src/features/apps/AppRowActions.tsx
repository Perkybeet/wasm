import { useNavigate } from "@tanstack/react-router";
import { CircleArrowUp, MoreHorizontal, PanelTop, RotateCw, ScrollText } from "lucide-react";

import { appStatus } from "../../components/page/status";
import { IconButton } from "../../components/ui/IconButton";
import { Menu, MenuItem, MenuSeparator } from "../../components/ui/Menu";
import type { AppInfo } from "./data";
import { useAppActions } from "./useAppActions";

/** Whether the app has a unit to restart, start or stop. A static site is served by nginx alone. */
export function hasUnit(app: Pick<AppInfo, "status" | "app_type">): boolean {
  return appStatus(app.status).state !== "static" && app.app_type !== "static";
}

/** The menu at the end of an application's row: open it, read its logs, restart, update. */
export function AppRowActions({ app }: { app: AppInfo }) {
  const navigate = useNavigate();
  const { restart, update } = useAppActions(app.domain);
  const domain = app.domain;
  return (
    <Menu
      align="end"
      trigger={
<IconButton label={`Actions for ${domain}`} icon={<MoreHorizontal />} size="sm" tooltip={false} />
      }
    >
      <MenuItem icon={<PanelTop />} onClick={() => void navigate({ to: "/apps/$domain", params: { domain } })}>
        Open
      </MenuItem>
      <MenuItem icon={<ScrollText />} onClick={() => void navigate({ to: "/apps/$domain/logs", params: { domain } })}>
        Logs
      </MenuItem>
      <MenuSeparator />
      <MenuItem icon={<RotateCw />} disabled={!hasUnit(app) || restart.isPending} onClick={() => restart.mutate()}>
        Restart
      </MenuItem>
      <MenuItem icon={<CircleArrowUp />} disabled={update.isPending} onClick={() => update.mutate()}>
        Update
      </MenuItem>
    </Menu>
  );
}
