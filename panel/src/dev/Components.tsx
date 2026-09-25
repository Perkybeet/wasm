import {
  Boxes,
  ExternalLink,
  GitBranch,
  Info,
  Pencil,
  Play,
  Plus,
  Power,
  RefreshCw,
  Rocket,
  RotateCw,
  Search,
  Settings,
  Trash2,
  WrapText,
} from "lucide-react";
import { Ellipsis } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import {
  Badge,
  Button,
  Card,
  Chart,
  Checkbox,
  ConfirmDialog,
  CopyButton,
  DataTable,
  Dialog,
  DialogClose,
  Drawer,
  EmptyState,
  Field,
  IconButton,
  Input,
  Kbd,
  LogViewer,
  Logo,
  LogoMark,
  Menu,
  MenuGroup,
  MenuItem,
  MenuSeparator,
  Meter,
  Mono,
  Popover,
  Progress,
  Select,
  Skeleton,
  SkeletonText,
  Spinner,
  StatusPill,
  Switch,
  Tab,
  TabList,
  TabPanel,
  Tabs,
  Textarea,
  Tooltip,
  toast,
} from "../components/ui";
import type { Column, LogLine, Status } from "../components/ui";
import { Item, Row, Section, Stage } from "./gallery";
import type { SampleApp } from "./sample";
import { SAMPLE_APPS, SAMPLE_BUILD_LOG, ago, nextJournalLine, sampleMetrics } from "./sample";

const STATES: Status[] = ["running", "deploying", "failed", "stopped", "static", "unknown"];

function Buttons() {
  return (
    <Section
      id="button"
      title="Button"
      description="One primary action per view, in the accent. Labels are verbs that say what happens. Loading keeps the label and focus; danger is for the final step of a destructive flow."
    >
      <Stage>
        <Row>
          <Item label="Primary">
            <Button variant="primary" icon={<Rocket />}>
              Deploy
            </Button>
          </Item>
          <Item label="Secondary">
            <Button icon={<RotateCw />}>Restart</Button>
          </Item>
          <Item label="Ghost">
            <Button variant="ghost">Cancel</Button>
          </Item>
          <Item label="Danger">
            <Button variant="danger">Delete application</Button>
          </Item>
          <Item label="Loading">
            <Button variant="primary" loading>
              Deploying
            </Button>
          </Item>
          <Item label="Disabled">
            <Button disabled>Restart</Button>
          </Item>
        </Row>
      </Stage>
      <Stage>
        <Row>
          <Item label="Small">
            <Button size="sm" icon={<Plus />}>
              Add domain
            </Button>
          </Item>
          <Item label="Medium">
            <Button icon={<Plus />}>Add domain</Button>
          </Item>
          <Item label="Large">
            <Button size="lg" variant="primary" icon={<Plus />}>
              New application
            </Button>
          </Item>
          <Item label="Trailing icon">
            <Button variant="ghost" trailingIcon={<ExternalLink />}>
              Open site
            </Button>
          </Item>
        </Row>
      </Stage>
    </Section>
  );
}

function IconButtons() {
  const [wrap, setWrap] = useState(true);
  return (
    <Section
      id="icon-button"
      title="Icon button"
      description="A square button with a mandatory label, shown as a tooltip on hover and focus and read by screen readers. Toggles expose their pressed state."
    >
      <Stage>
        <Row>
          <Item label="Ghost">
            <IconButton label="Refresh" icon={<RefreshCw />} />
          </Item>
          <Item label="Secondary">
            <IconButton label="Settings" icon={<Settings />} variant="secondary" />
          </Item>
          <Item label="Pressed toggle">
            <IconButton label="Wrap lines" icon={<WrapText />} pressed={wrap} onClick={() => setWrap((v) => !v)} />
          </Item>
          <Item label="With shortcut">
            <IconButton label="Search" icon={<Search />} shortcut={["/"]} />
          </Item>
          <Item label="Small">
            <IconButton label="Edit" icon={<Pencil />} size="sm" />
          </Item>
          <Item label="Disabled">
            <IconButton label="Start" icon={<Play />} disabled />
          </Item>
        </Row>
      </Stage>
    </Section>
  );
}

const RUNTIMES = [
  { value: "node", label: "Node.js 22", hint: "npm ci && npm run build" },
  { value: "next", label: "Next.js", hint: "next build && next start" },
  { value: "python", label: "Python 3.12", hint: "uvicorn app:main" },
  { value: "static", label: "Static site", hint: "served by nginx" },
];

