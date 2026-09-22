# Architecture, CLI, and data model review

Reviewed at commit `8dc7b7c` (after "Remove Fossil"). Scope: `src/cache22/`
(~5,300 lines of Python across 24 modules), the CLI surface, the SQLite index
schema, and the on-disk archive layout. Focus: where the design can be made
smaller without giving up the properties the project cares about (durability,
no silent data loss, safe concurrent use on one machine).

The codebase is careful and well tested (285 tests pass). Most of the
complexity below is *accumulated* rather than *wrong*: features were added
incrementally (index, queue, manager, bundles) and earlier layers were kept
alongside newer ones. The biggest wins come from collapsing those layers.

## Summary of recommendations

Ordered roughly by payoff-to-effort:

1. Merge `import repo` into `repo fetch` and remove the `import` command group.
   The two paths already execute the same code; the split is historical.
2. Remove `archive_type` from configuration, CLI, and function signatures.
   Git is the only value and there is a separate `storage_format` concept that
   actually varies.
3. Split `job_queue.py` into a pure queue (jobs, attempts, claims) and a
   separate scheduler; move the "operation fence" out of it.
4. Replace the `RepositoryStorage` / `ArchivePaths` / `repository_operation`
   trio with a single `Repository` object that owns its paths, locks, and
   storage-format dispatch. Dispatch on `storage_format` in one place.
5. Collapse the per-kind outcome/error columns in `repositories`
   (`check_*`, `fetch_*`, `convert_*`) into `job_attempts`, which already holds
   the same data.
6. Break the import cycles (`repo_service ⇄ import_service`,
   `repo_service ⇄ manager_service`, `repo_service ⇄ worker`,
   `git_bundle ⇄ archive_storage/git_observation/adoption`) by moving the
   "dispatch on storage format" logic into one module.
7. Fold `manager errors` / `manager queue` into `repo jobs` and rename the
   `manager` group to `web` (or `serve`).
8. Drop `project_name`, `display_path`, and `archive_file` as stored columns;
   derive them.

Each is expanded below.

---

## 1. Architecture

### 1.1 Layering after the Repository facade

Review step 3 is implemented. The dependency direction is now:

```
cli / web
    │
import_service / manager_service / worker
    │
repo_service / repo_audit / import_state       (inventory and job policy)
    │
storage.Repository                           (locked operations and format dispatch)
    │
git_bundle / adoption / git_mirror / git_observation
    │
archive_storage / archive_layout / git_config / git_layout / repository_ref
```

`git_bundle` uses mirror and snapshot helpers; mirror-only adoption remains
below the facade. Remote observation remains available directly to services.
Services also use index, queue, and operation fencing as before.

`repo_service` owns `fetch_locked`, `ImportResult`, and `registration_target`.
`import_service` and `manager_service` import those shared service operations;
`worker` imports job execution and owns both worker entry points. None of
these dependencies require service-to-service lazy imports.

The remaining function-local imports in production are in
`manager/app.py:create_datasette`: `Datasette`, `Database`, `pm`, and the
manager plugin name and route hook. Datasette imports follow plugin-loading
environment setup. These initialization imports are outside the storage
refactor; there is no blanket prohibition on lazy imports.

### 1.2 Two entry points for the same operation

There are two ways to fetch a repository, and they run the same code:

- `cache22 import repo URL` → `import_service.import_repository()` →
  `Queue.immediate(..., "fetch")` → `repo_service.execute_job()` →
  `import_service._import_repository()`.
- `cache22 repo fetch KEY` → `repo_cli._batch()` → `import_repository()` →
  same chain.
- Queued `fetch` job → `worker.run_continuous()` → `execute_job()` →
  `_import_repository()`.

`import_repository` is now a thin wrapper that (a) registers the repo in the
index, (b) creates an immediate job, and (c) calls `execute_job`. The only
extra thing `import repo` does that `repo fetch` cannot is the interactive
adoption prompt (`cli.py:102-120`), which is CLI presentation and could live in
`repo fetch` just as well.

