# Usage

## Archiving

```sh
# Configure where archives are stored
cache22 config archive add /absolute/path/to/archive

# Import a repository (remote paths are lowercased by default)
cache22 import repo https://github.com/ar-jan/cache22.git
```

Repeating an import fetches updates into the existing mirror.
Updates include new refs, forced changes, and pruning branches and tags deleted
upstream. The mirror's HEAD follows the advertised default branch, including
renames, or the advertised detached commit. A failed fetch keeps the initialized
mirror for retry. Ref updates are atomic; updating HEAD is a subsequent step.
Before publishing HEAD, Cache22 checks that its remote target and object ID stayed
the same across the fetch and that the fetched target matches. If this check or
HEAD publication fails, the command reports an incomplete update and retains the
fetched refs without publishing HEAD. Retry the import to complete it; Cache22
does not retry automatically.

Every existing-mirror update rechecks the storage layout before running Git.
Symlinks, redirected storage, and shallow or partial-clone state are rejected
without modifying the mirror. These checks inspect filesystem entries; full Git
object verification runs only during explicit adoption.

### Standalone Git bundles

Mirrors remain the default. To store an existing managed repository as a single
self-contained Git bundle, queue an offline conversion:

```sh
cache22 repo convert github.com/ar-jan/cache22 --to bundle
cache22 worker run --once
```

The command returns a job ID; it does not wait for conversion. The browser manager
also offers **Convert to bundle** on repository and bulk actions. Conversion runs
after earlier work for that repository, so it can follow an initial queued fetch.
An already bundled repository is verified without rewriting its bundle.

After conversion, `repo check`, `repo fetch`, direct imports, and scheduled updates
continue to work. Checks compare bundle refs and saved HEAD metadata with the
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
or `.cache22-bundle` staging; `import clean repo URL` cleans recognized state and
verifies the selected bundle before retiring old archives. `repo audit --fix`
rebuilds bundle inventory offline. Invalid active metadata preserves data for
inspection; restore a missing manifest rather than attempting adoption.

Empty mirrors cannot be converted. If an update becomes empty, it fails while
preserving the previous bundle. Only history reachable from current refs and HEAD
is retained; removed or force-pushed history can disappear on the next rewrite.
Conversion back to a mirror and external bundle adoption are not supported.

Conversion has its own diagnostics and does not advance fetch/check timestamps
or change the update schedule. The worker's `--convert-timeout` defaults to 7200
seconds. Queue and progress views show conversion alongside checks and fetches.
Synchronous operations report busy rather than overtake pending conversion.

### Adopting an existing mirror

If a mirror already exists at the expected path, for example
`ARCHIVE/github.com/karpathy/llm.c/llm.c.git`, initialize it with:

```sh
cache22 import repo https://github.com/karpathy/llm.c.git --adopt
```

Cache22 verifies the origin identity, bare mirror configuration, and full Git
object integrity before writing missing metadata and fetching updates. It never
reclones or deletes the supplied mirror on failure. Later imports need no flag.
Verification can take time for large mirrors.

An ordinary import encountering an eligible uninitialized directory offers:

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

Use `cache22 import repo https://host/Team/Repo --case-sensitive` to preserve remote path casing on a case-sensitive server.
Local identity stays lowercase.
Each local path binds to one source; conflicting casing is rejected before reuse or Git work.
Failed imports release that binding once cleanup leaves no archive or partial state.

### Clean-up

If an interrupted import leaves a complete Git mirror without Cache22 metadata,
try `--adopt` to verify and retain it. To discard incomplete import state instead:

```sh
# Clean one repository by URL
cache22 import clean repo https://github.com/ar-jan/cache22.git

# Clean all configured archive directories
cache22 import clean all
```

## Details

The entire configured archive directory is managed by Cache22.
For `https://Git.Example.ORG/Team/Project.git`, files are stored directly under `ARCHIVE/git.example.org/team/project/`, including `project.git`, the `.clone-complete` marker, and the persistent `.lock` file.
Repository directories are terminal containers. For example, importing both
`host/team/project` and `host/team/project/child` into the same archive is rejected
in either order, including simultaneous imports. Subgroup namespaces and sibling
repositories remain supported.
Previous layouts are unsupported and are not migrated.

Imports and cleanup report a repository-busy error on repository or namespace
reservation contention. Acquiring the brief archive-root lock can wait.
Verification reserves the candidate directory and its namespace ancestors using
inode locks. It creates no reservation files and does not hold the archive-wide
root lock during the integrity check. Sibling imports and targeted cleanup can
continue while verification runs; conflicting reservations fail immediately.
Reservations release on process exit, including interrupted adoption before any
Cache22 metadata has been published.
Cleanup keeps completed archives and lock files.
It only cleans recognized Cache22 storage; unowned mirrors are left untouched.
A completion marker must contain exactly `complete\n` for imports to reuse the
archive. A malformed marker causes an import error without changing the mirror.
Cleanup preserves a mirror beside such a marker, but can remove an orphan marker
when no mirror exists.
The retained lock keeps the path reserved as a repository even after its archive data is removed.
`clean all` visits subgroup namespaces, stops traversal at repository boundaries,
skips directory symlinks, and stops on a busy repository without rolling back earlier cleanup.
Targeted operations reject symlinked storage paths.
Missing archive roots are errors and must be restored before importing or cleaning.

