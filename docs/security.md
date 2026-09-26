# Security

WASM runs as root and deploys code from repositories onto the machine it manages. This page
states what it defends against, what it does not, and every control involved, so an operator
can decide how to expose it.

## Threat model

**Trusted**

- Root on the machine, and anyone who can run `wasm` as root. WASM does not defend the
  machine against its own administrator.
- The console's master token and any session signed in with it: they are equivalent to root.
- Admin-scoped API tokens, for the same reason.

**Defended against**

- **Anyone who can reach the console over the network** without a credential: the console
  listens on loopback by default, refuses cleartext beyond loopback, and locks out repeated
  failures.
- **Holders of narrow credentials**: a `read` token cannot read secrets or other processes'
  command lines, change anything or cancel jobs; a `deploy` token can only update, roll back
  and activate releases of applications that already exist - it cannot create one or name a
  source for the machine to fetch and build.
- **A hijacked browser tab or a malicious page**: CSRF tokens, `SameSite=Strict` cookies, a
  strict Content Security Policy with Trusted Types, and sudo mode before destructive actions.
- **Untrusted repository content**: a repository can contain symlinks, odd file names and
  values that end up in unit files and web server configuration.
- **Unprivileged local users**: secrets never travel in a command line (which `ps` shows to
  every user), and every file holding one is `0600`.
- **Forged webhooks** and **server-side request forgery** through notification URLs.

**Not defended against**

- **Applications from each other.** Every application's unit runs as the same account by
  default (`service_user`, `www-data`), so one compromised application can read the files,
  including the `.env`, of every other one. WASM is not a multi-tenant isolation boundary.
- **A compromised application reaching the console on loopback.** It still needs a
  credential, but it is on the same host.
- **Anything with root**, including a malicious package or dependency installed during a
  build, which runs as root during `npm install` or `pip install`.

## Processes

Everything WASM executes (nginx, systemctl, certbot, git, npm, database clients) goes through
one module, `CommandRunner`, and the test suite fails if any other module imports
`subprocess`.

- **Argv only, never a shell.** A string where an argument list is expected is refused, and so
  is a NUL byte. A repository or a form field cannot inject shell syntax.
- **Every call has a timeout.**
- **Secrets travel through the environment, standard input or a private defaults file**,
  never the command line. That includes every database password WASM hands to a client.
- **Running as another account** uses one prefix, `runuser -u <user> --`. There is no `sudo`
  in any command WASM runs.
- `--dry-run` is implemented in the same place: read-only commands still run, everything else
  is recorded and skipped. Files go through one seam that a rehearsal swaps out the same way,
  the console's own state included: a rehearsed `wasm token create` or `wasm sessions revoke`
  reads the real key and session database and writes neither, and on a fresh machine creates
  no `/etc/wasm` at all.

Cron jobs follow the same rule: a job's command is split like a POSIX shell would split it
and run without a shell, so pipes, `&&` and globs are inert text. Write `/bin/sh -c "..."`
explicitly when a job needs a shell.

## Exposing the console

The console acts as root, so how it is reached matters more than anything else on this page.

| Setup | Command |
|---|---|
| Loopback only (default). Reach it through an SSH tunnel. | `wasm web start -d` |
| TLS served by WASM | `wasm web start -d --host 0.0.0.0 --tls-cert fullchain.pem --tls-key privkey.pem` |
| TLS with a self-signed certificate | `wasm web start -d --host 0.0.0.0 --self-signed` |
| Behind a TLS-terminating reverse proxy | `wasm web start -d --trusted-proxy 127.0.0.1` |

- **Beyond loopback, TLS is mandatory.** Binding to any other address without a certificate
  and key (or `--self-signed`) is refused, both when the options are parsed and again where
  the socket is bound. `--insecure-http` overrides this, and says what it does: the access
  token and the session cookie then cross the network in clear.
- `--self-signed` mints a certificate under `/etc/wasm/panel-tls/` and reuses it while it is
  valid.
- `--allow-ip ADDR/CIDR` (repeatable) answers only those clients; everyone else gets `403`
  before authentication. It restricts who connects; it encrypts nothing, and does not lift the
  TLS rule.
- `--trusted-proxy ADDR/CIDR` is the only way `X-Forwarded-For`, `X-Real-IP` and
  `X-Forwarded-Proto` are believed, and only from that peer. By default WASM trusts nobody's
  forwarding headers. Declaring the proxy is what makes the session cookie `Secure` and the
  client address in the audit log the real one.