function Forms() {
  return (
    <Section
      id="field"
      title="Fields"
      description="Every control has a visible label. Help stays under the control; a validation error replaces nothing, it is added, in the fail colour with an icon, and announced with the field."
    >
      <Stage>
        <div className="grid gap-x-8 gap-y-6 md:grid-cols-2">
          <Field label="Domain" description="The address the app will answer on. DNS must point here.">
            <Input mono placeholder="example.com" defaultValue="shop.arenna.dev" />
          </Field>
          <Field label="Repository">
            <Input mono prefix="https://" placeholder="github.com/you/app.git" />
          </Field>
          <Field label="Port" error="Port 3004 is already used by wasm-shop.arenna.dev.service.">
            <Input mono defaultValue="3004" inputMode="numeric" />
          </Field>
          <Field label="Branch" optional>
            <Input mono placeholder="main" icon={<GitBranch />} />
          </Field>
          <Field label="Runtime" nativeLabel={false} description="Detected from package.json. Change it if the guess is wrong.">
            <Select options={RUNTIMES} defaultValue="next" />
          </Field>
          <Field label="Build command" disabled>
            <Input mono defaultValue="npm run build" suffix={<CopyButton value="npm run build" label="Copy build command" />} />
          </Field>
          <Field label="Environment" description="One KEY=value per line. Values are stored 0600 and never logged." className="md:col-span-2">
            <Textarea mono rows={4} defaultValue={"DATABASE_URL=postgres://shop@localhost/shop\nNODE_ENV=production\nSTRIPE_SECRET_KEY=sk_live_..."} />
          </Field>
        </div>
      </Stage>
      <Stage>
        <div className="grid gap-x-8 gap-y-5 md:grid-cols-2">
          <div className="flex flex-col gap-4">
            <Checkbox
              label="Include www"
              description={
                <>
                  Serve <Mono>www.shop.arenna.dev</Mono> and redirect it to the apex.
                </>
              }
              defaultChecked
            />
            <Checkbox label="Back up databases" />
            <Checkbox label="All volumes" indeterminate />
            <Checkbox label="Encrypt with a passphrase" disabled />
          </div>
          <div className="flex max-w-sm flex-col gap-4">
            <Switch label="Deploy on push" description="Build and release when main receives a commit." defaultChecked />
            <Switch label="Maintenance page" />
            <Switch label="Two-factor authentication" defaultChecked disabled />
          </div>
        </div>
      </Stage>
    </Section>
  );
}

function Overlays() {
  const [renameOpen, setRenameOpen] = useState(false);
  return (
    <Section
      id="overlay"
      title="Dialogs and drawers"
      description="Dialogs trap focus and return it to their trigger. Irreversible actions ask for the name of what will be destroyed, and a failure is shown as the system reported it, with the fix above."
    >
      <Stage>
        <Row>
          <Item label="Dialog">
            <Dialog
              title="Rename application"
              description="The service, site and certificate follow the new name. Traffic continues on the old one until DNS moves."
              open={renameOpen}
              onOpenChange={setRenameOpen}
              trigger={<Button data-testid="open-dialog">Rename</Button>}
              footer={
                <>
                  <DialogClose render={<Button>Cancel</Button>} />
                  <Button variant="primary" onClick={() => setRenameOpen(false)}>
                    Rename
                  </Button>
                </>
              }
            >
              <Field label="New domain">
                <Input mono defaultValue="shop.arenna.dev" />
              </Field>
            </Dialog>
          </Item>
          <Item label="Type to confirm">
            <ConfirmDialog
              title="Delete shop.arenna.dev"
              description="Stops the service, removes the nginx site and the release directories. Backups and the database are kept."
              confirmText="shop.arenna.dev"
              actionLabel="Delete application"
              onConfirm={() => new Promise((resolve) => setTimeout(resolve, 900))}
              trigger={
                <Button variant="danger" icon={<Trash2 />} data-testid="open-confirm">
                  Delete
                </Button>
              }
            />
          </Item>
          <Item label="Failure shown verbatim">
            <ConfirmDialog
              title="Stop worker.arenna.dev"
              description="The worker stops taking jobs. Jobs in progress are cancelled."
              confirmText="worker.arenna.dev"
              actionLabel="Stop service"
              onConfirm={() =>
                new Promise((_, reject) =>
                  setTimeout(() => {
                    reject(
                      Object.assign(new Error("systemctl stop failed"), {
                        hint: "The unit did not stop within 90 seconds. Check what it is waiting on, then stop it again.",
                        detail:
                          "Job for wasm-worker.arenna.dev.service canceled.\nwasm-worker.arenna.dev.service: State 'stop-sigterm' timed out. Killing.",
                      }),
                    );
                  }, 700),
                )
              }
              trigger={<Button icon={<Power />}>Stop</Button>}
            />
          </Item>
          <Item label="Drawer">
            <Drawer
              title={
                <>
                  Deployment <Mono className="text-[0.85em]">a1b2c3d</Mono>
                </>
              }
              description="Fix checkout rounding for EUR totals"
              trigger={<Button data-testid="open-drawer">View deployment</Button>}
              footer={<Button>Redeploy</Button>}
            >
              <dl className="grid grid-cols-[8rem_1fr] gap-x-4 gap-y-2 text-13">
                <dt className="text-fg-muted">Status</dt>
                <dd>
                  <StatusPill state="failed" label="Rolled back" size="sm" />
                </dd>
                <dt className="text-fg-muted">Release</dt>
                <dd>
                  <Mono>20260925-143012-a1b2c3d</Mono>
                </dd>
                <dt className="text-fg-muted">Duration</dt>
                <dd>
                  <Mono>1m 12s</Mono>
                </dd>
              </dl>
              <LogViewer lines={SAMPLE_BUILD_LOG} height={320} className="mt-5" label="Build log" />
            </Drawer>
          </Item>
        </Row>
      </Stage>
    </Section>
  );
}

