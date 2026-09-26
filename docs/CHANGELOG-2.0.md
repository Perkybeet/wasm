# WASM 2.0 changelog

Upgrade notes, including what may need a setting after upgrading, are in
[UPGRADING-2.0.md](UPGRADING-2.0.md).

## 2.0.1

Fixes found running 2.0.0 on a production server with about twenty applications.

- **Backups could land in the working directory.** A blank `backup.directory` (which the 1.x
  settings form saved) was read as the current directory, so backups went wherever `wasm` ran
  from, and scheduled ones to `/`. Blank now means `/var/backups/wasm`, a relative path is
  refused by the CLI, the API and a whole-section write alike, and `wasm backup import DIR`
  moves misplaced backups into place (`wasm backup storage` says where it found them).
- **Unit failures now alert.** The monitor watches every unit WASM manages without any
  configuration, alerts on a failure or a crash loop and not on a unit someone stopped, and
  scans every 60 seconds by default. Failed cron and backup runs alert too.
- **One definition of WASM's units.** The top bar, the Services page, `wasm service list`,
  the monitor and the live log stream agree; application units without the old `wasm-`
  prefix were missed, and "show all units" failed on systemd's escaped names.
- **Live logs** stream monorepo and legacy-named units, and show journalctl's own error.
- **A unit left by an earlier application type is removed** when the application becomes
  static, and new units stop retrying after five failed starts in five minutes instead of
  restarting forever.
