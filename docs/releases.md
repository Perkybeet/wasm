# Releases

WASM 2.0 builds every deploy of a new application in its own directory, switches to it
atomically, keeps it only if it answers, and can go back to any release still on disk in
seconds. This page describes that layout, how a deploy moves through it, and how an
application deployed by 1.x is moved onto it.

## Two layouts

| Layout | Who gets it | Deploy | Rollback |
|---|---|---|---|
| `releases` | Every application created by 2.0, unless told otherwise | A new directory per deploy, health-gated activation, automatic rollback | Re-point `current` and restart: seconds |
| `inplace` | Every application deployed by 1.x, and types that cannot build releases yet | Backup, pull and rebuild over the tree the service is running, restart. A failed health check after the restart is reported, not rolled back, except for Docker Compose and monorepo applications, which [go back automatically](#docker-compose-and-monorepo-applications). | Restore a backup |

The layout of a new application comes from `--layout` on `wasm create` (or `layout` on
`POST /api/apps`), and otherwise from the `deploy.layout` setting, which defaults to
`releases`:

```bash
wasm config get deploy.layout
wasm config set deploy.layout inplace    # new applications build in place, as in 1.x
```

An existing application always keeps the layout it has. A deploy or an update never changes
it; asking for a different layout on a redeploy is refused with an error. The only way from
`inplace` to `releases` is the explicit migration described [below](#moving-an-in-place-application-onto-releases).

Monorepo and Docker Compose applications cannot build releases yet. With the server default
they are deployed in place, as before; asking for `--layout releases` explicitly is an error
rather than a silent downgrade. Their updates still pass the health gate and go back to what
was serving when they fail it; see [Docker Compose and monorepo applications](#docker-compose-and-monorepo-applications).

## Layout on disk

```
/var/www/apps/{app}/
  releases/20260925-143012-a1b2c3d/   one complete build; never rebuilt once it is active
  releases/20260924-101500-9f8e7d6/
  current -> releases/20260925-143012-a1b2c3d
  shared/.env                          the environment, mode 0600, outside every release
  shared/<persistent paths>            uploads, storage, data: linked into each release
  repo/                                git cache the releases are exported from
```

`{app}` is the domain with dots replaced by dashes (`shop.example.com` becomes
`shop-example-com`). The systemd unit and the web server site point at `current`, never at a
release directly, so the same unit and site serve whichever release is active.

- A release id is `YYYYMMDD-HHMMSS-<commit>`: the UTC creation time and the first seven
  characters of the commit, or `nogit` for a source that is not a git repository. A second
  release created in the same second gets `-2`, and so on.
- Every link is relative, so the application directory can be moved or restored from a
  backup elsewhere and still resolve.
- `repo/` is an ordinary clone that is fetched and reset on each deploy; the target commit is
  exported into the new release without `.git`. Local directories and archives are copied or
  extracted straight into the release.

## What a deploy does

For an application on `releases`, a deploy (`wasm create`, the new-application wizard) and an
update (`wasm update`, the console's Update button, `POST /api/jobs/update`, a push webhook)
run the same sequence:

1. **Fetch into a new release.** The source is exported into `releases/<id>/`. The running
   application does not see this directory.
2. **Link what releases share.** `shared/.env` is linked into the release when it exists,
   and each persistent path becomes a link to `shared/<path>`. This happens before the
   install and the build, because both may need the environment or write into a persistent
   directory.
3. **Install dependencies, or reuse them.** When every lockfile (`package-lock.json`,
   `pnpm-lock.yaml`, `yarn.lock`, `bun.lockb`, `requirements.txt`, `poetry.lock`, `uv.lock`,
   `Pipfile.lock`) is byte-identical to the active release's, `node_modules`, `.venv` or
   `venv` is copied from the active release with `cp -a --reflink=auto` instead of being
   reinstalled. The deploy log says `Dependencies reused from <release>`. Otherwise the
   install runs normally.
4. **Build** inside the release.
5. **Hand the tree over** to the service user.
6. **Write the site, the certificate and the unit**, all pointing at `current`. Only a
   deploy (`wasm create`, `POST /api/apps`) does this; an update skips it, because the unit
   and the site already point at `current`.
7. **Activate behind the health gate.** See the next two sections.
8. **Prune** releases beyond the retention.

`wasm update` on an application on `releases` takes no backup first: the release that was
serving stays on disk and is what an automatic or manual rollback returns to.

## The health gate

Activation swaps `current` atomically: a new link is created next to it as
`current.tmp-<random>` and renamed over the old one, so `current` resolves at every instant.
Then the unit is restarted and probed.

| Application | Probe |
|---|---|
| Runs a process (Node, Next.js, Python, ...) | `GET http://127.0.0.1:<port><path>`, one attempt every 2 seconds for the timeout, 5 seconds each. By default the path is `/`, the timeout 30 seconds (15 attempts), and any status below 500 passes, redirects included (they are not followed). |
| Serves files only (static sites, Vite builds without SSR) | The files it serves are there: an `index.html` in the directory the web server serves. |

The same gate judges a deploy, an update, an instant rollback, a migration and a change of
resource limits with `--restart`, so none of them can activate something another would have
refused. `wasm diagnose` and the console's Diagnose page probe the same path and judge the
answer by the same statuses.

### Configuring the health check

An application that has a health endpoint, needs longer to start, or must not count a 404 as
up can say so. Three settings, each with the default above when unset:

| Setting | Accepts | Default |
|---|---|---|
| Path | A path on the application: begins with a single `/`, printable ASCII, no spaces. A query string is fine (`/health?deep=1`); a scheme or a host is refused, because the probe always asks the application itself on `127.0.0.1`. | `/` |
| Expected statuses | Statuses and inclusive ranges from 100 to 599, separated by commas: `200`, `200-399`, `200,204`, `200-299,301`. Only these pass; a redirect is not followed, so a `301` passes only if it is listed. | any status below 500 |
| Timeout | Seconds from 5 to 600 the release gets to answer: one probe every 2 seconds for that long. | 30 |

```bash
wasm app health shop.example.com                                   # the current settings, defaults marked
wasm app health shop.example.com --path /healthz --expect 200-299
wasm app health shop.example.com --timeout 120                     # options not named keep their value
wasm app health shop.example.com --reset                           # back to every default
```

Over the API, `PATCH /api/apps/{domain}/health` sets the three together (a field left out or
null goes back to its default) and needs sudo mode. See [api.md](api.md). The values are
validated where they are stored, so the CLI, the console and the API refuse the same input
with the same message. Nothing restarts: the next activation uses the new settings. A redeploy
keeps them. A static site has no settings to change: its check is its files.

## Automatic rollback

When a new release fails the gate:

1. The release is recorded as `failed` and its directory is removed.
2. `current` is pointed back at the release that was serving, and the unit restarted and
   probed again.
3. The deployment fails with the evidence verbatim: every failed probe (identical
   consecutive failures folded into one line) and the last 40 lines of the unit's journal.

The error says which release is active again and whether it answered. A failed release stays
in `wasm releases list` for a while, marked as not on disk, and cannot be activated. If the
very first release of an application fails, there is nothing to go back to and the deploy
fails; a first deploy that fails undoes the steps it ran.

## Instant rollback

```bash
wasm releases list shop.example.com                  # newest first; the active one has an asterisk
wasm releases rollback shop.example.com              # the release created just before the active one
wasm releases rollback shop.example.com 20260924-101500-9f8e7d6
```

Nothing is rebuilt: `current` is re-pointed and the unit restarted, behind the same health
gate. If the target does not answer, the release that was serving is put back and the command
fails with the probe's and the journal's output. Rolling forward works the same way: name a
newer release.

In the console, the Deployments tab of an application lists its releases; activating one is
the same operation. Over the API:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
  https://panel.example.com/api/apps/shop.example.com/releases/20260924-101500-9f8e7d6/activate
```

This needs a token with the `deploy` scope, the same as queueing an update. See
[api.md](api.md).

Release statuses, as `wasm releases list --json` and `GET /api/apps/{domain}/releases`
report them: `active`, `superseded`, `rolled_back`, `failed`, `built`. `active` is whatever
`current` points at, regardless of what the store row says.

### Releases and backups

`wasm releases rollback` and `wasm rollback` are different tools:

- `wasm releases rollback` switches between builds that are already on disk. It is instant,
  and it does not touch `shared/`: the environment and uploaded files are the same before and
  after.
- `wasm rollback` restores a backup archive, taking a safety backup of the current state
  first. It is the recovery tool for data and for in-place applications. See `wasm backup`.

## Rebuilding a deployment's commit

Update pulls the head of the branch. To deploy exactly the commit an earlier deployment was
built from, name it:

```bash
wasm update shop.example.com --commit 9f8e7d6      # full or abbreviated, at least 4 characters
```

In the console, "Redeploy" on a deployment's page does the same:
`POST /api/apps/{domain}/deployments/{id}/rebuild` queues the update job with that
deployment's commit and answers `202` with the job. It needs the `deploy` scope, like an
update, and answers `409 no_commit` for a deployment whose source is not git.

The id must name exactly one commit. It is looked up in the clone first; one the clone does
not have is fetched (a full id by name, an abbreviation by fetching every branch, with the
rest of the history when the clone is shallow). An id that names more than one commit asks
for more characters; one that names none is an error. `--commit` does not combine with
`--source` or `--branch`.

- **On releases**, when a release built from that commit is still on disk and is not the
  active one, it is activated behind the health gate, as `wasm releases rollback` would:
  nothing is built. Otherwise, including when the commit is the one that is live, the commit
  is exported from `repo/` into a new release and built like any update. Rebuilding the live
  commit is how a changed environment or a broken dependency install gets a clean build.
- **In place**, the pre-update backup is taken, the checkout is detached at the commit
  (`git checkout --force --detach`: tracked files are rewritten, untracked ones such as
  uploads stay), and the tree is rebuilt and restarted. The branch the checkout was on is
  remembered in the repository's own configuration (`wasm.branch`), so the next
  `wasm update` without `--commit` checks that branch out again and pulls it: the
  application follows its branch again. The deployment history records that branch, not
  `HEAD`.

## Nothing new to deploy

Before an update from the console or from `wasm update`, WASM asks the remote for the head
of the branch the application follows (`git ls-remote`: nothing is downloaded) and compares
it with the commit that is live, the active release's or the in-place checkout's.

When they are the same:

- The console's update (`POST /api/jobs/update`) answers `409` with `error: "nothing_new"`,
  `detail` "No new commits on main since 9f8e7d6, which is live" and a hint: rebuilding the
  same commit still makes sense when the environment or the dependencies changed, or the
  last build broke. Sending `{"domain": ..., "force": true}` rebuilds anyway.
- `wasm update` says the same and asks whether to rebuild anyway. `-y` or `--force` skips
  the question. Without a terminal (a script, cron) there is nobody to ask: it rebuilds and
  says so.

The check is skipped, and the update goes ahead, for a source that is not git (a local
directory or an archive), for an update with `--commit` or `--source`, and when the remote
cannot be asked (the reason is printed as a warning; the update itself then reports the
failure in full). A push webhook never checks: the push is itself the news.

## Rolling back to a deployment

Every deployment in the history says whether it can be gone back to:
`GET /api/deployments` and `GET /api/deployments/{id}` carry `rollback_available` and, when it
is false, `rollback_unavailable_reason`. `POST /api/apps/{domain}/deployments/{id}/rollback`
queues it as a job (`202`), with the `deploy` scope; a deployment that cannot be gone back to
answers `409 rollback_unavailable` with the reason.

- **On releases**, going back to a deployment activates the release it built, behind the
  health gate. It must still be on disk and not already active; a pruned one can be rebuilt
  from its commit instead.
- **In place**, it restores the deployment's snapshot. When an in-place update takes its
  pre-update backup, that backup holds exactly what the previous deployment produced, so it
  is recorded as that deployment's `snapshot_backup`. It is only recorded when it is true:
  the most recent finished deployment must have succeeded (after a failed build the tree is
  whatever the failure left) and its commit must match the tree's. Going back restores the
  snapshot with `wasm rollback`'s machinery: a safety backup of the current state first, the
  restore, a rebuild and a start, recorded as its own history row. The deployment that is live
  has no snapshot until the next update takes one; the typical use is the update that broke,
  whose pre-update backup is the snapshot of the deployment before it. A snapshot whose backup
  was rotated away is no longer offered.

## Persistent paths and `shared/`

Anything an application writes for itself that must survive a deploy (user uploads, a
SQLite file, `storage/`) has to live in `shared/`, because every deploy starts from a fresh
release directory.

- `.env` is always shared. `wasm env`, the console's Environment tab and the backups read
  and write `shared/.env` for an application on `releases`, and `.env` in the application
  directory for one in place.
- Other paths are declared per application: `wasm create --persist storage --persist
  public/uploads`, or `persistent_paths` on `POST /api/apps`. Paths are relative to the
  application root and may not be absolute or contain `..`.
- If a release already contains something at a persistent path (usually a file tracked in the
  repository), it is left exactly as it is and reported as a conflict in the deploy log. The
  tracked copy wins; untrack it or drop it from the persistent paths.
- WASM never writes through a symlink found inside a release or under `shared/`. A
  repository is untrusted input, and a tracked link to `/etc` must not become a place WASM
  writes to as root.

## Retention

After a successful activation, releases beyond the newest `N` are deleted, oldest first, where
`N` is the application's retention (5 unless it was changed). The active release is never
deleted, even when a rollback made it older than the newest `N`, and neither is the release
just before it, which is what `wasm releases rollback` goes back to. So even a retention of 1
keeps the way back. Rows of failed releases stay listed while they are among the newest `N`
and are forgotten after that.

```bash
wasm releases keep shop.example.com        # how many it keeps
wasm releases keep shop.example.com 10     # keep ten, from 1 to 50
```

Changing it prunes at once rather than at the next deploy, and says which releases were
removed. Raising it removes nothing, and cannot bring back a release already pruned. Over the
API, `PATCH /api/apps/{domain}/releases/retention` with `{"keep": 10}`, in sudo mode; the
current value is `keep_releases` in `GET /api/apps/{domain}`. An in-place application has no
releases, so it has no retention to set.

## Moving an in-place application onto releases

An application deployed by 1.x keeps running and updating in place after the upgrade, exactly
as before. Moving it onto releases is an explicit operation, one application at a time:

```bash
wasm --dry-run app migrate shop.example.com     # the plan, and a rehearsal that changes nothing
wasm app migrate shop.example.com               # asks for confirmation; -y to skip it
wasm app migrate shop.example.com --persist storage --persist public/uploads
```

`--dry-run` is a global option and goes before the command. The console offers the same
operation from the application's Settings tab; the API has `GET /api/apps/{domain}/migrate/plan`
(read only) and `POST /api/apps/{domain}/migrate` (needs sudo mode), which queues the migration
as a job and answers `202` with it.

What the migration does:

1. **The unit is stopped first**, so nothing writes into the tree while it moves. The plan
   states this downtime; it lasts until the health gate passes, usually seconds.
2. **The live tree becomes the first release.** It is moved, not copied: every change is a
   rename inside the application directory, or the creation of a directory or a link.
3. **The environment moves to `shared/`.** `.env` and WASM's inventory of it (`.wasm`) are
   moved to `shared/` and linked into the release.
4. **What the application wrote for itself moves to `shared/`.** In a git checkout that is
   every untracked directory, ignored or not (`git status --ignored`), minus build output
   (`node_modules`, `.next`, `dist`, `build`, `.venv`, `__pycache__` and similar). Without
   git, WASM cannot tell uploads from code: it keeps whichever of `uploads`,
   `public/uploads`, `storage` and `data` exist, and warns you to name the rest. `--persist`
   replaces detection entirely. SQLite databases are found by their file header wherever
   they are (outside build output) and always move to `shared/`, with their `-wal`, `-shm`
   and `-journal` files beside them; a `--persist` list that leaves one out is refused.
5. **The unit and the site are rewritten to run from `current`**, when they name the
   application directory. A proxied site only names a port and is left alone.
6. **The file count is verified.** Regular files and their bytes are counted before and after,
   `shared/` included. Any difference fails the migration.
7. **The application goes through the health gate.** If it does not answer, every rename is
   reversed, the unit and site are put back byte for byte, and the application is restarted
   on the tree it had. Every undo step is attempted even when an earlier one fails; anything
   that could not be put back is listed in the error with where it is now.

Read the plan before confirming. Two warnings in it matter:

- **Untracked files outside a persistent path** stay in the first release only. The next
  deploy will not have them. Name the directory that holds them with `--persist`.
- **A tree that is not a git checkout** gets only the usual upload directories. Anything else
  the application writes must be named with `--persist`.

The migration refuses an application that is already on releases, one whose type cannot build
releases (monorepo, Docker Compose), and one whose directory is missing. After it, the first
`wasm update` builds a second release from the recorded source through `repo/`.

## Docker Compose and monorepo applications

Neither type builds releases, so an update rebuilds it in place. Since 2.1 the update records
what is serving before it touches anything, judges the result with the same health check as
a release (the path, statuses and timeout [configured](#configuring-the-health-check) for the
application), and puts back what was serving when the result does not pass. The update then
fails with the evidence verbatim, and its row in the deployment history is `failed`, recording
the commit that failed.

What "what was serving" means is the commit the tree was on before the update pulled, plus,
for a stack, the image each running container was created from.

### Docker Compose

1. **Record what serves.** `docker compose ps -q` lists the running containers and
   `docker inspect` reads, for each, the image it runs, the image name it was created from and
   its Compose service and project. Each image is also tagged
   `<project>-<service>:wasm-previous`, because the build is about to move the service's own
   name to a new image, and an image with no name is what `docker image prune` deletes. Each
   update moves that tag, so one extra image per service is kept on disk.
2. **Build.** A failed build recreates nothing: the containers still run the old images. The
   tree is checked out at the previous commit and every image name is pointed back at the image
   that served, so the next start of the unit (after a reboot, say) does not bring up the half of
   the stack that did build.
3. **Recreate** with `docker compose up -d --remove-orphans`.
4. **Judge.** A stack with a web port is probed like any other application, on the port the
   site proxies to. Every stack then has its containers read with `docker compose ps -a`: a
   container that keeps restarting, is dead, exited with a non-zero code or reports itself
   `unhealthy` fails the update. A container that ran once and exited 0 (a migration, a seed)
   is fine. A headless stack (no ports) is judged by its containers alone.
5. **Go back** when it does not pass: the last 40 lines of the containers' output are read
   first (`docker compose logs --tail 40`), then the tree is checked out at the previous commit,
   every image name is pointed back at the image that served (`docker image tag`), and
   `docker compose up -d --no-build --remove-orphans` recreates the containers from them, in
   the project they belonged to. Every step is attempted even when an earlier one fails, and
   each one that failed is named in the error with Docker's or git's own output. When all of
   them succeeded, the stack is judged again, and the error says whether it answers.

Volumes are never touched. Nothing on the way runs `docker compose down`, `-v`, `--volumes`,
`--renew-anon-volumes` or any `docker volume` command: recreating a container reattaches its
named volumes by name and carries its anonymous ones over. That also means a database
migration the failed version ran inside its container is not undone; the previous version
runs against the data as the new one left it.

When nothing was running before the update, there is nothing to go back to and nothing is
put back. When the tree is not a git checkout, the images go back but the compose file cannot;
the error says so.

### Monorepo

After the build, every unit the application records is restarted, all of them before any is
probed, so the stretch in which some workspaces run the new build and others the old one is
as short as it can be. Then each unit is probed on its own port with the application's health
check; a unit without a port (a worker) has to be running. One workspace that does not answer
fails the update, however many of its siblings do.

Going back checks the tree out at the previous commit and rebuilds it: `pnpm install`,
Prisma's client generated again (no migration runs: it cannot be undone, and the previous
commit's are already applied), `pnpm build`, and the tree handed back to the service user.
Then every unit is restarted and probed again. When the rebuild of the previous commit fails,
nothing is restarted on the half-built tree and the error says so, with the build's output.

Why a checkout and a rebuild rather than restoring the backup `wasm update` takes first: the
backup leaves out `node_modules`, `.git` and the build output, so restoring it needs the same
install and build anyway; restoring replaces the whole tree, losing whatever the application
wrote into it since; and the update goes on without a backup when one cannot be taken. A
checkout rewrites tracked files only and leaves `.env` files and uploads where they are.

A monorepo that is not a git checkout has no commit to go back to: the update fails, the
workspaces keep running the new build, and the error names `wasm rollback <domain>`, which
restores the backup taken before the update.

### Either type

The checkout after a failed update is detached at the previous commit, like `wasm update
--commit`; the next `wasm update` follows the branch again. Local changes to tracked files in
the tree are overwritten by the checkout, as they are by the update's own pull. A build that
fails before anything was restarted is not rolled back for a monorepo: the units keep running
the processes they had, and the tree is left at the new commit for the next update to fix.

## Deploy history

Every deploy, update, activation and migration writes a row to the deployment history with
its build log, whatever the layout, monorepo and Docker Compose included. See it with the
console's Deployments tab or `GET /api/deployments?domain=<domain>`; the last 20 per
application are kept.
