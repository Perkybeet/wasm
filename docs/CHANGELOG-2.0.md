# WASM 2.0 changelog

Changes since 1.6.4. Upgrade notes, including what may need a setting after upgrading, are in
[UPGRADING-2.0.md](UPGRADING-2.0.md).

## Console

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

## Deploy engine

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
  (with reflinks where the filesystem supports them) when every lockfile is unchanged.
- **Retention**: the newest five releases and the active one are kept.
- **Migration** of in-place applications with `wasm app migrate`, never implicit: the live
  tree becomes the first release, the `.env` and the directories the application writes into
  move to `shared/`, nothing is deleted or copied, file counts are verified, and a migration
  that does not come up is undone exactly.
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

## Domains and certificates

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

## Diagnosis and health

- `wasm diagnose DOMAIN`, the Diagnose tab and `GET /api/apps/{domain}/diagnose` explain why
  an application is down: unit state and exit status, port listening or mismatched, HTTP
  straight to the application and through nginx, journal, nginx error log, certificate, last
  deployment, OOM kills and disk space, with the most likely cause first.
- `GET /api/system/health` returns the same report as `wasm health`.
- Application states are resolved from systemd everywhere, so the console, `wasm list` and
  the API agree.

## API

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

## Security

- **Sudo mode.** Deleting applications, databases, users, services, sites, certificates,
  backups and domains; restoring backups; revealing or writing an `.env`; editing units and
  sites; SQL in write mode; writing configuration; migrating to releases; changing limits;
  issuing API tokens; disabling 2FA and regenerating backup codes need a confirmation from
  the last 10 minutes in a browser session.
- **Least-privilege read-only SQL.** PostgreSQL statements run under a dedicated role without
  superuser rights inside a read-only transaction; MySQL and MariaDB under a dedicated
  account with `SELECT` only. `pg_read_file`, `LOAD_FILE()` and `INTO OUTFILE` are out of
  reach of the read-only runner.
- **SSRF guard** on notifications: loopback, private, carrier-grade NAT, link-local and
  metadata destinations are refused, on every redirect hop, unless listed in
  `notifications.allow_private_hosts`. The test button no longer echoes the remote response.
- **Webhook signature failures** count toward the login lockout.
- The loopback-or-TLS rule for the console is also enforced where the socket is bound.
- Strict Content Security Policy with Trusted Types and no `unsafe-inline`; hashed assets
  cached immutable, everything else `no-store`.
- No `sudo` in any command WASM runs: programs that run as another account go through one
  `runuser` prefix.
- The audit log is readable at `GET /api/audit` and on the Activity page, with an admin
  credential.

## CLI

- New commands: `wasm releases list|rollback`, `wasm app migrate|limits`,
  `wasm domain add|list|remove`, `wasm diagnose`, `wasm cron
  list|create|delete|run|enable|disable|runs`, `wasm config get|set` (with the same
  validation as the console), `wasm token create|list|revoke`, `wasm sessions
  list|revoke|revoke-others`, `wasm 2fa enroll|confirm|disable|status|backup-codes`,
  `wasm notify test`.
- `--json` on `wasm list`, `wasm status` and `wasm logs`, from one shared option.
- `--open` links point at the console's pages.
- `wasm create --layout`, `--persist`; `wasm create --www` records a redirect.

## Monitor and notifications

- The monitor warns about certificates expiring within 14 days, at most once a day per
  certificate, through the notification channels.

## Backups

- Backups of applications on releases carry the active release and `shared/`, not the
  repository cache or older releases, and restore the `.env` link.
- Backups created from the API and the console take the CLI's options: `.env`,
  `node_modules`, build output, databases, Docker volumes, PostgreSQL schemas, the Redis
  capture method and tags.

## Reliability

- The store uses SQLite's WAL mode with a busy timeout, so the console and the CLI can write
  at the same time without `database is locked`.
- The store and configuration singletons initialise without races.
- One ownership helper hands files to the service account; failures are reported, not
  swallowed.
- Generating an SSH key reports failure when its permissions could not be tightened.
- Deleting a service and rolling back from the console go through the same guards as the
  CLI.

## Development and packaging

- The console's source is in `panel/` (React 19, TypeScript, Vite, TanStack Router and Query,
  Base UI, Tailwind CSS); CI fails when the committed build or the generated API types
  differ from the source.
- End-to-end tests with Playwright against the real backend, with axe and CSP checks.
- A real-machine integration harness deploys applications in a systemd container.
- Store schema 8: application layout, persistent paths and limits; releases, domains and jobs
  tables; job actors; deployment job and release links.