- **Git never prompts.** A private repository fails in about a second with how to fix it
  (the SSH URL with this server's key, or a credential helper) instead of hanging until a
  ten-minute timeout; no process WASM runs reads from the terminal.
- **PostgreSQL's read-only console** signs in on the port the server reports (`SHOW port`), so
  a cluster on a port other than 5432 works; psql's own message is always shown, and pg_hba is
  suggested only when psql names it. Connection strings and engine lists use the real port.
- **Rate limits:** signed-in requests count per credential with a budget of 1200 a minute
  (`web.rate_limit_authenticated_requests`); the console's page no longer counts; the console
  waits out a 429 and retries once instead of showing raw JSON.
- **Console errors show the tool's own output** (git, psql, nginx, certbot) under the message.
- The unit tally in the top bar says what its numbers mean; the Telegram form explains that a
  group's chat ID is negative.
- Ctrl+C on `wasm web start` with a browser connected shuts down at once and quietly.
- Saving a job is atomic (no more "UNIQUE constraint failed: jobs.id"), and an expected
  failure is logged in one line instead of a traceback.
- A repository name ending in "g", "i" or "t" is no longer truncated when read from its URL.

## 2.0.0

Changes since 1.6.4.

### Console

- The server-rendered panel (Jinja, htmx, hand-written JavaScript and CSS) is replaced by the
  WASM Console, a React single-page application. Its build is committed to the package, so
  installing never runs Node and the console loads nothing from any other origin.
- Every page: Overview, Applications, a three-step new-application wizard that inspects the
  repository first, application tabs (Overview, Deployments, Logs, Metrics, Environment,
  Domains, Diagnose, Settings), a page per deployment with its phases and build log,
  Databases with a SQL runner, Services with a unit editor, Cron, Domains and certificates,
  Backups, Activity, Server and Settings.
- Command palette (`Ctrl K` / `Cmd K`), keyboard shortcuts (`g a`, `g d`, `/`, `?` and
  others), light, dark and system themes.
- Live machine strip, charts, application states, job progress and notifications over one
  Server-Sent Events stream; journals and running jobs over WebSockets. Log viewer with ANSI
  colours, search, follow, wrap, copy and download.
- Accessibility to WCAG 2.2 AA, enforced by axe on every page in both themes in the
  end-to-end suite, which also fails on any Content Security Policy violation.
- The console has parity with the CLI: updating applications, monitor controls, full backup
  and certificate options, certificate issuance when creating a site.

### Deploy engine

- **Releases.** Every deploy of a new application builds in its own directory under
  `releases/`, and `current` points at the one that serves. The environment and persistent
  paths live in `shared/` and are linked into each release. Git sources go through a
  repository cache.
- **Health-gated activation.** `current` is swapped atomically, the unit restarted and the
  application probed; a release that does not answer is recorded as failed and the previous
  one is put back automatically, with the probes and the journal as the deployment's error.
- **Instant rollback** to any release on disk: `wasm releases rollback`, the console, or
  `POST /api/apps/{domain}/releases/{id}/activate`, behind the same health gate.
- **Dependency reuse.** `node_modules`, `.venv` or `venv` is copied from the active release
  (with reflinks where the filesystem supports them) when every lockfile and the runtime
  version (Node and its package manager, or Python) are unchanged.
- **Retention**: the newest five releases, the active one and the one a rollback would go
  back to are kept.
- **Migration** of in-place applications with `wasm app migrate`, never implicit: the live
  tree becomes the first release, the `.env` and the directories the application writes into
  move to `shared/` (SQLite databases with their WAL files), the unit is stopped while the
  tree moves, nothing is deleted or copied, file counts are verified, and a migration that
  does not come up is undone step by step, naming anything that could not be put back.
- **One operation per application at a time.** Update, deploy, rollback, migration, limits,
  restore and deletion take a per-application lock; a second one is refused with the name of
  the operation that holds it (`409 app_busy` over the API).
- **`wasm create` never wipes an existing tree.** A non-empty directory WASM did not create
  is refused without `--force`, and a failed deploy never deletes a directory it did not
  create. It had overwritten such directories, and could delete them, since 1.x.
- New applications use the release layout by default (`deploy.layout`); existing
  applications keep theirs. `wasm create --layout` and `--persist` choose per application.
- **Resource limits** per application: `MemoryMax`, `CPUQuota` and `TasksMax`, from
  `wasm app limits`, the console or `PATCH /api/apps/{domain}/limits`, kept across redeploys
  and verified by the health gate when applied with a restart.
- Monorepo and Docker Compose deploys and updates are recorded in the deployment history with
  their build log.
- Deployments are linked to the job that started them and the release they built.
- One lookup for where an application's `.env` lives, used by `wasm env`, the console, the
  deployers and the backups. `.env` files WASM writes read back exactly, and lines starting
  with `export` are understood.
- `wasm create` without `--type` detects the application type. It had deployed every such
  application as Node.js since the first release.
- Deleting an application is one implementation for the CLI and the console, which also
  brings a Docker Compose stack down and removes every unit the application has.
- A Docker Compose update rebuilds with the compose file the deploy chose, and keeps the
  project name a stack declares for itself, so its volumes are never orphaned.
- Store migrations run each schema step in one transaction.

### Domains and certificates

- Several domains per application: one primary, any number of aliases (served like the
  primary) and redirects (a `301` to the primary). `wasm domain add|list|remove`, the
  application's Domains tab, and `/api/apps/{domain}/domains`.
- One certificate covers every name, redirects included; adding a name extends it.
- `wasm create --www` records `www` as a redirect. Applications deployed by 1.x keep their
  `www` and have it recorded as an alias on their first domain change.
- DNS check before adding a name or ordering a certificate: the addresses a name resolves to
  against this machine's.
- Site configuration is tested by the web server before it is installed; the console can
  test an edit without saving it. Site templates are listed, and a site reports the names it
  serves.
- Certificates report their issuer.
- Creating or deleting a site goes through one implementation: creating one with TLS obtains
  the certificate before the site refers to it, and deleting one removes it from nginx and
  Apache and removes its certificate.

### Diagnosis and health

- `wasm diagnose DOMAIN`, the Diagnose tab and `GET /api/apps/{domain}/diagnose` explain why
  an application is down: unit state and exit status, port listening or mismatched, HTTP
  straight to the application and through nginx, journal, nginx error log, certificate, last
  deployment, OOM kills and disk space, with the most likely cause first.
- `GET /api/system/health` returns the same report as `wasm health`.
- Application states are resolved from systemd everywhere, so the console, `wasm list` and
  the API agree.

### API

- One error contract for every router: `{error, detail, hint, fields, output}`, with
  per-field messages on validation errors and the system tool's own output verbatim.
- Typed responses everywhere; the OpenAPI contract is served at `/api/openapi.json` to any
  authenticated caller and compiled into the console, so the console cannot call an endpoint
  that does not exist.
- `/events` sends JSON: `machine`, `metrics`, `app`, `job`, `state` and `notice`, and
  authenticates like the rest of the API.
- Jobs and their logs are persisted; a job interrupted by a console restart is marked failed
  instead of disappearing. Jobs record who queued them.
- New: deployments with filters and keyset pagination and their build logs, rollback points,
  releases and activation, migration plan and migration, domains and DNS checks, resource
  limits, repository inspection before deploying, application types, diagnosis, server
  health, the machine snapshot, audit log, webhook deliveries, notification channel tests,
  cron schedule preview, unit verification, systemd units WASM does not own (read-only) and
  unit states, site configuration tests and templates, structured SQL results (columns,
  rows, count, duration), session bootstrap (`GET /api/auth/session`), sudo mode
  (`POST /api/auth/elevate`), signing out other sessions.
- Applications report their layout, limits, webhook state, unit, service account and last
  deployment. Timestamps carry their UTC offset.
- Removed: `POST /api/jobs/deploy` (use `POST /api/apps`), the `/ws/system` WebSocket (use
  `/events` or `GET /api/system/machine`), and every server-rendered page and fragment.

### Security

- **Sudo mode.** Deleting applications, databases, users, services, sites, certificates,
  backups and domains; restoring backups; revealing or writing an `.env`; editing units and
  sites; SQL in write mode; writing configuration; migrating to releases; changing limits;
  issuing API tokens; creating services, cron jobs and backup schedules; enrolling,
  disabling and regenerating 2FA need a confirmation from the last 10 minutes in a browser
  session, whichever channel the session token arrives on.
- **Least-privilege read-only SQL.** PostgreSQL read mode signs in as a dedicated
  `wasm_ro_<database>` role, never a superuser session that switched role; MySQL and MariaDB
  use a dedicated account with `SELECT` only. `pg_read_file`, `COPY ... TO PROGRAM`,
  `LOAD_FILE()` and `INTO OUTFILE` are out of reach of the read-only runner.
- **No client commands from the SQL console or a restore.** psql receives the statement as
  a `-c` string and mysql reads in `--binary-mode`, so `\!`, `system` and `source` never run.
  A PostgreSQL dump with psql meta-commands is refused; a MySQL dump is read as data. In 1.x a
  restored MySQL dump could run shell commands as root.
- **Scopes.** A `deploy` token updates, rolls back and activates releases of existing
  applications; creating or inspecting one is admin-only, and a local-path source needs the
  master token or an elevated session. Credentials in stored clone URLs are masked.
- **Streams re-check their credential.** Log and job WebSockets and the `/events` stream
  close when the credential is revoked or expires, have a per-credential cap and a maximum
  lifetime, and `/ws/logs` streams only units WASM manages.
- **Secrets scrubbed from logs.** Deployment and job logs and errors have the application's
  secret values replaced with `***` before they are stored or streamed.
- TOTP codes are single use; a locked-out address is refused on every channel; a session
  renews into exactly one successor; request bodies are capped before authentication;
  process command lines are admin-only.
- **SSRF guard** on notifications: loopback, private, carrier-grade NAT, link-local and
  metadata destinations are refused, also when wrapped in an IPv6 address (IPv4-mapped,
  NAT64, 6to4), on every redirect hop, unless listed in `notifications.allow_private_hosts`.
  The test button no longer echoes the remote response.
- **Webhook signature failures** lock out that application's hook, not the forge's address.
- Static sites refuse hidden files (`.env`, `.git/`) on nginx and Apache, while
  `/.well-known/` stays reachable for certificate validation.
- The loopback-or-TLS rule for the console is also enforced where the socket is bound.
- Strict Content Security Policy with Trusted Types and no `unsafe-inline`; hashed assets
  cached immutable, everything else `no-store`.
- No `sudo` in any command WASM runs: programs that run as another account go through one
  `runuser` prefix.
- The audit log is readable at `GET /api/audit` and on the Activity page, with an admin
  credential.

### CLI

- New commands: `wasm releases list|rollback`, `wasm app migrate|limits`,
  `wasm domain add|list|remove`, `wasm diagnose`, `wasm cron
  list|create|delete|run|enable|disable|runs`, `wasm config get|set` (with the same
  validation as the console), `wasm token create|list|revoke`, `wasm sessions
  list|revoke|revoke-others`, `wasm 2fa enroll|confirm|disable|status|backup-codes`,
  `wasm notify test`.
- `--json` on every command that reports something, from one shared option; commands where
  JSON means nothing refuse the flag instead of ignoring it.
- A tool's own output (psql, nginx, systemd) is printed verbatim under a CLI error.
- `--open` links point at the console's pages.
- `wasm create --layout`, `--persist`; `wasm create --www` records a redirect.

### Monitor and notifications

- The monitor warns about certificates expiring within 14 days, at most once a day per
  certificate, through the notification channels.

### Backups

- Backups of applications on releases carry the active release and `shared/`, not the
  repository cache or older releases, and restore the `.env` link.
- Backups created from the API and the console take the CLI's options: `.env`,
  `node_modules`, build output, databases, Docker volumes, PostgreSQL schemas, the Redis
  capture method and tags.

### Reliability

- The store uses SQLite's WAL mode with a busy timeout, so the console and the CLI can write
  at the same time without `database is locked`.
- The store and configuration singletons initialise without races.
- One ownership helper hands files to the service account; failures are reported, not
  swallowed.
- Generating an SSH key reports failure when its permissions could not be tightened.
- Deleting a service and rolling back from the console go through the same guards as the
  CLI.

### Development and packaging

- The console's source is in `panel/` (React 19, TypeScript, Vite, TanStack Router and Query,
  Base UI, Tailwind CSS); CI fails when the committed build or the generated API types
  differ from the source.
- End-to-end tests with Playwright against the real backend, with axe and CSP checks.
- Removing the package stops the monitor and the console daemon; upgrading never does.
- A real-machine integration harness deploys applications in a systemd container.
- Store schema 8: application layout, persistent paths and limits; releases, domains and jobs
  tables; job actors; deployment job and release links.