`repo add --fetch` is a third spelling of the same thing
(`repo_cli.py:101-108`).

**Recommendation.** Delete the `import` group. `repo fetch URL_OR_KEY`
registers if needed (it already accepts URLs via `Index.get`) and fetches.
Move the `--adopt` prompt to `repo fetch`. Keep `repo add` as register-only.
Rename `import clean repo|all` to `repo clean [SELECTOR|--all]`. This removes
`cli.py:88-158` and the `import_repository`/`_import_repository` split; the
public function becomes `fetch_repository(index, record, *, adopt, timeout)`.

### 1.3 `archive_type` is a stub abstraction

`config.py:15-17` defines `ArchiveType = Literal["git"]`,
`SUPPORTED_ARCHIVE_TYPES = frozenset({"git"})`. It is threaded through:

- `config.toml` (`archive_type` key), `_parse_archive_type`, `set_archive_type`,
  `default_archive_type`, `normalize_archive_type`
- `cache22 config archive-type show|set` (`cli.py:31,40,75-85`)
- `import_repository(archive_type=...)`, `_import_repository(archive_type=...)`,
  `_resolve_archive_type`, `execute_job(archive_type=...)`
- ~50 lines of tests in `test_config.py`

It never influences behaviour; `_import_repository` calls
`_resolve_archive_type(archive_type)` and discards the result
(`import_service.py:84`). Meanwhile the thing that *does* vary — mirror vs
bundle — is `storage_format`, stored per repository in the index and decided
at conversion time, not by config.

Fossil was the second archive type and has just been removed. The project
is greenfield and explicitly does not preserve compatibility.

**Recommendation.** Remove `archive_type` entirely. If a non-Git source ever
arrives, it will need a different `RepositoryRef`, different storage, and a
different fetch protocol; a config string will not be the interesting part.

### 1.4 One service-facing dispatch point for storage format

**Implemented (review step 3).** `storage.Repository` is the only production
module importing `git_bundle`. Services use `observe_local`, `fetch`,
`convert_to_bundle`, `clean`, and the facade's adoption and discovery helpers.
A focused AST test enforces the bundle import boundary for both absolute and
relative imports.

`Repository.open(root, ref)` derives canonical paths for known repositories.
`Repository.open_directory(root, directory)` opens discovery candidates without
requiring a valid source URL or creating missing directories. Both wrap the
existing `repository_operation`; `RepositoryStorage` and `ArchivePaths` remain
internal building blocks. Discovery walks retain `open_archive_directory` and
release their reservations before opening each repository.

Preparation receives the same facade subsequently yielded by the context.
It runs under directory reservations, potentially before ownership-lock
publication and final validation. Existing lock ordering, symlink rejection,
and root-lock release during adoption verification are preserved.

The facade evaluates manifest presence dynamically. Its selected-format guard
rejects a missing manifest when the index expects a bundle, but accepts a disk
bundle when the index still says mirror: an interrupted inventory publication
must remain recoverable. Local observation separately classifies incomplete
or missing storage.

Bundle fetch and conversion forward a service-provided publication callback.
The sequence remains durable manifest publication, fenced inventory publication,
then source retirement. A failed callback preserves the previous copy.
Storage fetch results contain only the archive path and informational messages;
the inventory-bearing `ImportResult` stays in the service tier.

Bundle-internal manifest checks and low-level structural validation remain in
their implementations. Generation filename enumeration now belongs to
`RepositoryStorage.bundle_generations`, removing the reverse bundle dependency.
`adoption` retains mirror-only verification. Git modules have not been renamed,
and operation contexts, schema, queue policy, and CLI behavior are unchanged.

### 1.5 `operation.py` mixes three concerns

`operation.py` holds:

1. A `ContextVar`-based operation fence (`guard`, `Operation`, `current_operation`)
   used by `Queue.running` to inject claim validation and deadlines into
   arbitrarily deep Git calls.