- Requests are rate limited. A request without a valid credential (the sign-in, the forge
  webhooks, anything anonymous) is counted per client address, 120 a minute by default
  (`web.rate_limit_requests`). A request with a valid credential is counted per credential
  instead (one sign-in, one API token, the master token), 1200 a minute by default
  (`web.rate_limit_authenticated_requests`): over an SSH tunnel every request comes from
  `127.0.0.1`, and a per-address budget made every tab and script share one. A wrong
  credential buys nothing: it is counted as anonymous, and guessing is stopped by the
  lockout below, which is separate. The console's shell (`index.html` on any console
  address) and its hashed build assets spend no budget, so reloading the page cannot lock
  the operator out. A refusal is `429` with `"error": "rate_limited"` and a `Retry-After`
  of the seconds until the budget frees; every counted response carries
  `X-RateLimit-Limit` and `X-RateLimit-Remaining` for the budget it was counted in. Both
  budgets share `web.rate_limit_window` (60 seconds) and `web.rate_limit_enabled`.
- Request bodies are capped before authentication and before routing: 1 MiB for everything
  but the forge webhooks, which get 5 MiB. A larger body is refused with `413` and
  `"error": "payload_too_large"`, from its `Content-Length` or, for a chunked body, as it is
  read. The API's largest legitimate bodies (a unit file, a site configuration, an `.env`)
  are a few kilobytes.

## Authentication

**The master token.** Every start of the console issues a new access token and prints it
once, in the same banner whether `wasm web start` runs in the foreground or, with `-d`, in
the background. Only a hash is stored, salted with the console's signing key, in
`/etc/wasm/web-token` (`0600`); a token cannot be shown again, only replaced with `wasm web
token --new`.

A running console reads the token hash from disk on every request, so rotating it takes
effect immediately - no restart needed. To retire a token that may have leaked, just run
`wasm web token --new`; the token it was started with stops working at once, in every
console already running, including one in the background.

`--regenerate` goes further: it rotates the signing key too, which immediately signs out
every session, and, because API tokens and TOTP backup codes are salted with that same key,
also invalidates every one of those - issue new ones afterwards with `wasm token create` and
`wasm 2fa backup-codes`.

**API tokens.** `wasm token create NAME --scope read|deploy|admin [--expires-hours N]`, or
the console's Settings. Tokens start with `wasm_tok_`, are shown once, and only their salted
hash is kept. `wasm token list` shows every token ever issued, live and revoked, without the
token itself; `wasm token revoke ID` takes effect on the next request.

**Sessions.** Signing in with the master token (and the second factor, when enabled) creates
a server-side session in `/etc/wasm/web-sessions.db`.

- Cookie `wasm_session`: `HttpOnly`, `SameSite=Strict`, `Secure` whenever the request arrived
  over TLS (directly or through a declared proxy).
- 12 hours of inactivity ends a session; activity renews it, rotating its id, but never past
  24 hours from sign-in. A rotated id keeps working for 30 seconds so requests in flight do
  not fail, and each of those requests is handed the same successor: a session renews into
  one new session, never several. Signing out or revoking a session also ends the id it was
  renewed from.
- A session is bound to the client address it was issued to.
- `wasm sessions list`, `wasm sessions revoke PREFIX`, `wasm sessions revoke-others`, and the
  console's Settings manage them.

**CSRF.** Every `POST`, `PUT`, `PATCH` and `DELETE` made with a session must carry the
session's CSRF token in the `X-WASM-CSRF` header; the console reads it from the `wasm_csrf`
cookie. That holds whichever channel carries the session: a client that signed in with
`bearer: true` and presents the session token as `Authorization: Bearer` sends the CSRF token
it received in the same answer. The master token and API tokens are not sessions and have no
CSRF token.

**Two-factor authentication.** TOTP (RFC 6238: SHA-1, 6 digits, 30 seconds, one step of
clock drift either way), enrolled with `wasm 2fa enroll` and `wasm 2fa confirm CODE` or from
the console. Confirming prints eight backup codes once; only their salted hashes are kept.
When enabled, sign-in requires a code or an unused backup code. A code is accepted once per
purpose - signing in, sudo mode, turning 2FA off: the time step it matched is remembered, and
that code, or an older one still inside the drift window, is refused for the same purpose
afterwards. Enrolling and confirming from the console need sudo mode, confirmed with the
master token, since 2FA is not on yet.

