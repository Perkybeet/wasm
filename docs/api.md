# API

Everything the console does, it does through the API described here: the console is a
client of it like any script. The contract is exported as OpenAPI and served, authenticated,
at `GET /api/openapi.json`.

Examples use `https://panel.example.com` for the console's address and `$TOKEN` for a
credential. On a console bound to loopback, run them on the server against
`http://127.0.0.1:8080`, or through an SSH tunnel (see [console.md](console.md)).

## Authentication

Two ways in, for two kinds of client.

### Bearer tokens, for scripts

```bash
wasm token create ci-update --scope deploy                 # prints the token once
wasm token create dashboard --scope read --expires-hours 720
curl -H "Authorization: Bearer $TOKEN" https://panel.example.com/api/apps
```

API tokens start with `wasm_tok_`, are shown once, and are stored only as a salted hash.
Manage them with `wasm token list|revoke`, the console's Settings > API tokens, or
`GET|POST /api/auth/tokens` and `DELETE /api/auth/tokens/{id}` (admin scope; issuing one
from a browser session also needs sudo mode). The master access token is also accepted as a
bearer credential, with admin scope.

The master token and API tokens carry no session, so they need no CSRF token and are never
asked for sudo mode. A *session* token presented as a Bearer credential (see `bearer` below)
is still a session: it needs the CSRF header and sudo mode exactly like the cookie.

### Scopes

| Scope | Allows |
|---|---|
| `read` | Every `GET`, `HEAD` and `OPTIONS`, except the three below. `.env` values come back redacted. |
| `deploy` | `read`, plus `POST /api/jobs/update`, `POST /api/jobs/rollback`, `POST /api/apps/{domain}/releases/{id}/activate`: moving an application that already exists. |
| `admin` | Everything. |

Every other `POST`, `PUT`, `PATCH` and `DELETE` needs `admin` - creating an application
(`POST /api/apps`) and inspecting a source (`POST /api/apps/inspect`) included, since both
fetch and build or read whatever source they are given, as root. Three reads also need
`admin`: `GET /api/auth/tokens`, `GET /api/audit`, and `GET /api/apps/{domain}/env?unmask=true`
(which from a session also needs sudo mode). Process listings answer every scope, but only
`admin` sees each process's `command`; other scopes get `null` (or `""` in
`/api/monitor/processes`). A credential without the scope gets `403` with
`"error": "forbidden"` and a detail naming the scope it has and the one required.

A `source` that is a local path rather than a repository URL is accepted by `POST /api/apps`
and `POST /api/apps/inspect` only from the master token or a session in sudo mode (`403`
`elevation_required` until it confirms). An API token gets `403` `forbidden`, whatever its
scope. Application reads show a credential stored inside a clone URL as `***`.

### Sessions, for browsers

```bash
curl -c jar -H 'Content-Type: application/json' \
  -d '{"token": "wasm_...", "totp_code": "123456"}' \
  https://panel.example.com/api/auth/login
```

`POST /api/auth/login` takes `token` (the master access token), `totp_code` (a TOTP code or a
backup code, required when 2FA is on) and `bearer` (also return the session token in the
body, for a client without a cookie jar; it then sends `X-WASM-CSRF` like a browser does).
It answers `{success, expires_in, csrf_token, session_token}` and sets two cookies:

- `wasm_session`: `HttpOnly`, `SameSite=Strict`, `Secure` over TLS.
- `wasm_csrf`: readable by the page, holding the CSRF token.

Every `POST`, `PUT`, `PATCH` and `DELETE` made with a session, in the cookie or as a Bearer
token, must echo the CSRF token in the `X-WASM-CSRF` header. `GET /api/auth/session` (no
credential required) reports whether the caller is signed in, its scope, `expires_at`,
`elevated_until`, whether 2FA is on, the hostname, the WASM version and the CSRF header and
cookie names. A credential presented to it that is wrong counts toward the lockout like
anywhere else; one the console signed that has merely expired does not.