2. `lock_fds` inheritance so child `git` processes keep flock descriptors alive
   (`inherited_lock`, `pass_fds=`).
3. A `subprocess.run` replacement (`run`, `_run_streaming`) with progress
   parsing (`git_progress`, `failure_message`, `sanitize`).

The contextvar approach lets `Index.update_in` validate the claim on every
write without threading a `job` through 30 signatures (`index.py:308-310`),
which is a reasonable trade. But it makes the control flow invisible: reading
`git_mirror.fetch_git_repository` gives no hint that it may raise
`ClaimLostError` from inside `operation.run`.

**Recommendation.** Keep the fence but make it explicit at the boundary:
`execute_job` passes an `Operation` object into `Repository`, and `Repository`
threads it to `git_local`/`bundle` as a plain argument. `run()` then takes
`operation: Operation | None` rather than reading a global. The `lock_fds`
contextvar can go away if the `Repository` object owns the fds and passes them
directly. Split `sanitize`/`failure_message`/`git_progress` into a small
`diagnostics.py`.

### 1.6 Manager process supervision

`manager_cli.run` (`manager_cli.py:146-210`, 65 lines) is a small process
supervisor: pipes for readiness, selector loop, SIGTERM fan-out, kill after
10 s. `manager/web.py` subclasses `uvicorn.Server` only to fire the readiness
fd. This exists so that `cache22 manager run` starts both web and worker.

This is worth questioning. Simpler alternatives:

- Run the worker as a thread inside the web process. The worker is already
  thread-safe (heartbeat thread, `stop` event, SQLite per-connection). Loss:
  a crash in Git handling takes down the UI; gain: 90 lines removed and one
  process to supervise.
- Or: drop the combined launcher, document `cache22 worker run --continuous`
  and `cache22 web` as two commands, and ship an example systemd unit file.
  The README already recommends `--web-only` plus an independent worker as
  the robust setup.

Either way the readiness-fd protocol (`CACHE22_READY_FD`, `notify_ready`)
disappears.

### 1.7 Datasette as the UI substrate

Pinning `datasette==1.0a40` and reaching into `datasette.views.table._table_filters`
(`manager/__init__.py:195`, a private symbol) to reuse filter parsing for
"select all filtered" is a fragility point. The value Datasette provides —
browse any table, facets, JSON export — is real, but the manager already has
its own template, JS, CSS, routes, permission action, host/origin guard, and
cell renderers. The plugin is 340 lines and the JS is 223.

Not recommending a rewrite now, but note the trade: an equivalent 3–4 route
Starlette app rendering `inventory` with `LIMIT/OFFSET` and a few `WHERE`
filters would be similar in size, would not depend on a private API or an
alpha pin, and would let `selected_ids` take a typed filter object instead of
SQL fragments (`manager_service.py:153-166` currently accepts `where: list[str]`
and defends against it with `PRAGMA query_only` and a progress-handler
deadline).

---

## 2. CLI design

### 2.1 Current surface

```
cache22 config archive add|list
cache22 config archive-type show|set
cache22 import repo URL [--case-sensitive] [--adopt]
cache22 import clean repo URL | all
cache22 repo add|list|show|check|fetch|convert|queue|unqueue|schedule|jobs|audit
cache22 worker run --once|--continuous
cache22 manager run|errors|queue
```

Six top-level groups, some with a single sub-command.

### 2.2 Overlaps and inconsistencies