function Navigation() {
  return (
    <Section
      id="tabs"
      title="Tabs, menus and popovers"
      description="Tabs switch between views of one subject and follow arrow keys. Menus hold actions on one subject; destructive items are set apart and in the fail colour."
    >
      <Stage plain>
        <Tabs defaultValue="deployments">
          <TabList aria-label="Application sections">
            <Tab value="overview">Overview</Tab>
            <Tab value="deployments" count={12}>
              Deployments
            </Tab>
            <Tab value="logs">Logs</Tab>
            <Tab value="metrics">Metrics</Tab>
            <Tab value="environment">Environment</Tab>
            <Tab value="domains">Domains</Tab>
            <Tab value="diagnose">Diagnose</Tab>
            <Tab value="settings" disabled>
              Settings
            </Tab>
          </TabList>
          {["overview", "deployments", "logs", "metrics", "environment", "domains", "diagnose", "settings"].map((value) => (
            <TabPanel key={value} value={value}>
              <p className="text-14 text-fg-muted">
                The <span className="font-medium text-fg">{value}</span> view of shop.arenna.dev.
              </p>
            </TabPanel>
          ))}
        </Tabs>
      </Stage>
      <Stage>
        <Row>
          <Item label="Menu">
            <Menu
              align="start"
              trigger={<Button trailingIcon={<Ellipsis />} data-testid="open-menu">Actions</Button>}
            >
              <MenuGroup label="shop.arenna.dev">
                <MenuItem icon={<RotateCw />} shortcut={["R"]}>
                  Restart
                </MenuItem>
                <MenuItem icon={<Rocket />} shortcut={["D"]}>
                  Redeploy
                </MenuItem>
                <MenuItem icon={<ExternalLink />}>Open site</MenuItem>
              </MenuGroup>
              <MenuSeparator />
              <MenuItem icon={<Power />}>Stop</MenuItem>
              <MenuItem icon={<Trash2 />} destructive>
                Delete application
              </MenuItem>
            </Menu>
          </Item>
          <Item label="Tooltip">
            <Tooltip content="Last deployed 12 min ago">
              <Button variant="ghost">Hover or focus</Button>
            </Tooltip>
          </Item>
          <Item label="Popover">
            <Popover
              title="What is a release?"
              description="Each deploy builds into its own directory. Switching releases is a symlink swap and a restart, so a rollback takes seconds."
              trigger={
                <Button variant="ghost" icon={<Info />} data-testid="open-popover">
                  Releases
                </Button>
              }
            />
          </Item>
        </Row>
      </Stage>
    </Section>
  );
}

