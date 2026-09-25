# Guarantees and edge cases

See [Usage](use.md) for the command reference and startup examples.

## Archiving

```sh
# Configure where archives are stored
cache22 config root add /absolute/path/to/archive

# Fetch a repository (remote paths are lowercased by default)
cache22 fetch https://github.com/ar-jan/cache22.git
```

A clone URL that is not yet indexed is registered and fetched into the first
configured archive root. An indexed repository can be fetched again by URL or by
its key, for example `github.com/ar-jan/cache22`; the stored source URL and
archive root are used. Repeating a fetch updates the existing mirror.
Updates include new refs, forced changes, and pruning branches and tags deleted
upstream. The mirror's HEAD follows the advertised default branch, including
renames, or the advertised detached commit. A failed fetch keeps the initialized
mirror for retry. Ref updates are atomic; updating HEAD is a subsequent step.
Before publishing HEAD, Cache22 checks that its remote target and object ID stayed
the same across the fetch and that the fetched target matches. If this check or
HEAD publication fails, the command reports an incomplete update and retains the
fetched refs without publishing HEAD. Retry the fetch to complete it; Cache22
does not retry automatically.

Every existing-mirror update rechecks the storage layout before running Git.
Symlinks, redirected storage, and shallow or partial-clone state are rejected
without modifying the mirror. These checks inspect filesystem entries; full Git
object verification runs only during explicit adoption.

### Standalone Git bundles

Mirrors remain the default. To store an existing managed repository as a single
self-contained Git bundle, queue an offline conversion:

```sh
cache22 queue github.com/ar-jan/cache22 --kind convert
cache22 worker --once
```

The command returns a job ID; it does not wait for conversion. The browser manager
also offers **Convert to bundle** on repository and bulk actions. Conversion runs
after earlier work for that repository, so it can follow an initial queued fetch.
An already bundled repository is verified without rewriting its bundle.

After conversion, `check`, `fetch`, and scheduled updates continue to
work. Checks compare bundle refs and saved HEAD metadata with the
remote without restoring objects. Fetches restore temporary bare Git storage,
fetch incrementally, and publish a newly verified standalone bundle. Network
transfers are incremental, but local storage is restored and the bundle rewritten.
Allow space for the old bundle, working repository, replacement, and independent
verification copy. No persistent mirror remains after successful cleanup.

Inventory reports `storage_format` and `archive_path`. The container retains
`.lock`, `source.json`, `bundle.json`, and one `project.<uuid>.bundle` generation.
The manifest preserves the original source URL, exact HEAD state, and commit date.
Keep it with the bundle: cloning a bundle alone cannot reliably recover the
original symbolic HEAD target. For manual restoration, clone the selected bundle
with `git clone --mirror FILE DEST`, set origin to the manifest's `source_url`,
then set HEAD with `git symbolic-ref HEAD REF` or
`git update-ref --no-deref HEAD OID` for a detached HEAD.

Publication is atomic. A failed fetch or verification preserves the selected
archive. Interrupted cleanup may leave retired generations, a retained mirror,
or `.cache22-bundle` staging; `clean SELECTOR` cleans recognized state and
verifies the selected bundle before retiring old archives. `audit --fix`
rebuilds bundle inventory offline. Invalid active metadata preserves data for
inspection; restore a missing manifest rather than attempting adoption.

Empty mirrors cannot be converted. If an update becomes empty, it fails while
preserving the previous bundle. Only history reachable from current refs and HEAD
is retained; removed or force-pushed history can disappear on the next rewrite.
Conversion back to a mirror and external bundle adoption are not supported.

Conversion has its own diagnostics and does not advance fetch/check timestamps
or change the update schedule. The worker's `--timeout-convert` defaults to 7200
seconds. Queue and progress views show conversion alongside checks and fetches.
Synchronous operations report busy rather than overtake pending conversion.

### Adopting an existing mirror

If a mirror already exists at the expected path, for example
`ARCHIVE/github.com/karpathy/llm.c/llm.c.git`, initialize it with:

```sh
cache22 fetch https://github.com/karpathy/llm.c.git --adopt
```

Cache22 verifies the origin identity, bare mirror configuration, and full Git
object integrity before writing missing metadata and fetching updates. It never
reclones or deletes the supplied mirror on failure. Later fetches need no flag.
Verification can take time for large mirrors.

An ordinary fetch encountering an eligible uninitialized directory offers:

```text
Verify and adopt the existing Git mirror, then fetch updates? [y/N]:
```

The default is No. Acceptance performs the same verification as `--adopt`.
The prompt appears on stderr only when both stdin and stderr are terminals;
scripts and redirected sessions must supply `--adopt` explicitly. Locks are
released while waiting for an answer, and the same target is rechecked afterward.