A failed sign-in answers `401` with `error` set to `invalid_token`, `totp_required` or
`invalid_totp`. A TOTP code is accepted once per purpose (signing in, `elevate`, disabling
2FA); sending it again answers `invalid_totp`. Five failures from one address lock it out
for 15 minutes (`429`, `"error": "locked_out"`, with `Retry-After`), on every request that
carries a credential, the session cookie included.

### Sudo mode

Some operations ask a session to confirm its operator: deleting an application, a
database, a user, a service, a site, a certificate, a backup or a domain; restoring a
backup; revoking a certificate; revealing or writing an `.env`; migrating to releases;
changing resource limits; editing a unit or site by hand; creating a service; creating or
rewriting a cron job or a backup schedule; deploying or inspecting a local path; SQL in
write mode; writing the configuration; issuing an API token; enrolling, confirming or
disabling 2FA; regenerating backup codes.

Without a recent confirmation they answer:

```json
HTTP/1.1 403 Forbidden

{"error": "elevation_required", "detail": "Confirm it's you to continue",
 "hint": "POST /api/auth/elevate with your two-factor code, or the master token.",
 "fields": null, "output": null}
```

`POST /api/auth/elevate` with `{"code": "123456"}` (TOTP or backup code) when 2FA is on, or
`{"token": "wasm_..."}` when it is off, elevates the session for 10 minutes and answers
`{"elevated_until": "..."}`. Then retry the request. The master token and API tokens are
exempt; a session is not, whichever header carries it.

## Errors

Every error from every router has the same shape:

```json
{
  "error": "validation_error",
  "detail": "What happened",
  "hint": "How to fix it, or null",
  "fields": {"domain": "Not a valid domain"},
  "output": "The system's own output (nginx -t, systemctl, certbot), or null"
}
```

- `error` is a stable machine code. For WASM's own exceptions it is the exception class in
  lower case (`deploymenterror`, `certificateerror`, `serviceerror`, ...); otherwise one of
  `unauthorized`, `forbidden`, `not_found`, `conflict`, `validation_error`, `rate_limited`,
  `locked_out`, `elevation_required`, `payload_too_large`, `app_busy`, `invalid_token`,
  `totp_required`, `invalid_totp`, `internal`.
- `detail` is one sentence for a person. `hint` is the suggested fix, or `null`.
- `fields` is set on `422` validation errors, `null` otherwise: a map from field name to
  message, which the console shows next to each form control.
- `output` carries a system tool's own output verbatim, when there is one. Show it in a
  monospace block; do not paraphrase it.

Refusals made before routing (address not allowed, TLS required, body too large, rate limit,
lockout) have the same keys except `output`.

| Status | When |
|---|---|
| `400` | Invalid input WASM checked itself: a domain, a name, a path, a configuration value, a source |
| `401` | No credential, or an invalid or expired one |
| `403` | Scope too narrow, sudo mode required, address not allowed |
| `404` | Unknown application, database, job, release... |
| `409` | Conflict: the domain is taken, the database exists, the application is in place and has no releases; `app_busy` when another deploy, update, rollback, migration, restore or deletion is running on the application (`detail` names it; wait for it, or follow it in Jobs) |
| `413` | Request body over the limit, checked before authentication: 1 MiB, or 5 MiB under `/hooks/` |
| `422` | Request body failed validation; see `fields` |
| `429` | Rate limit (120 requests a minute per address by default) or lockout |
| `500` | A system operation failed; `detail`, `hint` and `output` say which and why |

## Conventions