| Issue | Where |
| --- | --- |
| Three ways to fetch: `import repo`, `repo fetch`, `repo add --fetch` | see §1.2 |
| Two ways to queue: `repo queue`, `repo add --queue` | `repo_cli.py:87-111, 235-239` |
| `repo queue --check` vs `repo convert --to bundle` vs implicit `fetch`: job kind is expressed three different ways | `repo_cli.py:219-239` |
| `repo jobs` and `manager queue` and `manager errors` all list jobs, with different shapes and filters | `repo_cli.py:263-267`, `manager_cli.py:121-143` |
| `--json` declared as `typer.Option(False, "--json")` in every command; `as_json` parameter name leaks | every command |
| Argument called `selector` in some commands, `url` in others, `selectors` (list) in check/fetch; `Index.get` accepts both keys and URLs but this is undocumented in `--help` | `repo_cli.py` |
| `import clean repo` takes a URL only; `repo *` take key-or-URL | `cli.py:130` |
| Two nearly identical error-wrapping decorators (`cli._user_command`, `repo_cli.command`) with different exception tuples | `cli.py:45-53`, `repo_cli.py:29-44` |
| `manager run` is a service, `manager errors|queue` are queries; same group | `manager_cli.py` |
| `worker run --once|--continuous` requires exactly one flag; a boolean default would do (`worker run` = continuous, `worker run --once`) | `repo_cli.py:285-296` |
| `--timeout` on check/fetch but `--check-timeout/--fetch-timeout/--convert-timeout` on worker | `repo_cli.py:201,213,290-292` |

### 2.3 Proposed surface

```
cache22 config root add|list                      # "archive dir" → "root" (matches archive_root column)
cache22 add       URL... [--root PATH] [--case-sensitive] [--fetch|--queue]
cache22 list      [filters] [--json]
cache22 show      SELECTOR [--json]
cache22 fetch     SELECTOR... | --all  [--adopt] [--timeout] [--json]
cache22 check     SELECTOR... | --all  [--timeout] [--json]
cache22 queue     SELECTOR... [--kind check|fetch|convert]   # default fetch
cache22 unqueue   SELECTOR...
cache22 schedule  SELECTOR... --every 6h | --off
cache22 jobs      [SELECTOR] [--state running|pending|failed|history] [--json]
cache22 audit     [--fix] [--adopt]
cache22 clean     [SELECTOR|--all]
cache22 worker    [--once] [--timeout-check N] [--timeout-fetch N]
cache22 web       [--port N]
```

Rationale:

- The application manages repositories; `repo` as a prefix on every verb adds
  nothing. Top-level verbs are conventional (`git fetch`, not `git repo fetch`).
- `queue --kind` replaces `queue --check`, `convert --to bundle`, and the
  implicit default. The `convert` verb can stay as an alias if preferred, but
  it is just a job kind.
- `jobs --state` replaces `manager queue --section` and `manager errors`. The
  "errors" view is `jobs --state failed` with the latest attempt joined in.
- `worker` and `web` are the two long-running processes; naming them as such
  is clearer than `worker run` and `manager run`.
- One `--json` option, one error decorator, one selector convention
  (key or URL, everywhere, documented in `--help`).
- `add` accepts multiple URLs. `register_batch` already exists for the web UI;
  the CLI should get it too (it is the natural way to paste a list).

### 2.4 Output formatting

`repo_cli.output()` (`repo_cli.py:57-84`) hardcodes a seven-column tab layout
when it sees a dict with `repo_key`, otherwise dumps JSON lines, otherwise
`key: value`. `manager_cli._report_snapshot` has a separate 60-line
pretty-printer. `_json_value` exists in both `repo_cli` and `manager_service`
(`json_value`) with identical bodies.

**Recommendation.** One `render(rows, *, columns, json)` helper. Let the
caller name columns. Delete the duplicate timestamp converter. Consider
`--format table|json|tsv`.

---

## 3. Data model

### 3.1 On-disk layout

```
ROOT/host/namespace/.../name/
  .lock                      # flock target, contains "cache22-storage-v1\n"
  source.json                # {"source_path": "host/ns/name"}
  .clone-complete            # "complete\n"   (mirror only)
  name.git/                  # bare mirror     (mirror only)
  bundle.json                # manifest         (bundle only)
  name.<uuid32>.bundle       # one generation   (bundle only)
  .cache22-bundle/           # staging          (transient)
```