Adoption supports the current container layout only. Working checkouts, shallow
or partial clones, external object alternates, symlinked storage, unrelated files,
unfinished staging state, and conflicting or malformed metadata are rejected.
HTTPS and SSH origins are equivalent, but origin path casing must match the
effective requested source; use `--case-sensitive` when appropriate.

### Repository Git configuration

Before adoption verification or an existing-mirror update, Cache22 checks the
mirror's local configuration without following include directives. Unsupported
settings and worktree configuration are rejected without rewriting them.
Use user-level Git configuration for authentication and transport customization;
normal SSH and credential settings from that trusted configuration remain available.
Local adoption integrity checks disable system/global Git configuration and
counted environment overrides, replacement objects, and lazy fetching. Network
operations retain trusted transport settings.

Allowed local settings are:

- `core.repositoryformatversion`, `core.filemode`, `core.bare`,
  `core.logallrefupdates`, `core.ignorecase`, `core.precomposeunicode`, and `core.symlinks`
- `extensions.objectformat` (`sha1` or `sha256`)
- `remote.origin.url`, `remote.origin.fetch` (`+refs/*:refs/*`), and `remote.origin.mirror`
- `remote.origin.tagOpt` (`--tags` or `--no-tags`; Git 2.55 mirror clones emit the latter)

The origin must match the requested source and describe a full bare mirror.
Local includes, additional remotes, SSH commands, credential helpers, and custom
hook paths are not accepted. Repository hooks are disabled during verification,
fetching, and HEAD synchronization; fetching does not run automatic maintenance.

### Case-sensitive sources

Use `cache22 fetch https://host/Team/Repo --case-sensitive` (or `add
--case-sensitive`) to preserve remote path casing on a case-sensitive server when
registering a new URL. Local identity stays lowercase. Each local path binds to one
source; conflicting casing is rejected before reuse or Git work. Failed fetches
release that binding once cleanup leaves no archive or partial state.

### Clean-up

If an interrupted fetch leaves a complete Git mirror without Cache22 metadata,
try `--adopt` to verify and retain it. To discard incomplete import state instead:

```sh
# Clean one or more repositories by key or URL
cache22 clean github.com/ar-jan/cache22

# Clean all configured archive directories
cache22 clean --all
```

## Details

The entire configured archive directory is managed by Cache22.
For `https://Git.Example.ORG/Team/Project.git`, files are stored directly under `ARCHIVE/git.example.org/team/project/`, including `project.git`, the `.clone-complete` marker, and the persistent `.lock` file.
Repository directories are terminal containers. For example, fetching both
`host/team/project` and `host/team/project/child` into the same archive is rejected
in either order, including simultaneous fetches. Subgroup namespaces and sibling
repositories remain supported.
Previous layouts are unsupported and are not migrated.

Fetches and cleanup report a repository-busy error on repository or namespace
reservation contention. Acquiring the brief archive-root lock can wait.
Verification reserves the candidate directory and its namespace ancestors using
inode locks. It creates no reservation files and does not hold the archive-wide
root lock during the integrity check. Sibling fetches and targeted cleanup can
continue while verification runs; conflicting reservations fail immediately.
Reservations release on process exit, including interrupted adoption before any
Cache22 metadata has been published.
Cleanup keeps completed archives and lock files.
It only cleans recognized Cache22 storage; unowned mirrors are left untouched.
A completion marker must contain exactly `complete\n` for fetches to reuse the
archive. A malformed marker causes a fetch error without changing the mirror.
Cleanup preserves a mirror beside such a marker, but can remove an orphan marker
when no mirror exists.
The retained lock keeps the path reserved as a repository even after its archive data is removed.
`clean --all` visits subgroup namespaces, stops traversal at repository boundaries,
skips directory symlinks, and stops on a busy repository without rolling back earlier cleanup.
Targeted operations reject symlinked storage paths.
Missing archive roots are errors and must be restored before fetching or cleaning.

Fetching a new URL uses the first configured archive root; `add --root`
chooses another root before the first fetch. Cleanup by selector searches all
configured roots.

Repository inputs reject ASCII control characters and DEL before normalization,
and reject literal `?` and `#` in HTTPS, SSH, and scp-style forms. Surrounding
ordinary spaces are trimmed.

Configuration writers lock the configuration-directory inode across loading,
changing, and atomically replacing settings, so concurrent commands preserve
each other's changes. Readers see complete files without taking the lock.
A failed write preserves the previous configuration; locks release on failure
or process exit.

Exit codes are 0 for success, 1 for operational or configuration errors, and 2
for CLI usage errors.

## Repository index and scheduled updates

The index is stored at `$XDG_DATA_HOME/cache22/index.sqlite3` (default:
`~/.local/share/cache22/index.sqlite3`). Keep this database on local storage.
It covers all archive roots and remains browsable when drives are disconnected.