- **Timestamps** are ISO 8601 with an explicit offset.
- **Long operations run as jobs.** Creating an application, updating, deleting, backing up,
  restoring and issuing certificates answer `202` with `{job_id, status, message, job}`.
  Follow the job with `GET /api/jobs/{id}`, its log with `GET /api/jobs/{id}/log?tail=N`, the
  `job` events on `/events`, or `/ws/jobs/{id}`. A finished deploy or update job carries the
  `deployment_id` of its history row. Jobs and their logs are persisted; a job left
  running when the console restarts is marked failed with "Interrupted by a panel restart".
  Only a job that has not started can be cancelled (`POST /api/jobs/{id}/cancel`).
- **Lists** answer an object with the items and a count: `{apps, total}`,
  `{backups, total}`, `{items, total}`, and so on. The one exception is
  `GET /api/system/disks`, a bare array.
- **Pagination.** Two endpoints page with a keyset cursor:
  - `GET /api/deployments?limit=50&before_id=N`: answers `{items, total, next_before_id}`;
    pass `next_before_id` back as `before_id` until it is `null`. `limit` is at most 200.
    Filters: `domain`, `status` (`queued`, `running`, `success`, `failed`, `rolled_back`),
    `trigger` (`panel`, `cli`, `webhook`).
  - `GET /api/audit?limit=50&before=T`: answers `{items, next_before}`. `limit` at most 200.
    Filters: `action`, `result`, `actor`.

  Other lists take a `limit` and return the newest entries: `GET /api/jobs` (at most 100),
  `GET /api/backups` (at most 1000), `GET /api/monitor/observations` and the process lists
  (at most 500). There is no `offset` or `page` parameter anywhere.
- **Logs** take a tail: `GET /api/apps/{domain}/logs?lines=N` (at most 1000),
  `GET /api/jobs/{id}/log?tail=N` (lines), `GET /api/deployments/{id}/log?tail=N` (bytes).

## Realtime

### `GET /events` (Server-Sent Events)

One stream carries everything the console updates live. It authenticates like any `GET`:
the session cookie or a bearer token with `read` scope.

```bash
curl -N -H "Authorization: Bearer $TOKEN" https://panel.example.com/events
```

The stream starts with a `: connected` comment and sends a `: keepalive` comment after 25
seconds of silence. It sends no `id:` or `retry:` fields: after a disconnection, reconnect and
refetch what you display. A client that cannot keep up loses the oldest queued frames. The
credential is checked again every 25 seconds; once it is revoked, rotated or expired the
stream ends, and the reconnection answers `401`.

| Event | When | Data |
|---|---|---|
| `machine` | Every 5 seconds | `{hostname, uptime_s, load: [1m, 5m, 15m], load_history, cpu_percent, memory: {used, total, percent}, disk: {used, total, percent}, units: {running, failed, stopped}, apps: {running, failed, stopped, static}}`. Same object as `GET /api/system/machine`. |
| `metrics` | Every 2 seconds | A flat map of the latest samples: `cpu.percent`, `mem.used_bytes`, `mem.total_bytes`, `swap.used_bytes`, `disk.used_bytes`, `disk.total_bytes`, `net.rx_bytes_s`, `net.tx_bytes_s`, `load.1m`, and per application `app.<domain>.cpu.percent`, `app.<domain>.mem.bytes`. |
| `job` | On every job transition and log line | The job as `GET /api/jobs/{id}` returns it, with `logs` trimmed to the newest entry, plus `domain`, `message`, `level` and `finished`. |
| `state` | With every `job` event | `{id, state}` for the job, and again for its domain when it has one. `state` is `busy`, `active`, `failed` or `idle`. |
| `notice` | When a job completes, fails or is cancelled | `{text, state}`: a one-line message for a toast. |
| `app` | When an application changes (a job on it ends, an action on it succeeds) | The application as `GET /api/apps/{domain}` returns it; `{domain, status: "deploying"}` while a deploy, update or restore runs; `{domain, deleted: true}` when it is gone. |

### WebSockets