This is a sensible layout. Observations:

- `.lock` as both mutex *and* ownership marker is elegant; the signature check
  distinguishes Cache22 containers from arbitrary directories.
- `source.json` and the `.lock` signature carry overlapping intent (ownership
  + identity). `bundle.json` also carries `source_url`. Three files answer
  "whose is this and where did it come from". A single `cache22.json`
  containing `{version, source_path, source_url, storage_format, ...}` next to
  `.lock` would replace `source.json`, `.clone-complete`, and `bundle.json`.
  `.clone-complete` becomes `"state": "complete"` in that file (written
  atomically via rename as today). The bundle manifest fields move in
  unchanged.
- Bundle generations with a UUID name plus a manifest pointer is the right
  pattern for atomic replacement. Keep it.
- Requirements.md says "avoid many small files"; mirrors are the default and
  bundles are opt-in via conversion. If bundles are the intended end state
  (per `requirements.md`), consider making `storage_format` a per-root or
  global default so that new repositories go straight to bundle without a
  conversion job.

### 3.2 Index schema: `repositories`

**Implemented (review step 4, schema version 3).** The 18 per-kind attempt,
success, outcome, and error columns have been removed. `repositories` now has
22 columns for identity, storage, timestamps, and local/remote observations.
Execution outcomes are written only through queue attempt finalization and
interruption handling; services no longer duplicate those writes in inventory.

`inventory` derives `last_checked_at`, `last_fetched_at`, and `last_converted_at`
from the maximum completion timestamp of successful attempts of each kind.
These describe whole job outcomes. The optional remote probe after a fetch does
not count as a check job, and its tolerated transport failure creates no separate
check diagnostic. Failed or interrupted jobs cannot advance a success timestamp.

`job_attempts.kind` captures the job kind at claim time, so promoting a pending
check retry to fetch cannot relabel earlier attempts. Historical detail and
error reporting use this immutable kind; queue displays retain current job kind.

The shared `job_errors` view selects the latest completed failed or interrupted
attempt per pending, running, or failed job. Both manager errors and inventory
use it. Inventory exposes `has_error` plus `last_error`, `last_error_kind`,
`last_error_category`, and `last_error_at`, selecting one problem by completion
time descending and attempt ID descending. A running retry retains its previous
diagnostic; success or cancellation clears that job's diagnostic. A separate
successful job does not hide an older failed job.

Correlated inventory queries use `jobs_repository_history`, `attempts_job`, and
a partial `attempts_success(job_id,kind,finished_at)` index. There is no cached
copy of the removed outcome fields. History cleanup retains the latest successful
job per repository and kind, including its attempts, beyond 30 days; superseded
successes and other terminal jobs expire normally. This preserves timestamps
without retaining all history.

Fresh indexes use `user_version=3`; other versions are rejected without migration
or automatic deletion. Rebuilding through offline audit restores archive identity
and observations, but cannot restore schedules or execution history. Queue and
scheduler separation remains step 5; `jobs.error*`, progress storage, and local
observation state are unchanged.

The remaining derived columns (`project_name`, `display_path`, and `archive_file`)
are still candidates for removal as recommended in the summary. Their storage
and derivation have not changed in this step.

### 3.3 `reconciliation_required` and `local_state`

`local_state ∈ {unknown, absent, ready, missing, incomplete}` plus a boolean
`reconciliation_required`. The `inventory` view treats
`reconciliation_required=1` as `remote_status='unknown'`. The distinction
between `absent` (never had a mirror) and `missing` (had one, now gone) is
carried via `previously_ready=record["local_state"] in {"ready","missing"}`
at three call sites.

**Recommendation.** Fold `reconciliation_required` into `local_state` as a
sixth value `stale`, or make `local_state` nullable and treat NULL as
"needs observation". One column, one state machine. Compute `previously_ready`
inside `local_fields` from the record, not at every call site.

### 3.4 `jobs` and `schedules`