The local commit date is the committer timestamp at local HEAD. No remote commit
date is collected. Remote status compares the last observed remote refs and HEAD
with the local mirror: `unknown`, `not_fetched`, `current`, or `updates_available`.
Force pushes, deleted refs, tags, and non-default branches all participate. Status
is an observation, not a live guarantee.

Inventory's `last_checked_at`, `last_fetched_at`, and `last_converted_at` are
completion times of successful whole job attempts, derived from history. Running,
failed, and interrupted attempts do not advance them, even if local Git work
finished before the failure. Unknown timestamps are null. Only check jobs advance
`last_checked_at`: fetch can refresh remote refs without advancing that timestamp.
A transport failure in fetch's optional final remote probe preserves the previous
remote snapshot and does not fail the fetch or create a separate check diagnostic.
Detailed attempt outcomes are available through `jobs` and the manager;
per-kind outcome/error fields are no longer exposed by inventory.

`add` registers only; `fetch` runs immediately, while `queue` submits work.
Schedules are disabled until explicitly enabled. A successful scheduled check
fetches only if needed. Structural errors block a schedule until corrected by a
successful manual operation or re-enabled. Transport failures retry after 1, 5,
30, and 120 minutes; unavailable roots and busy repositories defer for one minute.

Check/fetch accept multiple exact keys or URLs, or explicit `--all`. Default
operation timeouts are 120 seconds for checks and 7200 seconds for fetching;
use `--timeout` on check/fetch, or `--timeout-check`/`--timeout-fetch` on the worker.
JSON output uses UTC ISO 8601 timestamps; unknown values are null. List pagination
uses `--limit` and `--offset` (default limit 100).

Cleanup also maintains the index. Existing mirrors are discovered through their
next operation or `audit --fix`. Audit without `--adopt` never adopts unowned storage; audit never
deletes archives, and cannot recover old scheduling or fetch/check timestamps from
disk.


## Job diagnostics and retention

Errors are stored only on job attempts. Job inspection exposes a derived
`diagnostic` separately from the current attempt and progress, with no duplicate
job-level `error` or `error_category` fields.

`jobs --state failed` shows the latest failed or interrupted completed attempt per
problem job, including retries. Its diagnostic stays visible while another attempt
runs; succeeded and cancelled jobs are excluded. A separate successful job does
not hide an older failed job. Inventory's `has_error` follows the same rule;
`last_error`, `last_error_kind`, `last_error_category`, and `last_error_at` describe
the most recent such problem, ordered by completion time and then attempt ID.
The error kind belongs to the completed attempt, even if its job has since been
promoted from check to fetch. These fields are null when no problem job remains.

Terminal history is retained for 30 days, except the latest successful job per
repository and kind. Those jobs and their attempts remain until a newer success
replaces them; this preserves last-success timestamps for inactive repositories.
Pending and running jobs do not expire through history cleanup. Old failed jobs
still expire after 30 days, removing their diagnostics from inventory.

See [job inspection](use.md#job-inspection) for filters, pagination, and output.

## Browser and index lifecycle

The web server and worker run independently. Closing a browser or stopping the
web server does not stop a worker. Use SSH port forwarding for remote access.

Full index data can be inspected through Datasette; generic writes are disabled.
Only Cache22's forms/API perform mutations through shared services. Keep the
inventory ID column visible for live row updates and selection.

The index uses schema version 4; other versions are rejected without migration.
Before using an older index, stop all Cache22 processes, back it up, and discard that index
and its SQLite `-wal`/`-shm` sidecars. A command that modifies inventory, or
web/worker startup, then initializes the new schema.
This discards schedules, queued work, registrations, and history, but never archive
files. With archive roots connected, `cache22 audit --fix` can rediscover managed
mirrors and bundles. Re-register repositories without discoverable archives with
`cache22 add URL` and restore desired schedules. There is no migration and no
automatic reset during normal startup.

`project_name` is derived in the inventory view from the final component of
`display_path`, preserving the first registration's spelling. It is available for
sorting, filtering, and search but cannot be written independently. The retained
`archive_file` identifies the selected bundle generation, so inventory can return
archive paths even when storage is disconnected.

Index initialization is explicit and transactional. Concurrent initializers produce
one schema; opening an existing supported index takes no schema write transaction.
Services receive the entry point's index, including cleanup across archive roots.
`list`, `show`, `jobs`, and audit without repair open an existing index read-only:
they never initialize schema or recover expired claims, and a missing index remains
missing. Ordinary writable opens also require an existing initialized database.
Explicit initialization can finish an empty version-zero database left by an
interrupted initializer; it rejects nonempty unversioned and corrupt databases.