**Lockout.** Five failed credentials from one address lock it out for 15 minutes. Every
channel counts against the same budget: sign-in, sudo-mode elevation, disabling 2FA, bearer
tokens, session cookies, a credential presented to `GET /api/auth/session`, and WebSocket
handshakes. A session token this console signed but that has since expired or been revoked is
refused without being counted: it cannot be a guess, and counting it would lock out an
operator whose browser merely outlived its session. A locked-out address is refused at
`/api/auth/login`, `/api/auth/elevate` and `/api/auth/2fa/disable`, on any request carrying a
bearer token or a session cookie, and on WebSocket handshakes. The lockout is the attacker's
address; an operator connecting from elsewhere is not affected. Webhook signatures are
counted per application instead (see [Outbound requests](#outbound-requests)).

## Authorization

| Scope | May |
|---|---|
| `read` | Every `GET`: applications, logs, metrics, jobs, deployments. `.env` values come back redacted. |
| `deploy` | `read`, plus queueing an update or a rollback, and activating a release, of an application that already exists. |
| `admin` | Everything else. |

The policy is stated once, in the authentication chokepoint, and applies to every request:
reads need `read`, the deploy operations above need `deploy`, and every other mutation needs
`admin`. Creating an application and inspecting a source are `admin`: both fetch whatever
source the caller names and build or read it as root. A few reads are raised to `admin`:
listing API tokens, reading the audit log, and revealing an application's `.env`. Process
listings (`GET /api/system/processes`, `GET /api/monitor/processes`, monitor observations)
show command lines, which often carry passwords, only to `admin`; other scopes see the
process name. The master token and console sessions are always `admin`.

**Local paths.** Creating an application from, or inspecting, a directory on the machine
rather than a repository URL reaches every file on it. It is accepted from the master token,
and from a console session in sudo mode; an API token is refused with `403` whatever its
scope. Scripts deploy from repositories.

**Stored sources.** A clone URL with a credential in it (`https://user:token@host/...`, or
`https://token@host/...`), stored by an older release, is shown with the credential replaced
by `***` in every application read; the stored value is left as it was, because updates clone
from it.

## Sudo mode

Destructive and credential-changing actions ask a console session to prove it is still its
operator: `POST /api/auth/elevate` with a current TOTP code or backup code, or the master
token when 2FA is off. The session is then elevated for 10 minutes. The console shows a
"confirm it's you" dialog and retries the action.

It is required for:

- deleting an application, a database, a database user, a service, a site, a certificate, an
  application backup, a backup schedule, a cron job, or a domain from an application;
- uninstalling a database engine;
- restoring an application backup or a database backup; revoking or deleting a certificate;
- revealing an application's `.env` in clear, and writing it;
- moving an application to releases; changing its resource limits;
- editing a unit or a site configuration by hand;
- running a SQL statement in write mode;
- writing WASM's configuration, whole or by section (webserver, backup, SSL, web, apps
  directory);
- issuing an API token; enrolling or confirming 2FA; disabling 2FA; regenerating backup codes;
- creating a service, from a raw unit or from fields; creating or rewriting a cron job;
  creating or rewriting a backup schedule - each is a command systemd runs as root, on a
  timer or for as long as the machine is up;
- deploying or inspecting a local path (see [Local paths](#authorization));
- `POST /api/jobs/delete`, which queues the same deletion `DELETE /api/apps/{domain}` does.

Sudo mode applies to sessions, whichever channel carries the session token: the cookie, or
`Authorization: Bearer` for a client that signed in with `bearer: true`. The master token and
API tokens are not asked to elevate: issuing that credential already required the operator
once. The exemption follows what the credential is, never the header it arrived in.

## Streams

The event stream (`GET /events`) and the WebSockets (`/ws/...`) are authenticated once, at
the handshake, and then stay open for hours. So:

- **The credential is checked again** every 30 seconds on a WebSocket and every 25 on
  `/events`. Revoking an API token, rotating the master token (`wasm web token --new` or
  `--regenerate`), signing out, revoking a session or letting it expire ends every stream it
  opened: a WebSocket closes with `4401`, the event stream ends. A session renewal does not;
  it is the same sign-in under a new id.
- **One credential holds at most 8 WebSockets at once.** Every log socket is a
  `journalctl -f` running as root; the ninth handshake is refused with `4429` until one
  closes. The budget is per credential, so a script at its limit does not stop the console.
- **A WebSocket lasts at most 12 hours.** It then closes with `4408`, and the client
  reconnects, authenticating again.
- **`/ws/logs/{name}` streams only a unit WASM manages**, decided by the same ownership rule
  every other service operation goes through. A unit WASM did not create, `ssh` for example,
  is refused.

## The browser

The console is a static build served by WASM itself, loads nothing from any other origin,
and runs under this policy:

```
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self';
  img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none';
  base-uri 'none'; form-action 'self'; object-src 'none'; require-trusted-types-for 'script'
```

There is no `unsafe-inline` for scripts or styles, and Trusted Types forbid string-to-DOM
sinks. The end-to-end suite fails on any CSP violation.

Other headers: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: no-referrer`, `Cross-Origin-Opener-Policy: same-origin`,
`Cross-Origin-Resource-Policy: same-origin`,
`Permissions-Policy: geolocation=(), microphone=(), camera=()`, and
`Strict-Transport-Security: max-age=31536000; includeSubDomains` when the request came over
TLS. Everything is `Cache-Control: no-store` except the content-hashed files under
`/assets/`, which are immutable for a year.

## Databases

The console's SQL runner is read-only unless told otherwise, and that is enforced by the
database server, not by inspecting the statement:

- **PostgreSQL**: the statement runs in a `READ ONLY` transaction in a session that signs in
  over `127.0.0.1`, with a password, as a dedicated role `wasm_ro_<database>`: not a
  superuser, `default_transaction_read_only` on, granted only `CONNECT`, `USAGE` and `SELECT`.
  Because the login itself is the limit, `SET ROLE`, `RESET ROLE`, `set_config('role', ...)`
  and `SET SESSION AUTHORIZATION` have nothing to return to, and the server refuses
  superuser-only functions such as `pg_read_file` and `COPY ... TO PROGRAM`. The password is
  random, set as a SCRAM verifier, and kept in a 0600 root-owned file under
  `/var/lib/wasm/secrets/postgresql/`. `pg_hba.conf` must allow
  `host <database> wasm_ro_<database> 127.0.0.1/32 scram-sha-256` (the Debian and Ubuntu
  default `host all all 127.0.0.1/32` line does); if it does not, read mode fails with that
  line in the error and never falls back to the superuser.
- **MySQL and MariaDB**: the statement runs in a `READ ONLY` transaction as a dedicated
  account with `SELECT` on that one database and nothing else (no `FILE`, so no
  `LOAD_FILE()` or `INTO OUTFILE`). Its password is rotated on every call and never stored.
- **MongoDB and Redis** have no read-only mode; every statement is a write and needs sudo
  mode.

Only one statement is accepted per request. The statement text is logged by the console
process (logger `wasm.audit`); the request itself is in the audit log like every mutation.

The statement reaches the client as data, never as a script: psql gets it as a `-c` string
(a leading backslash is refused) and mysql reads it in `--binary-mode` with its client
commands (`system`, `\!`, `source`, `tee`, `pager`) refused, so no shell escape is reachable
from the console.

Restoring a dump runs its SQL with the engine's administrative account, so a restore trusts
the dump's SQL; it needs sudo mode and is audited. The client never reads the dump as a
script: a PostgreSQL plain dump with psql meta-commands (`\!`, `\o`, `\set`, `\connect`)
outside its COPY data is refused before anything is dropped, pg_dump's own
`\restrict`/`\unrestrict` excepted; MySQL and MariaDB read the dump on stdin in
`--binary-mode`, where `system`, `\!` and `source` are off.

## Outbound requests

- **Notifications** (webhook, Slack, Discord, Telegram) refuse destinations in loopback,
  private (RFC 1918), carrier-grade NAT, link-local (which covers cloud metadata at
  `169.254.169.254`), the unspecified address (`0.0.0.0/8`, `::`) and their IPv6 equivalents.
  An IPv6 address that carries an IPv4 one - IPv4-mapped (`::ffff:0:0/96`), NAT64
  (`64:ff9b::/96`), 6to4 (`2002::/16`) - is judged by the IPv4 address inside it, so
  `::ffff:127.0.0.1` is loopback like `127.0.0.1`. Every redirect hop is checked again, so a
  public name that redirects or rebinds to a private address is still refused. Names you
  trust can be exempted with `notifications.allow_private_hosts`. Only `http` and `https` are
  accepted, and the "test" button never echoes the remote response back.
- **Deploy webhooks** (`POST /hooks/deploy/{domain}`) verify `X-Hub-Signature-256` (GitHub)
  or `X-Gitea-Signature` (Gitea) as HMAC-SHA256 of the body, or `X-Gitlab-Token` (GitLab),
  all in constant time. An unknown application and one without a webhook secret look the
  same from outside. A bad signature is counted against that application, not the address:
  after 10 in 15 minutes its hook answers `429` (`"error": "locked_out"`, with `Retry-After`)
  for 15 minutes, while every other application's deliveries keep arriving. A forge sends
  every customer's deliveries from a few shared addresses, so counting against the address
  let anyone with an account on the same forge lock it out of the console. Deliveries larger
  than 5 MiB are refused with `413`. The per-application secret is stored in the WASM store
  (`0600`), because verifying an HMAC needs it.

What WASM itself sends off the machine: git fetches from your repositories, certbot's
requests to Let's Encrypt, notifications you configured, and, on every CLI run whose cached
answer is older than five minutes, a request to `api.github.com` for the latest WASM release.
There is no setting to turn that check off in 2.0.

## Untrusted repositories

- Nothing is written through a symlink found inside a release or under `shared/`. A tracked
  link to `/etc` does not become a path WASM writes to as root.
- Persistent paths must be relative and may not contain `..`.
- Every value interpolated into a systemd unit (the start command, the working directory,
  environment variables) is validated and escaped: a newline cannot start a new directive.
- Release ids are validated before they reach the disk, and a commit id must be hexadecimal
  before it becomes part of a directory name.
- `ServiceManager` refuses to touch a unit that WASM does not own, whatever the caller.

## Files

| Path | Mode | Holds |
|---|---|---|
| `/etc/wasm/` | `0700` | Configuration and the console's state |
| `/etc/wasm/config.yaml` | `0600` | Configuration, secrets included. `wasm config get` and the API print secrets as `***`; `wasm config show` prints them in clear |
| `/etc/wasm/web-secret` | `0600` | Signing key for sessions and token hashes |
| `/etc/wasm/web-token` | `0600` | Hash of the master token |
| `/etc/wasm/web-totp` | `0600` | TOTP secret and backup code hashes |
| `/etc/wasm/web-sessions.db` | `0600` | Sessions, API token hashes, WebSocket tickets |
| `/etc/wasm/web-audit.log` | `0600` | Audit trail |
| `/var/lib/wasm/wasm.db` | `0600` | The store: applications, deployments, jobs, webhook secrets |
| `/var/lib/wasm/job-logs/` | `0700`, files `0600` | Output of console jobs |
| `/var/lib/wasm/deploy-logs/` | `0750`, files `0640` | Build logs, readable by an admin group |
| `.env`, `shared/.env` | `0600` | Application environment, owned by the service account |
| `/etc/systemd/system/*.service` | `0644` | Units. See the note below. |

The store falls back to `~/.local/share/wasm/` when `/var/lib/wasm` is not writable; `wasm
store path` prints where it is. The console state directory can be moved with
`WASM_WEB_STATE_DIR`.

Environment variables given when an application is created (`wasm create --env-file`, or
`env_vars` in `POST /api/apps`) are written into its `.env` file (`0600`, owned by the
service account, `shared/.env` on the releases layout), and the unit loads it with
`EnvironmentFile=`. Only `PORT` and `NODE_ENV` - not secret, and WASM's to decide - stay
inline in the unit as `Environment=` lines. systemd lets `EnvironmentFile=` override
`Environment=`, so `wasm env configure` and the console's Environment tab both refuse to set
either one there; change the port by redeploying (`wasm create -d <domain> -s <source>
--port <port>`, or `POST /api/apps` with the new port) instead. A unit an earlier
version wrote, with every variable inline, keeps working as it is; the next `wasm update` or
redeploy moves them into the `.env` file.

## Audit log

`/etc/wasm/web-audit.log` receives one JSON line per event: time, action, result, actor (a
session id, `token:<name>` or `master`), client address, resource and detail. Credentials are
never written to it. Recorded:

- every state-changing API request (`POST`, `PUT`, `PATCH`, `DELETE` under `/api`), with its
  path and outcome;
- sign-ins and sign-outs, failed credentials, CSRF and scope refusals, lockouts;
- API token, session and 2FA changes, sudo elevations;
- `.env` reveals and writes, configuration writes;
- webhook deliveries and webhook secret changes, WebSocket connections and tickets.

The file is opened append-only and rotated at 5 MB, keeping three old files, so flooding it
can only push out the oldest entries rather than fill the disk. Read it with an admin
credential from the console's Activity page or `GET /api/audit`.

## The monitor

`wasm monitor` reports what it sees and does nothing else: it never signals or kills a
process, never deletes or modifies a file other than its own unit, and never decides anything
from a process's command line. See [MONITOR.md](MONITOR.md).

## Reporting a vulnerability

Please report security issues privately, by email to yago.lopez.adeje@gmail.com, rather
than in a public GitHub issue. Include the WASM version (`wasm --version`), the
distribution, and the steps to reproduce.
