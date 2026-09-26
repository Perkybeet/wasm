# Upgrading to 2.0

This guide is written against 1.6.4. Older 1.x releases upgrade the same way: the store
migrations run from any earlier schema.

The short version: upgrading the package changes nothing about the applications you have
deployed. Their units, sites, directories and certificates are left exactly as they are, and
they keep updating in place, as in 1.x, until you move each one to releases yourself. What
changes at once is the browser interface, some of the API, and the defaults for applications
you create from now on.

## What changes

### Your applications

- **Existing applications stay in place.** `wasm update` on an application deployed by 1.x
  does what it did in 1.6.5: backup, pull, rebuild, restart. Nothing is converted
  implicitly; moving an application to the release layout is `wasm app migrate DOMAIN`, one
  application at a time (see [releases.md](releases.md)).
- **New applications build releases.** The new setting `deploy.layout` defaults to
  `releases`: every deploy of an application created from now on builds in its own directory,
  goes live only if it answers, and rolls back by itself when it does not. `wasm create
  --layout inplace`, or `wasm config set deploy.layout inplace`, keeps the 1.x behaviour.
  Monorepo and Docker Compose projects are always deployed in place.
- **`wasm create` without `--type` detects the type.** In 1.x it deployed every application
  created without `--type` as Node.js. A script that relied on that should pass
  `--type nodejs`.
- **`wasm create` refuses a directory that already holds files.** In 1.x, deploying a domain
  whose application directory already existed overwrote it, and a failed deploy could delete
  it. 2.0 refuses unless you pass `--force`, and even then never deletes a directory it did
  not create. Redeploying an application that is already on releases needs no flag.
- **One operation per application at a time.** An update, deploy, rollback, migration,
  restore or deletion takes a lock on the application; a second one started meanwhile (a
  webhook during a manual update, say) is refused with the name of the operation that holds
  it, instead of both running over the same tree.
- **`www` is a redirect.** `wasm create --www` now records `www.<domain>` as a redirect to the
  domain (a `301`), where 1.x served the application on both names. Applications deployed by
  1.x with `--www` keep serving both: the first `wasm domain add` or `remove` on one of them
  records the names its site already answers on as aliases. See [domains.md](domains.md).
- **The store is migrated on first use.** The first `wasm` command after the upgrade moves
  the store from schema 3 to schema 8: layout, persistent paths and resource limits on each
  application, and new tables for releases, domains and persisted jobs. The store also
  switches to SQLite's WAL mode. **1.6.4 cannot read the migrated store** (it fails with
  `unexpected keyword argument 'layout'`), which is why the procedure below backs it up.

### The panel is replaced by the console

- `wasm web start` and its options, the address it listens on, and the state in `/etc/wasm`
  (signing key, sessions, two-factor enrolment, audit log) carry over. Every start issues a
  new access token and prints it once, in the same banner whether it runs in the foreground
  or, with `-d`, in the background - unlike 1.6.4, which printed nothing for a background
  start.
- The server-rendered pages, the `/login` form and the htmx fragments are gone. The console
  is a single-page application served from the same address. A bookmark to a 1.x page may
  land on "Page not found"; certificates and sites are now under Domains and certificates.
- **Sudo mode.** Deleting, restoring, revealing or writing an `.env`, editing units and sites,
  SQL in write mode, writing settings, issuing API tokens and disabling 2FA ask the browser
  session to confirm it is you (a TOTP or backup code, or the access token when 2FA is off).
  A confirmation lasts 10 minutes.

### The API

- **Removed: `POST /api/jobs/deploy`.** Create applications with `POST /api/apps` (scope
  `deploy`), which answers `202` with the job.
- **Removed: the `/ws/system` WebSocket.** Use the `machine` events of `/events`, or
  `GET /api/system/machine`, which returns the same object.