function Feedback() {
  const [state, setState] = useState<Status>("running");
  const [progress, setProgress] = useState(64);
  return (
    <Section
      id="status"
      title="Status and feedback"
      description="Status pills sit on a soft ground in headers and stand bare in dense rows. A change of state pulses once. Toasts report outcomes; failures stay until dismissed and carry the system's output."
    >
      <Stage>
        <div className="flex flex-col gap-6">
          <Row>
            {STATES.map((s) => (
              <StatusPill key={s} state={s} />
            ))}
          </Row>
          <Row>
            {STATES.map((s) => (
              <StatusPill key={s} state={s} appearance="inline" />
            ))}
          </Row>
          <Row>
            {STATES.map((s) => (
              <StatusPill key={s} state={s} size="sm" />
            ))}
          </Row>
          <Row>
            <Item label="Custom label">
              <StatusPill state="deploying" label="Building" />
            </Item>
            <Item label="State change pulses once">
              <div className="flex items-center gap-3">
                <StatusPill state={state} />
                <Button
                  size="sm"
                  onClick={() => setState((s) => STATES[(STATES.indexOf(s) + 1) % STATES.length] ?? "running")}
                >
                  Next state
                </Button>
              </div>
            </Item>
          </Row>
        </div>
      </Stage>
      <Stage>
        <Row>
          <Item label="Success">
            <Button
              data-testid="toast-success"
              onClick={() => toast.success("Deployed shop.arenna.dev", { description: "a1b2c3d is live. Build took 42 s." })}
            >
              Success toast
            </Button>
          </Item>
          <Item label="Error, stays">
            <Button
              data-testid="toast-error"
              onClick={() =>
                toast.error("Deploy failed", {
                  description: "The health check did not pass. The previous release is still serving.",
                  detail: "GET http://127.0.0.1:3004/ returned 502 after 30s",
                  action: { label: "View log", onClick: () => undefined },
                })
              }
            >
              Error toast
            </Button>
          </Item>
          <Item label="Warning">
            <Button onClick={() => toast.warning("Certificate expires in 6 days", { description: "Renewal failed twice. Check that port 80 is reachable." })}>
              Warning toast
            </Button>
          </Item>
          <Item label="Info">
            <Button onClick={() => toast.info("Backup started", { description: "shop.arenna.dev, database included." })}>
              Info toast
            </Button>
          </Item>
        </Row>
      </Stage>
      <Stage>
        <div className="grid gap-8 md:grid-cols-3">
          <div className="flex flex-col gap-5">
            <Progress label="Uploading backup" value={progress} />
            <Progress label="Waiting for the build slot" value={null} />
            <Button size="sm" className="self-start" onClick={() => setProgress((p) => (p >= 100 ? 0 : Math.min(100, p + 12)))}>
              Advance
            </Button>
          </div>
          <div className="flex flex-col gap-4">
            <Meter label="CPU" value={23} />
            <Meter label="Memory" value={6.2} max={8} valueText="6.2 of 8 GB" />
            <Meter label="Disk" value={71} max={75} valueText="71 of 75 GB" />
          </div>
          <div className="flex flex-col gap-4">
            <Row>
              <Item label="Spinner">
                <Spinner label="Loading applications" />
              </Item>
              <Item label="Skeleton">
                <div className="flex w-48 flex-col gap-2">
                  <Skeleton className="h-4 w-28" />
                  <SkeletonText lines={2} />
                </div>
              </Item>
            </Row>
          </div>
        </div>
      </Stage>
    </Section>
  );
}

function Attributes() {
  return (
    <Section
      id="badge"
      title="Badges, keys and values"
      description="Badges state attributes and stay neutral unless the attribute is a state. System values are always mono and copyable where an operator would paste them into a terminal."
    >
      <Stage>
        <div className="flex flex-col gap-6">
          <Row>
            <Item label="Neutral">
              <Badge>Next.js</Badge>
            </Item>
            <Item label="Mono">
              <Badge mono>node 22.23.3</Badge>
            </Item>
            <Item label="Accent">
              <Badge tone="accent">Primary domain</Badge>
            </Item>
            <Item label="State">
              <div className="flex gap-1.5">
                <Badge tone="ok">Valid</Badge>
                <Badge tone="warn">Expires in 6 d</Badge>
                <Badge tone="fail">Expired</Badge>
              </div>
            </Item>
          </Row>
          <Row>
            <Item label="Shortcuts">
              <div className="flex items-center gap-4 text-13 text-fg-muted">
                <span className="flex items-center gap-1.5">
                  <Kbd>Ctrl</Kbd>
                  <Kbd>K</Kbd> Command palette
                </span>
                <span className="flex items-center gap-1.5">
                  <Kbd>g</Kbd>
                  <Kbd>a</Kbd> Applications
                </span>
                <span className="flex items-center gap-1.5">
                  <Kbd>?</Kbd> Shortcuts
                </span>
              </div>
            </Item>
          </Row>
          <Row>
            <Item label="Values">
              <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-14">
                <Mono>/var/www/apps/shop/current</Mono>
                <Mono>:3004</Mono>
                <Mono tone="muted">a1b2c3d</Mono>
                <Mono>wasm-shop.arenna.dev.service</Mono>
              </div>
            </Item>
            <Item label="Copy">
              <div className="flex items-center gap-1 rounded-control border border-border bg-surface py-0.5 pr-0.5 pl-2.5">
                <Mono>ssh root@arenna.dev</Mono>
                <CopyButton value="ssh root@arenna.dev" label="Copy SSH command" />
              </div>
            </Item>
          </Row>
        </div>
      </Stage>
    </Section>
  );
}

