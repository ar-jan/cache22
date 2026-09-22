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

### 1.1 Layering as it exists

```
cli.py ─┬─ repo_cli.py ────┐
        ├─ manager_cli.py ─┤
        └─ (import/config) ┘
                 │
     import_service ⇄ repo_service ⇄ manager_service ⇄ worker
                 │            │
        adoption / git_mirror / git_bundle / git_observation / repo_audit / import_state
                 │
        archive_storage ── archive_layout ── repository_ref
                 │
             operation (contextvar-based deadline/fence/progress)
                 │
             index ── job_queue
```

The intended layering (CLI → service → storage → git) is sound, but the
service tier has become a set of mutually dependent modules. Twelve imports are
deferred into function bodies to avoid cycles (`grep -n "^\s\{4,\}from \."`):

| Module | Lazy imports | Why |
| --- | --- | --- |
| `repo_service` | `import_service._import_repository`, `manager_service.registration_target`, `worker.run_continuous`, `git_bundle.materialize`, `config.normalize_archive_type` | Both directions of `repo_service ⇄ import_service`, `⇄ manager_service`, `⇄ worker` |
| `archive_storage`, `git_observation`, `adoption`, `import_state`, `repo_audit`, `import_service` | `git_bundle.*` | `git_bundle` imports `archive_storage`; everyone else needs bundle helpers to check `bundle_manifest` |

Lazy imports are a symptom, not a design. Recommendation: see §1.4.

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

### 1.4 The service tier should have one dispatch point for storage format

The check "is this a bundle or a mirror?" (`storage.entry(paths.bundle_manifest.name) is not None`)
appears 13 times across 7 modules. Each caller then branches to
`git_bundle.*` or mirror code. Because `git_bundle` imports `archive_storage`,
`git_observation`, `git_mirror`, etc., and those modules need `git_bundle`
back, every one of them defers the import.

Proposed shape:

```
repository_ref   (pure: parse URL → RepositoryRef)
archive_paths    (pure: RepositoryRef + root → paths)
locking          (open_archive_directory, root lock, owned lock; today's archive_storage minus RepositoryStorage)
git_local        (git_layout + git_config + mirror_snapshot + fetch_git_repository + ensure_git_mirror)
git_remote       (remote_snapshot, _remote_head)
bundle           (read/write/restore/verify bundle; depends on git_local only)
storage          (class Repository: format-aware facade; imports git_local, bundle, locking)
index, queue, scheduler
services         (fetch, check, convert, clean, audit — thin; imports storage, index, queue)
cli / web
```

`storage.Repository` becomes the only module that knows both `git_local` and
`bundle` and does the `if bundled:` dispatch. Everything above it calls
`repo.observe_local()`, `repo.fetch(url)`, `repo.convert_to_bundle()`,
`repo.clean()`. Everything below it never imports upward.

This eliminates all twelve lazy imports and the `RepositoryStorage` /
`ArchivePaths` / `repository_operation` triple that callers currently have to
assemble by hand (`root`, `paths = archive_paths_for_repository(root, ref)`,
`with repository_operation(root, paths, ...) as storage`), which is repeated
in `repo_service.check_locked`, `repo_service.convert_locked`,
`import_service._import_repository`, `import_state.clean_repository_import_state`,
`repo_audit.audit` (twice).

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

Forty columns. Grouped:

| Group | Columns | Notes |
| --- | --- | --- |
| Identity | `id`, `repo_key`, `host`, `source_url`, `source_path`, `archive_root` | Keep. |
| Derived identity | `project_name`, `display_path` | `project_name = repo_key.rsplit('/',1)[1]`; `display_path` is only used to derive `project_name` and is never read back. Drop both, or keep `display_path` (original casing) and drop `project_name`. |
| Storage | `storage_format`, `archive_file` | `archive_file` is always `name.git` or the active bundle generation; the latter is also in `bundle.json`. Could be derived from `storage_format` + a `bundle_generation` column, or kept — it's cheap. |
| Timestamps | `created_at`, `updated_at` | Keep. |
| Local observation | `local_state`, `local_observed_at`, `reconciliation_required`, `local_head_ref`, `local_head_oid`, `local_head_committed_at`, `local_ref_digest` | Keep; this is the core value. |
| Remote observation | `remote_head_ref`, `remote_head_oid`, `remote_ref_digest` | Keep. |
| Per-kind outcome (×3) | `last_{check,fetch,convert}_attempt_at`, `last_{checked,fetched,converted}_at`, `{check,fetch,convert}_outcome`, `{check,fetch,convert}_error_category`, `{check,fetch,convert}_error`, `{check,fetch,convert}_error_at` | **18 columns** duplicating `jobs` + `job_attempts`. |

The 18 per-kind columns exist so `inventory` can show "last error" without a
join, and so direct (non-queued) operations record outcomes. But every direct
operation now *does* create a job via `Queue.immediate()`, so `job_attempts`
has the same rows. `execute_job` writes each outcome twice
(`repo_service.py:144,149-156,182-192` and `queue.finish`).

**Recommendation.** Drop the 18 columns. Add to `inventory`:

```sql
(SELECT max(finished_at) FROM job_attempts a JOIN jobs j ON j.id=a.job_id
 WHERE j.repository_id=r.id AND j.kind='fetch' AND a.outcome='succeeded') AS last_fetched_at,
(SELECT ... outcome='failed' ... ORDER BY a.id DESC LIMIT 1) AS last_error
```

or a small `repository_status` view. With the `jobs_repository_history` index
this is cheap at 1–100k rows. `has_error` becomes "latest attempt for any
unfinished-or-failed job failed", which is what `manager errors` already
computes (`manager_service.py:210-220`).

If the extra join is judged too costly for the list view, keep *one* pair —
`last_fetched_at`, `last_checked_at` — and drop the other 16.

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
job_attempts(id, job_id, started_at, finished_at, outcome, error_category, error)
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

1. Remove `archive_type` (config, CLI, signatures, tests). Mechanical; no
   behaviour change.
2. Merge `import repo` into `repo fetch`; `import clean` → `repo clean`.
   Update `use.md`.
3. Introduce `Repository` facade in one module; move the 13
   `bundle_manifest` checks into it. Remove lazy imports as they become
   unnecessary.
4. Drop the 18 per-kind outcome columns; add `inventory` subqueries. Bump
   `user_version` to 3 (the README already says older indexes are discarded).
5. Split `Queue` into queue + scheduler.
6. CLI flattening (`repo` prefix removal, `jobs` replaces `manager
   errors|queue`, `web`/`worker`). This is the most user-visible change; do
   it last so the docs are rewritten once.
7. Decide on the process model for `web` + `worker` (thread vs two commands)
   and delete the supervisor.

Steps 1–2 are safe warm-ups. Step 3 is the one that most improves the code's
readability. Step 4 is the biggest schema change. Steps 5–7 are independent
of each other.
