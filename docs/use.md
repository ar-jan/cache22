# Usage

## Start here

```sh
cache22 config root add /absolute/path/to/archive
cache22 fetch https://github.com/ar-jan/cache22.git
cache22 list
```

Unknown fetch URLs are registered in the first configured root. Indexed repositories
use their stored root and source URL. To choose a different root before fetching,
use `add URL --root PATH`. The root must already exist. `add` only registers;
it neither fetches nor queues work. Registration accepts multiple URLs.

Commands that modify inventory, including `audit --fix` and `audit --adopt`,
initialize the index when needed. Web and worker startup also initialize it.
`list`, `show`, `jobs`, and `audit` without repair open an existing index read-only
and fail with exit code 1 if it is missing. Register a repository with `add` or
`fetch`, or use `audit --fix` to rebuild inventory from managed archives. Help
and configuration commands do not access the index.

## Commands

```text
cache22 config root add PATH
cache22 config root list
cache22 add URL... [--root PATH] [--case-sensitive] [--json]
cache22 list [--host HOST] [--local-state STATE] [--remote-status STATUS]
             [--queued] [--scheduled] [--sort FIELD] [--descending]
             [--limit N] [--offset N] [--json]
cache22 show SELECTOR [--json]
cache22 fetch [SELECTOR...] [--all] [--case-sensitive] [--adopt]
              [--timeout SECONDS] [--json]
cache22 check [SELECTOR...] [--all] [--timeout SECONDS] [--json]
cache22 clean [SELECTOR...] [--all]
cache22 queue SELECTOR... [--kind check|fetch|convert] [--json]
cache22 unqueue SELECTOR... [--json]
cache22 schedule SELECTOR... (--every DURATION | --off) [--json]
cache22 jobs [SELECTOR]
             [--state all|running|pending|runnable|deferred|failed|history]
             [--db PATH] [--limit N] [--offset N] [--json]
cache22 audit [--fix] [--adopt] [--json]
cache22 worker [--once] [--timeout-check SECONDS]
               [--timeout-fetch SECONDS] [--timeout-convert SECONDS] [--json]
cache22 web [--port PORT]
```

A `SELECTOR` is an exact canonical key (such as `github.com/ar-jan/cache22`)
or a supported Git URL, never a partial name. Check, fetch, and clean require
selectors or `--all`, exclusively. Queue, unqueue, and schedule require explicit
selectors; jobs optionally filters one repository. Fetch and clean accept unknown
URLs; other selector commands require indexed repositories.

Check/fetch and batch add/queue/unqueue/schedule continue after individual failures
and exit 1 if any item fails. Add reports each input, including duplicates; queue,
unqueue, and schedule act once per resolved repository in input order. Command-wide
option errors are rejected before work starts. Cleanup retains its existing
stop-on-error behavior and never removes completed archives.

`--json` emits a single JSON value with UTC ISO 8601 timestamps and nulls for
unknown values. Batch registration and mutation commands return ordered result
arrays with selector, repository identity, status, job ID where relevant, and
errors. Continuous worker output is one JSON result per line. Exit codes are 0
for success, 1 for operational failures/audit discrepancies, and 2 for usage errors.
Use `cache22 COMMAND --help` for defaults and argument descriptions.

List sorting supports `repo_key` (default), `project_name`,
`local_head_committed_at`, `last_checked_at`, `last_fetched_at`, `last_converted_at`,
and `remote_status`.
Its text columns are key, local state, remote status, local HEAD commit date,
last successful check, storage format, and active archive path.

## Queue and schedules

```sh
cache22 add https://host/team/one https://host/team/two
cache22 queue host/team/one host/team/two
cache22 queue host/team/one --kind check
cache22 schedule host/team/one host/team/two --every 6h
cache22 worker --once
cache22 schedule host/team/one --off
cache22 unqueue host/team/two
```

Queue defaults to fetch. Repeated requests coalesce under the existing ordering
rules. `unqueue` cancels pending work without changing recurring policy. `--off`
disables recurring checks and their pending automatic work, preserving manual
jobs. Durations are positive integers with `s`, `m`, `h`, `d`, or `w` suffixes.

Offline conversion uses `queue SELECTOR --kind convert`. It returns a job ID and
requires a worker. Conversion does not fetch; it preserves ordering barriers and
cannot be overtaken by immediate operations. Mirrors remain the default.

## Job inspection

```sh
cache22 jobs
cache22 jobs host/team/one --state failed
cache22 jobs --state deferred --limit 50
cache22 jobs --state history --limit 50 --offset 50
cache22 jobs --db /path/to/index.sqlite3 --json
```

| State view | Contents |
| --- | --- |
| `all` (default) | All retained jobs, newest job ID first |
| `running` | Running jobs, including expired claims awaiting recovery |
| `pending` | All pending jobs |
| `runnable` | Due pending jobs with no blocking predecessor |
| `deferred` | Pending jobs waiting for their due time or a predecessor |
| `failed` | Problem jobs with a latest completed failed/interrupted attempt, including active retries |
| `history` | Succeeded, failed, and cancelled jobs, newest completion first |

