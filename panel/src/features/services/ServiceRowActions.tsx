import { useNavigate } from "@tanstack/react-router";
import { CirclePause, CirclePlay, MoreHorizontal, PanelTop, RotateCw, ToggleLeft, ToggleRight } from "lucide-react";

import { IconButton } from "../../components/ui/IconButton";
import { Menu, MenuItem, MenuSeparator } from "../../components/ui/Menu";
import type { ServiceInfo } from "./data";
import { useServiceActions } from "./useServiceActions";

/**
 * The menu at the end of a service's row: open it, or act on the unit directly. A unit WASM
 * did not create has no menu at all - read-only is enforced here, at the one place every row
 * of every services table gets its actions from, not left to each caller to remember.
 */
export function ServiceRowActions({ service }: { service: ServiceInfo }) {
  const navigate = useNavigate();
  const name = service.name;
  const { start, stop, restart, enable, disable } = useServiceActions(name);

  if (!service.managed) return null;

  return (
    <Menu
      align="end"
      trigger={<IconButton label={`Actions for ${name}`} icon={<MoreHorizontal />} size="sm" tooltip={false} />}
    >
      <MenuItem icon={<PanelTop />} onClick={() => void navigate({ to: "/services/$name", params: { name } })}>
        Open
      </MenuItem>
      <MenuSeparator />
      {service.active ? (
        <MenuItem icon={<CirclePause />} disabled={stop.isPending} onClick={() => stop.mutate()}>
          Stop
        </MenuItem>
      ) : (
        <MenuItem icon={<CirclePlay />} disabled={start.isPending} onClick={() => start.mutate()}>
          Start
        </MenuItem>
      )}
      <MenuItem icon={<RotateCw />} disabled={restart.isPending} onClick={() => restart.mutate()}>
        Restart
      </MenuItem>
      <MenuSeparator />
      {service.enabled ? (
        <MenuItem icon={<ToggleLeft />} disabled={disable.isPending} onClick={() => disable.mutate()}>
          Disable at boot
        </MenuItem>
      ) : (
        <MenuItem icon={<ToggleRight />} disabled={enable.isPending} onClick={() => enable.mutate()}>
          Enable at boot
        </MenuItem>
      )}
    </Menu>
  );
}