- **`/events` carries JSON only.** In 1.x the `machine` event was an HTML fragment. It now
  sends JSON `machine`, `metrics`, `app`, `job`, `state` and `notice` events, and accepts a
  bearer token like any other `GET`. See [api.md](api.md#realtime).
- **One error shape everywhere:** `{error, detail, hint, fields, output}`, with `detail`
  always a string. A `422` no longer carries FastAPI's list of errors in `detail`; it has
  `"detail": "Validation failed"` and a `fields` map from field name to message.
- **Sudo mode for cookie sessions:** a destructive request from a browser session without a
  recent confirmation answers `403` with `"error": "elevation_required"`; confirm with
  `POST /api/auth/elevate` and retry. Requests with `Authorization: Bearer` are not affected.
- **The `deploy` scope** covers `POST /api/apps`, `POST /api/apps/inspect`,
  `POST /api/jobs/update`, `POST /api/jobs/rollback` and
  `POST /api/apps/{domain}/releases/{id}/activate`.
- **Timestamps** carry an explicit UTC offset.
- **Jobs are persisted** with their logs; a job that was running when the console restarted
  is marked failed ("Interrupted by a panel restart") instead of vanishing.
- New endpoints: `GET /api/openapi.json`, `GET /api/auth/session`, `POST /api/auth/elevate`,
  deployments and their logs, releases and activation, migration plan and migration, domains
  and DNS checks, resource limits, diagnosis, server health, audit log, webhook deliveries,
  notification tests, cron schedule preview, unit verification, site configuration tests.

### Security behaviour that may need a setting

- **Notification destinations on private networks are refused.** Webhook, Slack, Discord and
  Telegram URLs that resolve to loopback, RFC 1918, carrier-grade NAT or link-local addresses
  (and their IPv6 equivalents) no longer receive anything. If you deliver to an internal
  service, allow its host name: `wasm config set notifications.allow_private_hosts
  hooks.internal.example --list`. Test with `wasm notify test webhook`.
- **Bad webhook signatures count toward the lockout**: together with failed sign-ins and
  bad tokens, five from one address lock that address out of sign-in, bearer-token requests
  and WebSockets for 15 minutes.
- **The read-only SQL runner uses a dedicated role** (PostgreSQL) or account (MySQL,
  MariaDB) per database with nothing but `SELECT`, created on first use with the engine's
  administrative credentials.

### The CLI

New commands: `wasm releases`, `wasm app migrate|limits`, `wasm domain`, `wasm diagnose`,
`wasm cron`, `wasm config get|set`, `wasm token`, `wasm sessions`, `wasm 2fa`,
`wasm notify test`. `--json` on `list`, `status` and `logs`. `--open` prints console URLs.

## Upgrading a production server

Upgrading does not stop or restart any application. The console is unavailable between
steps 2 and 6.

### 1. Take stock

```bash
wasm --version
wasm list                          # keep this output to compare afterwards
wasm store path                    # where the store is
wasm store stats
wasm web status
systemctl list-timers 'wasm-*'     # scheduled backups and cron jobs about to run
```

### 2. Stop the panel

A running 1.x panel keeps serving the old code after the package is replaced, and holds the
store open. Note the options it was started with (`--host`, TLS, `--trusted-proxy`,
`--allow-ip`), then:

```bash
wasm web stop
```

Avoid running other `wasm` commands, and let any scheduled backup that is about to start
finish, before you copy the store.

### 3. Back up

```bash
install -d -m 700 /root/wasm-1.6-backup
cp -a /var/lib/wasm /root/wasm-1.6-backup/var-lib-wasm     # the store, deploy logs
cp -a /etc/wasm /root/wasm-1.6-backup/etc-wasm             # configuration and console state
wasm store export -o /root/wasm-1.6-backup/store.json      # every record, readable
```

If `wasm store path` printed a location outside `/var/lib/wasm`, copy that directory too.

Keep a way to reinstall 1.6.4:

- **Debian and Ubuntu:** the `.deb` is attached to the GitHub release:
  `https://github.com/Perkybeet/wasm/releases/tag/v1.6.4` (`wasm_1.6.4-1_all.deb`).
- **Fedora and openSUSE:** OBS publishes only the latest build. Save the installed package
  before upgrading, while the repository still serves it: `dnf download wasm-cli`, or copy it
  from the zypper cache.
- **PyPI:** `pip install 'wasm-cli==1.6.4'` stays available.

### 4. Upgrade the package

```bash
apt update && apt install --only-upgrade wasm        # Debian, Ubuntu
dnf upgrade --refresh wasm-cli                       # Fedora
zypper refresh && zypper update wasm-cli             # openSUSE
pip install --upgrade 'wasm-cli[all]'                # PyPI
```

The package runs `wasm config upgrade`, which adds the new settings and keeps every value you
set, and reinstalls and restarts the monitor unit if it is enabled.

### 5. Verify

```bash
wasm --version                     # 2.0.x
wasm list                          # the same applications, in the same states
wasm health
wasm status shop.example.com       # for a few applications
wasm config get deploy.layout      # releases, unless you want new applications in place
```

If `wasm list` says no applications are deployed, the records are not lost: WASM is reading
the store from another location. Compare `wasm store path` with the location you noted in
step 1 before doing anything else.

### 6. Start the console and sign in

```bash
wasm web start -d <the options you noted>
wasm web token --new               # the token to sign in with
```

Sign in, check that every application appears with its state, and try one action that asks
for sudo mode (revealing an `.env`, for example). If you have not enrolled two-factor
authentication, do it now under Settings > Security. If you use notifications, send a test
from Settings > Notifications, or `wasm notify test <channel>`.

### 7. Move applications to releases, one at a time

Start with the least critical application. For each one:

```bash
wasm backup create shop.example.com -m "Before moving to releases"
wasm --dry-run app migrate shop.example.com     # read the plan; nothing changes
wasm app migrate shop.example.com               # asks for confirmation
wasm releases list shop.example.com             # one release, active
curl -sI https://shop.example.com | head -1     # it answers
wasm update shop.example.com                    # the first release built from source
```

`--dry-run` is a global option: it goes before `app`. Read the plan before confirming:

- **Moves to shared/** should list the `.env` and every directory the application writes
  into (uploads, storage, SQLite files). If one is missing, name them all with
  `--persist PATH`, repeated; `--persist` replaces detection.
- **"untracked file(s) stay in the first release only"** means the next deploy will not have
  those files. Name the directory holding them with `--persist`.
- **"is not a git checkout"** means WASM could only guess the usual upload directories. Name
  every directory the application writes into.

Nothing is deleted or copied, the file count is checked before and after, and if the
application does not answer on the release layout the migration puts everything back as it
was. The console does the same from the application's Settings > Releases.

Monorepo and Docker Compose applications are not migrated: they keep deploying in place.

## Rolling back the upgrade

Going back to 1.6.4 is simple as long as no application was created or migrated on the
release layout. If you want to keep that option open for a while, set
`wasm config set deploy.layout inplace` and do not migrate until you have decided to stay.

1. Stop the console: `wasm web stop`.
2. Reinstall 1.6.4:

   ```bash
   apt install --allow-downgrades ./wasm_1.6.4-1_all.deb      # Debian, Ubuntu
   dnf downgrade ./wasm-cli-1.6.4-*.noarch.rpm                # Fedora, the package you saved
   zypper install --oldpackage ./wasm-cli-1.6.4-*.noarch.rpm  # openSUSE, the package you saved
   pip install 'wasm-cli==1.6.4'                              # PyPI
   ```

3. Put the 1.6.4 store and configuration back. The 2.0 store cannot be read by 1.6.4, and its
   WAL files must not be left next to the restored database:

   ```bash
   mv /var/lib/wasm /var/lib/wasm.2.0
   cp -a /root/wasm-1.6-backup/var-lib-wasm /var/lib/wasm
   mv /etc/wasm /etc/wasm.2.0
   cp -a /root/wasm-1.6-backup/etc-wasm /etc/wasm
   ```

4. Check with `wasm --version` and `wasm list`, then start the panel as before.

What does not come back:

- **Applications created or migrated under 2.0.** The restored store has no record of their
  layout, and 1.6.4 does not understand `releases/`, `current` and `shared/`. Their units and
  sites point at `current` and keep serving, but 1.6.4 cannot update them. There is no
  automated way back from the release layout: redeploy them, or stay on 2.0.
- **Domains added under 2.0** are no longer on record. They stay in the site files until the
  next redeploy renders the site again without them.
- Deployment history, jobs and audit entries recorded under 2.0 (kept in
  `/var/lib/wasm.2.0` and `/etc/wasm.2.0`).

## Known limitations in 2.0

**Deploy engine**

- Monorepo and Docker Compose projects deploy in place: no releases, no health-gated
  activation (a failed health check is reported, not rolled back), no migration, and no
  aliases or redirects. They are fronted by nginx only: Compose ignores `--webserver apache`,
  and a monorepo created with `--webserver apache` gets no site configuration.
- Docker Compose applications take their resource limits from the compose file. Limits given
  when creating one through the API are accepted and not applied.
- The health gate probes `/` on the application's port and accepts any status below 500. It
  is not configurable; an application whose `/` answers 5xx cannot pass it.
- Activating a release restarts the unit: expect the restart's worth of downtime.
  Blue/green activation is not in 2.0.
- Release retention is five per application and cannot be changed from the CLI, the console
  or the API.
- An application in place keeps 1.x behaviour: it is built over the live tree, and a failed
  health check after the restart is reported, not rolled back.
- Migrating a tree that is not a git checkout needs every writable directory named with
  `--persist`; untracked files outside a persistent path stay in the first release only.
- `wasm cron create --app` defaults the working directory to the application directory, not
  `current/`: for an application on releases, pass `--working-directory`.
- `--pm` accepts `npm`, `pnpm`, `yarn` and `bun` (`POST /api/apps` takes the same as
  `package_manager`); left out, it is detected from the lock file. Monorepo and Docker Compose
  applications ignore it.
- Variables given with `wasm create --env-file` (or `env_vars` on `POST /api/apps`) are
  written into the application's `.env` file, and the unit loads it with `EnvironmentFile=`.
  Only `PORT` and `NODE_ENV` stay inline in the unit, which local users can read; `wasm env
  configure` and the console's Environment tab both refuse to set either from the `.env`
  file, since that would silently override what the unit and nginx expect. A unit from
  before this change, with every variable inline, keeps working as it is: the next `wasm
  update` or redeploy moves them into the `.env` file, once, automatically.
- Python applications need the `venv` module. The Debian and Ubuntu package depends on
  `python3-venv`; a pip installation on those systems has to install it by hand.

**Domains**

- The DNS check compares the name's addresses with the machine's interfaces, so it reports a
  false negative behind NAT and behind a proxying CDN. It is advisory only.
- Wildcard domains are not accepted.

**Console and API**

- `wasm web start -d` is a background process, not a systemd unit: it does not start again
  after a reboot. It now prints its access token the same way a foreground start does, and a
  running console reads the token from disk on every request, so issuing a new one with
  `wasm web token --new` retires the old one at once, with no restart needed.
- WebSocket tickets from `POST /api/auth/ws-ticket` now also work for an API token, not only
  a browser session: the ticket redeems as that same token, its scope included (admin or
  otherwise). Before, the endpoint accepted the request but no handshake could ever redeem
  the ticket it issued.
- Webhook secrets are created from the console or the API only; there is no CLI command.
- Every CLI run checks GitHub for a newer release at most every five minutes;
  `wasm config set updates.check false` turns that off.