Failed diagnostics are ordered by completion time and attempt ID, newest first.
Active views use manual priority, due time, and job ID. Success or cancellation
removes a job from the failed view; a different successful job does not hide an
older failure. The diagnostic's kind describes the completed attempt, even when
its job has since been promoted from check to fetch.

Inspection reads an existing index without initializing it, starting workers,
recovering claims, scanning archives, or contacting Git. Missing and unsupported
databases are errors. Default limit is 100 (range 1–500); offsets are nonnegative.
Errors in jobs and unavailable workers do not make inspection exit unsuccessfully.

JSON contains `database`, `observed_at`, `state`, `counts`, `workers`, `jobs`, and
`next_offset` (null on the last page). Counts cover all views within the selected
repository scope and overlap; workers are global. Each job includes its current
attempt/progress, blocking predecessor, full retained `attempts`, and a separate
`diagnostic` for its latest completed problem, or null. During a retry these may
refer to different attempts. Errors are stored on attempts only: job objects have
no top-level `error` or `error_category` fields. Read those fields from `diagnostic`
for the current problem, or from individual `attempts` for history. Text output
includes full diagnostics and a next-page hint.

## Inventory queries

CLI listing and the browser share one inventory query model. Search names and keys
with `cache22 list --q TEXT`; this is literal substring matching using SQLite's
`lower()` (ASCII case-insensitive, not Unicode case folding). `%` and `_` are literal.
Repeat `--host`, `--archive-root`, `--local-state`, `--remote-status`, or
`--storage-format` for alternatives within a field. Different fields combine with AND.

Boolean filters accept positive and negative flags: `--queued/--no-queued`,
`--running/--no-running`, `--scheduled/--no-scheduled`,
`--schedule-blocked/--no-schedule-blocked`, `--has-error/--no-has-error`, and
`--reconciliation-required/--no-reconciliation-required`. Omission means either value.

```sh
cache22 list --host github.com --host gitlab.com --no-queued --has-error --json
cache22 list --q project --storage-format bundle --sort last_fetched_at
```

Sort field names use underscores (for example `--sort last_fetched_at`). Supported
fields are `repo_key`, `project_name`, `host`, `archive_root`, `local_state`,
`remote_status`, `storage_format`, `local_head_committed_at`, `last_checked_at`,
`last_fetched_at`, `last_converted_at`, and `next_due_at`. Use `--descending` to
reverse the primary sort; ID breaks ties. SQLite text/NULL ordering applies.
`--limit` accepts 1–500 (default 100); `--offset` is nonnegative.

Browser query parameters use the same underscore field names, repeated categorical
parameters, and `true`/`false` booleans. The inventory lives at `/`; old Datasette
routes and parameters are unsupported. Filter changes return to the first page.
Host/local-state/remote-status facets count matches under all other filters.

Select this page adds to the tab's captured IDs; Select all filtered replaces the
selection with every match, up to 10,000. Filtering, pagination, and polling keep
that selection. Commands use captured IDs even if repository states change later.
JSON/CSV downloads export all matches in the chosen sort order, ignoring pagination.
They include details-level inventory metadata except `source_url`, with UTC ISO
timestamps, JSON booleans/nulls, and CSV true/false/blank values.

## Web and worker

In one terminal:

```sh
cache22 web
# Open http://127.0.0.1:8001/
```

In another:

```sh
cache22 worker
```

The web server only serves the browser manager; it never starts a worker. It binds
to loopback and accepts `--port` (default 8001). The browser can register repositories
and queue jobs while workers are stopped. Each process can restart independently;
closing the browser does not stop jobs.

Worker runs continuously by default. `--once` drains currently runnable work,
including follow-up fetches, and exits; deferred retries wait for another run.
Check/fetch/convert timeouts default to 120/7200/7200 seconds. Use `--timeout` for
immediate check/fetch, or the per-kind worker options shown above.

For unattended operation, use separate user services with these service sections
and `[Install] WantedBy=default.target`:

```ini
# cache22-web.service
[Service]
ExecStart=/absolute/path/to/cache22/.venv/bin/cache22 web
Restart=on-failure
TimeoutStopSec=20
```

```ini
# cache22-worker.service
[Service]
ExecStart=/absolute/path/to/cache22/.venv/bin/cache22 worker
Restart=on-failure
TimeoutStopSec=20
```

Both services must use the same configuration/data environment. Alternatively,
invoke `cache22 worker --once` from cron or a user timer. Cache22 installs no
services or timers automatically.

See [Guarantees and edge cases](guarantees.md) for adoption, locking, Git
configuration, bundle publication, inventory observations, recovery, and retention.