Direct CLI imports use the first configured archive root; both cleanup commands
search all configured roots. An explicit root override is available through
the Python service, not import CLI options.

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

```sh
# Register without downloading; the first configured archive root is the default
cache22 repo add https://github.com/ar-jan/cache22.git

# Read inventory without scanning archives or contacting remotes
cache22 repo list
cache22 repo list --remote-status updates_available --json
cache22 repo show github.com/ar-jan/cache22

# Check refs, then fetch explicitly
cache22 repo check github.com/ar-jan/cache22
cache22 repo fetch github.com/ar-jan/cache22

# Queue one download, or enable recurring check-then-fetch updates
cache22 repo queue github.com/ar-jan/cache22
cache22 repo schedule github.com/ar-jan/cache22 --every 6h
cache22 worker run --once
cache22 repo jobs github.com/ar-jan/cache22 --json

# Disable automatic updates or cancel pending work independently
cache22 repo schedule github.com/ar-jan/cache22 --disable
cache22 repo unqueue github.com/ar-jan/cache22

# Discover existing managed mirrors and refresh local observations
cache22 repo audit
cache22 repo audit --fix
```

Run `cache22 worker run --continuous` for a persistent worker, or schedule
`cache22 worker run --once` with cron or a system timer, for example every minute.
Cache22 does not install a timer or service automatically. Run-once drains due
work and exits; retries wait for a later invocation. Continuous mode keeps waiting
for future schedules and retries.

The local commit date is the committer timestamp at local HEAD. No remote commit
date is collected. Remote status compares the last observed remote refs and HEAD
with the local mirror: `unknown`, `not_fetched`, `current`, or `updates_available`.
Force pushes, deleted refs, tags, and non-default branches all participate. Status
is an observation, not a live guarantee; check its timestamp and diagnostics.

`repo add --fetch` downloads immediately; `--queue` queues a one-off fetch.
Schedules are disabled until explicitly enabled. A successful scheduled check
fetches only if needed. Structural errors block a schedule until corrected by a
successful manual operation or re-enabled. Transport failures retry after 1, 5,
30, and 120 minutes; unavailable roots and busy repositories defer for one minute.

Check/fetch accept multiple exact keys or URLs, or explicit `--all`. Default
operation timeouts are 120 seconds for checks and 7200 seconds for fetching;
use `--timeout` on check/fetch, or `--check-timeout`/`--fetch-timeout` on the worker.
JSON output uses UTC ISO 8601 timestamps; unknown values are null. List pagination
uses `--limit` and `--offset` (default limit 100).

Direct imports and cleanup also maintain the index. Existing mirrors are discovered
through their next operation or `repo audit --fix`. Audit never adopts unowned
storage or deletes archives, and cannot recover old scheduling or fetch/check
timestamps from disk. Explicit `repo fetch SELECTOR --adopt` uses the same verified
adoption rules as `import repo --adopt`.


## Inspecting queue errors and status

```sh
cache22 manager errors
cache22 manager queue --section deferred
cache22 manager errors --db /path/to/index.sqlite3 --json
```

`manager errors` shows the latest failed or interrupted completed attempt per
problem job, including retries. Its diagnostic stays visible while another attempt
runs; succeeded and cancelled jobs are excluded. A separate successful job does
not hide an older failed job. Terminal history is retained for 30 days.

`manager queue` shows queue counts, worker availability, and job progress. Choose
`--section running|runnable|deferred|history` (default: `running`).

Both commands read the default index without requiring a running manager or
access to archives. `--db PATH` selects another existing database read-only;
missing databases are errors and are never created. Use `--json` for structured
output and `--limit N --offset N` for pagination (default limit 100, maximum 500).
Listing errors or unavailable workers still exits 0; inspection failures exit 1.

## Browser manager

Run `cache22 manager run` and open `http://127.0.0.1:8001/`. Use `--port` to choose
another port, or `--web-only` when running an independent continuous worker.
The manager supports inventory filters, explicit bulk selection, registration,
checks/fetches, schedules, and queue/progress monitoring. Closing a tab does not
stop jobs. Stopping the combined launcher stops its worker; an external worker
continues when a web-only manager stops. Use SSH port forwarding for remote use.

Full index data can be inspected through Datasette; generic writes are disabled.
Only Cache22's forms/API perform mutations through shared services. Keep the
inventory ID column visible for live row updates and selection.

The index uses schema version 2; older versions are rejected without migration.
Before using an older index, stop all Cache22 processes, back it up, and discard that index
and its SQLite `-wal`/`-shm` sidecars. Normal startup then creates the new schema.
This discards schedules, queued work, registrations, and history, but never archive
files. `cache22 repo audit --fix` can rediscover managed mirrors and bundles. There is no
migration and no automatic reset during normal startup.