function Cards() {
  return (
    <Section
      id="card"
      title="Card and empty state"
      description="A card groups one subject; it is not decoration. Empty states say what the place is for, offer the action, and give the terminal command for the same thing."
    >
      <div className="grid gap-6 lg:grid-cols-2">
        <Card
          title="Resources"
          description="Against the limits set for this app"
          actions={<Button size="sm">Edit limits</Button>}
        >
          <div className="flex flex-col gap-4">
            <Meter label="CPU quota" value={38} valueText="38% of 200%" />
            <Meter label="Memory" value={812} max={1024} valueText="812 of 1024 MB" />
            <Meter label="Tasks" value={61} max={64} valueText="61 of 64" />
          </div>
        </Card>
        <Card
          title="Deploy on push"
          description="Webhook for github.com/arenna/shop"
          footer={
            <>
              <Button variant="ghost">Disable</Button>
              <Button variant="primary">Save</Button>
            </>
          }
        >
          <Field label="Payload URL">
            <Input
              mono
              readOnly
              value="https://arenna.dev/hooks/github/shop"
              suffix={<CopyButton value="https://arenna.dev/hooks/github/shop" label="Copy payload URL" />}
            />
          </Field>
        </Card>
      </div>
      <EmptyState
        icon={<Boxes />}
        title="No applications yet"
        description="Deploy a repository and WASM builds it, runs it as a systemd unit and puts nginx and a certificate in front of it."
        action={
          <Button variant="primary" icon={<Plus />}>
            New application
          </Button>
        }
        command="wasm create -d example.com -s git@github.com:you/app.git"
      />
    </Section>
  );
}

const COLUMNS: Column<SampleApp>[] = [
  {
    id: "domain",
    header: "Application",
    cell: (row) => row.domain,
    sortValue: (row) => row.domain,
  },
  {
    id: "status",
    header: "Status",
    cell: (row) => <StatusPill state={row.status} appearance="inline" />,
    sortValue: (row) => row.status,
  },
  { id: "type", header: "Type", cell: (row) => <Badge>{row.type}</Badge>, hideBelow: "md" },
  {
    id: "port",
    header: "Port",
    cell: (row) => row.port ?? <span className="text-fg-faint">-</span>,
    sortValue: (row) => row.port,
    align: "end",
    mono: true,
    hideBelow: "sm",
  },
  { id: "commit", header: "Commit", cell: (row) => row.commit, mono: true, hideBelow: "lg" },
  {
    id: "deployed",
    header: "Deployed",
    cell: (row) => <span className="text-fg-muted">{ago(row.deployedMinutesAgo)}</span>,
    sortValue: (row) => row.deployedMinutesAgo,
    align: "end",
    hideBelow: "sm",
  },
];