```sql
jobs(id, repository_id, kind, origin, state, due_at, created_at, finished_at,
     retry_count, claim_token, lease_until, error_category, error)
job_attempts(id, job_id, kind, started_at, finished_at, outcome, error_category, error)
attempt_progress(attempt_id PK, phase, observed_at, completed, total, unit, percentage, detail)
schedules(repository_id PK, enabled, blocked, interval_seconds, next_due_at)
workers(id, pid, started_at, heartbeat_at, stopped_at, current_job_id)
```

This is well designed. Small points:

- `jobs.error_category`/`jobs.error` duplicate the latest `job_attempts` row.
  Drop them; `error_snapshot` already joins the latest attempt.
- `attempt_progress` is 1:1 with `job_attempts`. Merge the seven columns into
  `job_attempts` (they're nullable anyway). Saves a table and an upsert.
- `schedules.enabled` + `schedules.blocked` + `jobs.origin='scheduled'`: three
  places encode "is automatic updating active". `origin` is needed for
  priority ordering and cancellation-on-disable; `enabled`/`blocked` could be
  a single `state ∈ {on, off, blocked}` or `enabled` could be `next_due_at IS NOT NULL`.
- `workers` table: needed only for the UI "worker available" indicator. Fine,
  but consider whether `jobs.lease_until` on the running job already conveys
  liveness; the UI could show "N running jobs with live leases" instead.

### 3.5 The `Queue` class does too much

`job_queue.py` (373 lines) is: enqueue with coalescing rules
(`enqueue_in`), cancel, schedule/unschedule, `materialize()` (scheduler tick
+ 30-day GC), lease recovery, claim, heartbeat thread, progress publication
with rate limiting, the `Operation` fence installer (`running()`), retry
policy + schedule-advance + follow-up-fetch (`finish()`), and a special
"immediate" path for synchronous CLI use.

Suggested split:

- `queue.py`: `enqueue`, `cancel`, `claim`, `heartbeat`, `finish(outcome)`.
  Pure CRUD on `jobs`/`job_attempts`. No knowledge of schedules or retries.
- `scheduler.py`: `tick()` — materialise due schedules, apply retry policy,
  advance `next_due_at`, block on structural error, enqueue follow-up fetch.
  Runs after `finish`. Knows the policy tables (`RETRIES`, 60 s busy defer).
- `operation.py` (or `Repository`): the fence and progress. `Queue.running()`
  becomes `with Operation(job, queue, timeout) as op:`.

`immediate()` (`job_queue.py:322-353`) is a claimed-on-insert job. It could
be `enqueue(...); claim(job_id)` with a `prefer` flag; the "refuse if convert
pending" rule is really a scheduler/policy rule.

### 3.6 Coalescing rules in `enqueue_in`

`enqueue_in` (`job_queue.py:29-62`) merges a new request into an existing
pending job: kind promotes `check→fetch`, origin promotes `scheduled→manual`,
`due_at` moves earlier, and `convert` never merges with non-convert. These
rules are correct but encoded in nested conditionals. A small table would be
clearer:

```python
MERGE = {("check","fetch"): "fetch", ("fetch","check"): "fetch", ...}
```

or simply: "at most one pending non-convert job per repo, kind = max(kinds)".

---

## 4. Smaller items

- `Index.__init__` does schema creation on every instantiation, including a
  `BEGIN IMMEDIATE`. `Index()` is constructed in many places per command
  (`repo_cli` constructs one per command, `import_state._clean_repository_storage`
  constructs another per repository). Construct once in the CLI callback and
  pass it down; move `ensure_schema()` to an explicit call.
- `Index.update_in` runs `PRAGMA table_info(repositories)` on every update to
  validate field names (`index.py:311`). Cache the column set at class level,
  or accept a typed dataclass.
- `Index.get(selector)` heuristically decides whether the string is a URL
  (`index.py:233-236`). Make the CLI parse `SELECTOR` into a key up front.
- Exception tuples `(OSError, RuntimeError, ValueError, subprocess.SubprocessError[, sqlite3.Error])`
  appear in ten places with slight variations. Define
  `class Cache22Error(Exception)` and subclass `TransportError`,
  `RepositoryBusyError`, `StructuralError`, `ClaimLostError` from it; wrap
  Git/OS failures at the `git_local`/`bundle` boundary. `category_for`
  (`repo_service.py:30-41`) then becomes an attribute on the exception.
- `config.py` (212 lines) is mostly TOML validation for a two-key file with a
  directory-inode flock. After removing `archive_type` it is a list of paths.
  `tomllib` + `tomli_w` + `dataclass` + `frozenset` + `Literal` + `cast` for
  that is a lot. Consider `roots.txt`, one path per line, or keep TOML and
  shrink validation to "each entry is an absolute path".
- `repository_ref.py` builds `clone_url` by reconstructing scheme/credentials/
  port/path (`repository_ref.py:49-64`). Storing the user's original URL
  (after control-char rejection) and deriving `repo_key` from it would be
  simpler; the normalised form is only needed for the key.
- `ImportResult.info_messages` carries `"INFO: updated Git mirror: ..."`
  strings from `git_mirror` back to `cli` for printing. Return a structured
  result (`adopted: bool`, `updated: bool`) and let the CLI phrase it.
- `docs/use.md` is 312 lines of dense behavioural guarantees. After the CLI
  consolidation, a short "Commands" reference plus a separate
  "Guarantees and edge cases" page would read better than one long file.
- `utils/editor/` (a browser YAML editor with its own `package.json`) and
  `docs/related-works.*` are unrelated to the Python package. Consider a
  separate repository, or at least a top-level `experiments/` to make the
  main tree clearer.

---

## 5. What not to simplify

Things that look like over-engineering but are earning their keep:

- **Directory-fd locking with `O_NOFOLLOW`, ancestor shared locks, and the
  `.lock` signature.** This is what makes concurrent CLI + worker + web safe
  and rejects symlink tricks. Keep, but hide it inside `Repository`.
- **Lease-based claims with heartbeat and `ClaimLostError` fencing on index
  writes.** Correct for a multi-process SQLite queue. Keep.
- **`--atomic --prune` fetch, then HEAD sync as a separate step with
  before/after `ls-remote` comparison.** Necessary for correct mirroring of
  default-branch renames and detached HEADs. Keep.
- **Bundle generation + manifest + independent restore-and-fsck before
  retiring the old copy.** Expensive but this is the "never lose data" path.
  Keep.
- **`git_config.validate_git_mirror_config` allow-list.** Protects against
  running `git` in an adopted repo with hostile config. Keep.
- **Sanitising credentials out of persisted error strings.** Keep.

---

## 6. Suggested sequence

1. ~~Remove `archive_type` (config, CLI, signatures, tests).~~ Done.
2. ~~Merge `import repo` into `repo fetch`; `import clean` → `repo clean`.~~ Done.
3. ~~Introduce the `Repository` facade and centralize service-level storage
   dispatch.~~ Done. Locking remains internal, discovery opens by directory,
   and service import cycles are removed. Bundle-internal checks and manager
   initialization imports remain intentionally.
4. ~~Drop the 18 per-kind outcome columns; derive inventory status from
   attempts and bump `user_version` to 3.~~ Done. Latest successful jobs survive
   history expiry; manager and inventory share job-scoped error semantics.
5. Split `Queue` into queue + scheduler.
6. CLI flattening (`repo` prefix removal, `jobs` replaces `manager
   errors|queue`, `web`/`worker`). This is the most user-visible change; do
   it last so the docs are rewritten once.
7. Decide on the process model for `web` + `worker` (thread vs two commands)
   and delete the supervisor.

Steps 1–2 are safe warm-ups. Step 3 is the one that most improves the code's
readability. Step 4 is the biggest schema change. Steps 5–7 are independent
of each other.