| Path | Streams |
|---|---|
| `/ws/logs/{domain}?lines=N` | The application's journal: the last `N` lines (1 to 500, default 50), then follows. `{domain}` may also name a unit WASM manages; any other unit is refused. |
| `/ws/jobs/{id}` | One job, until it finishes. |
| `/ws/jobs` | Every job's transitions. |
| `/ws/events` | Journal entries of units named `wasm-*` (cron and backup timers, the monitor, legacy application units). |

A handshake authenticates with any one of:

- the session cookie (a browser on the console's own origin);
- `Authorization: Bearer <token>` (any client that can set headers);
- the subprotocol header `Sec-WebSocket-Protocol: wasm.auth, wasm.token.<token>`;
- `?ticket=<ticket>`, from `POST /api/auth/ws-ticket` (answers `{ticket, expires_in}`): single
  use, valid for 30 seconds, and bound to the address it was issued to. This is how the
  console connects. Tickets are issued for cookie sessions; a script should use the header.

A long-lived token is never accepted in the query string. A handshake from a foreign
`Origin` is refused.

Close codes: `4401` not authenticated, or the credential stopped being valid while the socket
was open; `4403` forbidden (origin, address, or a unit WASM does not manage); `4408` the
socket reached its 12 hour lifetime, reconnect; `4429` rate limited, locked out, or the
credential already holds 8 open sockets.

An open socket re-checks its credential every 30 seconds: revoking the API token, rotating
the master token or signing out closes it with `4401` (after an `{"type": "error"}` frame).
A session renewal does not close it.

Frames are JSON:

- `/ws/logs/{domain}` sends `{"type": "connected", "domain", "service"}`, then
  `{"type": "log", "data": "<line>"}` per journal line, `{"type": "warning", "data"}` for
  journalctl's own complaints and `{"type": "error", "message"}`. Every socket lasts at most
  12 hours; reconnect after that.
- `/ws/jobs/{id}` sends `{"type": "connected", "job"}`, `{"type": "update", "job"}` on every
  change, `{"type": "finished", "job"}` once the job completes, fails or is cancelled, and
  `{"type": "heartbeat"}` after 30 quiet seconds. An unknown id gets `{"type": "error"}` and
  the socket closes.
- Every socket answers `{"type": "ping"}` with `{"type": "pong"}`. `/ws/jobs/{id}` also
  accepts `{"type": "cancel"}` from an admin credential; any other scope is answered with an
  error and nothing is cancelled.

```bash
websocat -H "Authorization: Bearer $TOKEN" \
  "wss://panel.example.com/ws/logs/shop.example.com?lines=100"
```

## Examples

Create an application and follow its deploy (an admin credential):

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"domain": "shop.example.com", "source": "https://github.com/you/shop.git"}' \
  https://panel.example.com/api/apps
# 202 {"job_id": "a1b2c3d4", "status": "pending", "message": "...", "job": {...}}

curl -s -H "Authorization: Bearer $TOKEN" https://panel.example.com/api/jobs/a1b2c3d4
curl -s -H "Authorization: Bearer $TOKEN" "https://panel.example.com/api/jobs/a1b2c3d4/log?tail=200"
```

`POST /api/apps` accepts `domain`, `source`, `app_type` (default `auto`), `port`,
`webserver`, `branch`, `ssl`, `env_vars`, `layout` (`inplace` or `releases`; omitted, the
server's `deploy.layout`), `include_www`, `persistent_paths`, `memory_max_mb`,
`cpu_quota_percent`, `tasks_max`, and for monorepos and Compose projects
`subdomain_overrides`, `workspace_filter`, `skip_database`, `compose_file`,
`compose_profiles`. `POST /api/apps/inspect` with `{"source", "branch"}` previews what would be
deployed (type, commands, port, variables from `.env.example`) without deploying anything.

Update, and roll back to a release:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"domain": "shop.example.com"}' https://panel.example.com/api/jobs/update

curl -s -H "Authorization: Bearer $TOKEN" https://panel.example.com/api/apps/shop.example.com/releases
curl -s -X POST -H "Authorization: Bearer $TOKEN" \
  https://panel.example.com/api/apps/shop.example.com/releases/20260924-101500-9f8e7d6/activate
# {"domain", "release_id", "previous_id", "changed", "rolled_back", "deployment_id"}
```