function Tables() {
  return (
    <Section
      id="table"
      title="Data table"
      description="Dense where the operator works. Sortable columns announce their order; rows open with a click or Enter and arrow keys move between them. Numbers align right in tabular mono."
    >
      <DataTable
        caption="Applications"
        columns={COLUMNS}
        rows={SAMPLE_APPS}
        getRowId={(row) => row.domain}
        defaultSort={{ column: "deployed", direction: "ascending" }}
        onRowActivate={(row) => toast.info(`Open ${row.domain}`)}
        rowActions={(row) => (
          <Menu align="end" trigger={<IconButton label={`Actions for ${row.domain}`} icon={<Ellipsis />} size="sm" tooltip={false} />}>
            <MenuItem icon={<RotateCw />}>Restart</MenuItem>
            <MenuItem icon={<Rocket />}>Redeploy</MenuItem>
            <MenuSeparator />
            <MenuItem icon={<Trash2 />} destructive>
              Delete
            </MenuItem>
          </Menu>
        )}
      />
      <div className="grid gap-6 lg:grid-cols-2">
        <DataTable caption="Applications, loading" columns={COLUMNS.slice(0, 3)} rows={[]} getRowId={(row) => row.domain} loading />
        <DataTable
          caption="Databases"
          columns={[
            { id: "name", header: "Database", cell: (row: { name: string }) => row.name },
            { id: "engine", header: "Engine", cell: () => "PostgreSQL 16" },
          ]}
          rows={[]}
          getRowId={(row) => row.name}
          density="compact"
          empty={<p className="py-6 text-center text-13 text-fg-muted">No databases yet. Create one to attach it to an app.</p>}
        />
      </div>
    </Section>
  );
}

function Charts() {
  const metrics = sampleMetrics();
  return (
    <Section
      id="chart"
      title="Chart"
      description="Canvas for speed, but never only canvas: each chart is an image with a written summary and turns into a table on request. Series differ by line style as well as colour."
    >
      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <Chart
            title="CPU"
            description="Last 30 minutes"
            timestamps={metrics.timestamps}
            series={[{ label: "shop.arenna.dev", values: metrics.cpu }]}
            formatValue={(v) => `${v.toFixed(0)}%`}
            yRange={[0, 100]}
          />
        </Card>
        <Card>
          <Chart
            title="Memory"
            description="Last 30 minutes"
            timestamps={metrics.timestamps}
            series={[
              { label: "Used", values: metrics.memory },
              { label: "Limit", values: metrics.timestamps.map(() => 640) },
            ]}
            formatValue={(v) => `${v.toFixed(0)} MB`}
          />
        </Card>
      </div>
    </Section>
  );
}

function Logs() {
  const [streaming, setStreaming] = useState(false);
  const [lines, setLines] = useState<LogLine[]>(() => Array.from({ length: 400 }, (_, i) => nextJournalLine(i + 1)));
  const nextId = useRef(401);

  useEffect(() => {
    if (!streaming) return;
    const timer = setInterval(() => {
      setLines((current) => [...current, nextJournalLine(nextId.current++)]);
    }, 350);
    return () => {
      clearInterval(timer);
    };
  }, [streaming]);

  return (
    <Section
      id="logs"
      title="Log viewer"
      description="Output stays text: select it, search it, copy it, download it. Program colours map onto state tokens. Following pauses as soon as you scroll up and offers the way back."
    >
      <LogViewer lines={SAMPLE_BUILD_LOG} height={420} label="Build log for shop.arenna.dev" filename="shop-a1b2c3d.log" />
      <div className="flex flex-col gap-3">
        <Row>
          <Switch label="Stream journal lines" checked={streaming} onCheckedChange={setStreaming} />
        </Row>
        <LogViewer lines={lines} height={300} label="Journal for shop.arenna.dev" filename="shop-journal.log" />
      </div>
      <LogViewer lines={[]} height={140} label="Empty log" />
    </Section>
  );
}

function Brand() {
  return (
    <Section
      id="logo"
      title="Logo"
      description="A gear for the server and an arrow rising from its hub for the deploy. The gradient from the original artwork lives in the mark and nowhere else."
    >
      <Stage>
        <Row>
          {[16, 24, 32, 48, 72].map((size) => (
            <Item key={size} label={`${String(size)} px`}>
              <LogoMark size={size} title="WASM" />
            </Item>
          ))}
        </Row>
      </Stage>
      <div className="grid gap-4 sm:grid-cols-2">
        {(["light", "dark"] as const).map((theme) => (
          <div key={theme} data-theme={theme} className="flex flex-col gap-5 rounded-card border border-border bg-bg p-6 text-fg">
            <Logo size="lg" />
            <Logo size="md" product="Console" />
            <Logo size="sm" />
          </div>
        ))}
      </div>
    </Section>
  );
}

export function Components() {
  return (
    <>
      <Buttons />
      <IconButtons />
      <Forms />
      <Overlays />
      <Navigation />
      <Feedback />
      <Attributes />
      <Cards />
      <Tables />
      <Charts />
      <Logs />
      <Brand />
    </>
  );
}