Activation answers when the release has passed the health gate, or fails with the probe's and
the journal's output after putting the previous release back.

Why is it down:

```bash
curl -s -H "Authorization: Bearer $TOKEN" https://panel.example.com/api/apps/shop.example.com/diagnose
# {"domain", "verdict": "down", "probable_cause": "...", "checks": [{"name", "status", "summary", "evidence"}]}
```

Deployment history:

```bash
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://panel.example.com/api/deployments?domain=shop.example.com&status=failed&limit=20"
curl -s -H "Authorization: Bearer $TOKEN" "https://panel.example.com/api/deployments/42/log"
```

## Deploy webhooks

`POST /api/apps/{domain}/webhook-secret` (admin) creates or replaces an application's webhook
secret and answers `{domain, secret, hook_url}`; the secret is shown once. Configure the forge
to send push events to `hook_url`, which is `https://<console>/hooks/deploy/{domain}`:

| Forge | Header checked |
|---|---|
| GitHub | `X-Hub-Signature-256: sha256=<HMAC-SHA256 of the body>` (set the webhook secret) |
| Gitea | `X-Gitea-Signature: <HMAC-SHA256 of the body>` |
| GitLab | `X-Gitlab-Token: <secret>` |

A delivery for a branch other than the application's answers `200 {"status": "ignored",
"reason": "branch"}`; a forge retry of the same delivery answers `"reason": "duplicate"`.
An accepted delivery queues an update (`202 {job_id, status}`), exactly like `wasm update`.
An unknown application and one without a secret both answer `404`; a bad signature answers
`401` and counts against that application: after 10 in 15 minutes its hook answers `429`
(`"error": "locked_out"`, `Retry-After`) for 15 minutes. Other applications, and the forge's
address, are not affected. A delivery larger than 5 MiB answers `413`. `DELETE /api/apps/{domain}/webhook-secret` turns
webhooks off; `GET /api/apps/{domain}/webhook/deliveries` lists the deployments webhooks
triggered. The hook must be reachable from the forge, which means exposing the console (see
[security.md](security.md)).

## Health

`GET /health` answers `{"status": "healthy", "service": "wasm-web"}` without authentication,
for load balancers and uptime checks. It says nothing about the machine; use
`GET /api/system/health` (authenticated) for that.

## Endpoint reference

The rule: `GET` needs `read`, the operations listed under [Scopes](#scopes) need `deploy`,
everything else needs `admin`. In the tables, "deploy" marks the `deploy` operations and
"sudo" what a browser session must confirm first.
Request and response bodies are in `/api/openapi.json`.

### Applications

| Endpoint | |
|---|---|
| `GET /api/apps` | List, with state, layout, limits, last deployment |
| `POST /api/apps` | Deploy a new application (job). A local path needs sudo |
| `POST /api/apps/inspect` | Preview a source without deploying. A local path needs sudo |
| `GET /api/apps/types` | Application types |
| `GET /api/apps/{domain}` | One application |
| `DELETE /api/apps/{domain}` | Delete (job). sudo |
| `POST /api/apps/{domain}/start`, `/stop`, `/restart` | Control its unit |
| `GET /api/apps/{domain}/logs` | Journal tail |
| `GET /api/apps/{domain}/env` | `.env`, redacted; `?unmask=true` needs admin and sudo |
| `PUT /api/apps/{domain}/env` | Replace the `.env`; the answer says a restart is required. sudo |
| `PATCH /api/apps/{domain}/limits` | `{memory_max_mb, cpu_quota_percent, tasks_max, restart}`; null removes a limit. sudo |
| `GET /api/apps/{domain}/releases` | Releases, newest first |
| `POST /api/apps/{domain}/releases/{id}/activate` | Instant rollback or roll forward. deploy |
| `GET /api/apps/{domain}/rollback-points` | Backups usable by a backup rollback |
| `GET /api/apps/{domain}/migrate/plan` | What moving to releases would do |
| `POST /api/apps/{domain}/migrate` | Move to releases. sudo |
| `GET /api/apps/{domain}/diagnose` | Why it is down |
| `GET /api/apps/{domain}/domains` | Its domains |
| `POST /api/apps/{domain}/domains` | Add an alias or redirect; certificate extension is a job |
| `DELETE /api/apps/{domain}/domains/{name}` | Remove one. sudo |
| `GET /api/apps/{domain}/domains/{name}/dns` | Does it resolve here |
| `GET /api/domains/dns?name=` | Same check, for a name not attached yet |
| `POST`, `DELETE /api/apps/{domain}/webhook-secret` | Enable (rotate) or disable webhooks |
| `GET /api/apps/{domain}/webhook/deliveries` | Deployments triggered by webhooks |

### Jobs and deployments

| Endpoint | |
|---|---|
| `GET /api/jobs`, `GET /api/jobs/active` | Jobs (`limit`, `status`, `domain`) |
| `GET /api/jobs/{id}`, `GET /api/jobs/{id}/log` | One job, its log |
| `POST /api/jobs/{id}/cancel` | Cancel a job that has not started |
| `POST /api/jobs/update` | `{domain}`. deploy |
| `POST /api/jobs/rollback` | `{domain, backup_id}`: restore a backup. deploy |
| `POST /api/jobs/backup` | `{domain, description}` |
| `POST /api/jobs/cert` | `{domain, email, webserver, include_www}` |
| `POST /api/jobs/delete` | `{domain, remove_files, remove_ssl}`. sudo |
| `DELETE /api/jobs/cleanup` | Forget finished jobs in memory (`max_age_hours`); the history stays |
| `GET /api/deployments`, `/{id}`, `/{id}/log` | Deployment history and build logs |

### Sites, certificates, services, cron

| Endpoint | |
|---|---|
| `GET`, `POST /api/sites`; `GET /api/sites/templates` | Sites and the templates to create them from |
| `GET /api/sites/{domain}`, `GET /api/sites/{domain}/config` | One site, its configuration file |
| `POST /api/sites/{domain}/config/test` | Test a configuration without installing it |
| `PUT /api/sites/{domain}/config` | Install a configuration after testing it. sudo |
| `POST /api/sites/{domain}/enable`, `/disable`; `POST /api/sites/reload` | |
| `DELETE /api/sites/{domain}` | Delete a site, its configuration and certificate. sudo |
| `GET /api/certs`, `GET /api/certs/{domain}` | Certificates, expiry, issuer |
| `POST /api/certs/{domain}` | Obtain |
| `POST /api/certs/{domain}/renew`, `POST /api/certs/renew-all` | Renew |
| `POST /api/certs/{domain}/revoke`, `DELETE /api/certs/{domain}` | sudo |
| `GET /api/services`; `POST /api/services/verify` | Services; check a unit with `systemd-analyze verify` |
| `POST /api/services` | Create a service, from a raw unit or from fields. sudo |
| `GET /api/services/{name}`, `/logs`, `/config` | |
| `POST /api/services/{name}/start`, `/stop`, `/restart`, `/enable`, `/disable` | |
| `PUT /api/services/{name}/config`, `DELETE /api/services/{name}` | sudo |
| `GET /api/cron`; `POST /api/cron/preview` | Jobs; the next runs of a schedule |
| `POST /api/cron` | Create or rewrite a job. sudo |
| `DELETE /api/cron/{name}`; `POST /api/cron/{name}/run`, `/enable`, `/disable` | |
| `GET /api/cron/{name}/runs` | Recent runs, from the journal |

### Backups and databases

| Endpoint | |
|---|---|
| `GET`, `POST /api/backups`; `GET /api/backups/storage` | |
| `GET /api/backups/{id}`; `POST /api/backups/{id}/verify` | |
| `POST /api/backups/{id}/restore`, `DELETE /api/backups/{id}` | sudo |
| `GET /api/backup-schedules` | Scheduled backups |
| `POST /api/backup-schedules`, `DELETE /api/backup-schedules/{domain}` | Create or rewrite, delete. sudo |
| `GET /api/databases/engines`; `GET /api/databases/engines/{engine}/status`, `/logs`, `/privileges` | |
| `POST /api/databases/engines/{engine}/install`, `/uninstall`, `/start`, `/stop`, `/restart` | |
| `GET`, `POST /api/databases/databases`; `GET /api/databases/databases/{engine}/{name}` | |
| `DELETE /api/databases/databases/{engine}/{name}` | sudo |
| `POST /api/databases/users`, `/users/grant`, `/users/revoke`; `GET /api/databases/users/{engine}` | |
| `DELETE /api/databases/users/{engine}/{username}` | sudo |
| `GET`, `POST /api/databases/backups` | |
| `POST /api/databases/backups/restore` | sudo. A PostgreSQL plain dump holding a psql meta-command (`\!`, `\o`, `\connect`...) outside COPY data is refused before anything is dropped |
| `POST /api/databases/query` | One statement; `mode: "write"` needs sudo. PostgreSQL read mode signs in over `127.0.0.1` as `wasm_ro_<database>` with a password, so `pg_hba.conf` must allow `host <database> wasm_ro_<database> 127.0.0.1/32 scram-sha-256` (Debian and Ubuntu's default `host all all 127.0.0.1/32` line does) |
| `POST /api/databases/connection-string` | |

### Machine, monitor, configuration, audit, authentication

| Endpoint | |
|---|---|
| `GET /api/system`, `/machine`, `/cpu`, `/memory`, `/disks`, `/network`, `/processes`, `/health`, `/version` | `command` in `/processes` for admin only |
| `GET /api/metrics`, `GET /api/metrics/{metric}` | Stored metric history for charts |
| `GET /api/monitor/status`, `/config`, `/metrics`, `/processes`, `/observations` | |
| `POST /api/monitor/scan`, `/install`, `/uninstall`, `/enable`, `/disable`, `/start`, `/stop`, `/test-email` | |
| `POST /api/monitor/observations/{id}/acknowledge` | |
| `GET /api/config`, `/defaults`, `/apps-directory`, `/webserver`, `/backup`, `/ssl`, `/web` | Secrets come back as `***` |
| `PUT`, `PATCH /api/config`; `PUT /api/config/{section}` | sudo |
| `POST /api/config/reload` | |
| `POST /api/config/notifications/{channel}/test` | Send a test through `webhook`, `slack`, `discord`, `telegram` or `email` |
| `GET /api/audit` | admin |
| `POST /api/auth/login`, `/logout`, `/elevate`, `/ws-ticket`; `GET /api/auth/session`, `/verify` | |
| `GET /api/auth/sessions`; `POST /api/auth/sessions/revoke-all`, `/revoke-others`; `DELETE /api/auth/sessions/{prefix}` | |
| `GET /api/auth/2fa` | |
| `POST /api/auth/2fa/enroll`, `/confirm`, `/disable`, `/backup-codes` | sudo |
| `GET`, `POST /api/auth/tokens`; `DELETE /api/auth/tokens/{id}` | admin; issuing needs sudo |
| `GET /api/openapi.json` | This contract |

FastAPI's own `/docs`, `/redoc` and `/openapi.json` are disabled: the schema of an API that
runs systemd as root is only served to authenticated callers.
