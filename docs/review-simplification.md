# Remaining architecture and simplification issues

Reviewed against `3c9ce9a` on 2026-09-23. This document evaluates remaining
problems and design choices in the current implementation. Recommendations are
not commitments to implement every possible simplification. Schema changes follow
the project's greenfield policy: reject unsupported versions rather than add
compatibility migrations or silently reset data.

## Recommended order

1. **Completed:** separate index initialization from ordinary access and remove
   repeated schema introspection.
2. **Completed:** make attempts the sole source of job diagnostics and derive
   `project_name`; combine these schema edits in version 4.
3. **Completed:** establish a shared inventory query model and replace Datasette
   with a focused Cache22 web application.
4. Improve domain errors and operation results, extract pure diagnostics, and
   centralize observation-state interpretation.

Storage defaults and on-disk metadata deserve separate decisions. Neither should
block the index or web work. Naming and repository organization are lower priority.

## 1. Index initialization is explicit — completed

`Index.initialize` owns transactional schema creation at CLI/web entry points;
the worker CLI initializes once before starting its worker lifecycle. Ordinary
`Index` construction validates an existing database using SQLite's existing-file
modes, without a schema write transaction. Initialization of a supported index
also avoids schema writes and journal-mode changes.

Services require a supplied index, including cleanup traversal. `Index.update_in`
validates fields against an explicit allowed-field set and retains claim validation
inside the inventory-write transaction. This step retained schema version 3;
the diagnostic and project-name cleanup below advances it to version 4.

`list`, `show`, `jobs` (including `--db`), and audit without repair open read-only.
Missing indexes fail with creation/rebuild guidance without creating directories
or databases. Repairing audit, other inventory mutations, and web/worker startup
initialize explicitly. Help and configuration commands do not access the index.

Lifecycle tests cover concurrent initialization, opening alongside an active writer,
initialization rollback/retry, missing indexes, unsupported and unrecognized
content, custom-index cleanup, and inspection without claim recovery. Existing
fencing and rollback checks remain applicable.

## 2. Attempts own job diagnostics — completed

Schema version 4 removes `jobs.error_category` and `jobs.error`.
[job_queue.py](../src/cache22/job_queue.py) persists errors only on attempts,
eliminating the stale job-level errors previously left behind by interruption.
[manager_service.py](../src/cache22/manager_service.py) exposes the attempt-derived
`diagnostic` separately from current attempt/progress in CLI and browser responses,
without top-level error aliases.

`job_errors` retains its existing semantics: pending, running, and failed jobs may
have a diagnostic from their latest completed attempt. Running retries keep it
visible; success and cancellation remove that job from the problem view. A separate
successful job does not hide older failures. Promoting check to fetch retains the
original attempt kind. Retry, scheduling, and retention policies are unchanged.

Existing lifecycle and inspection tests cover failure, retry, interruption,
promotion, cancellation, success, and history expiry. Inspection assertions also
check agreement between CLI JSON, browser queue responses, and inventory.

## 3. Project names are derived; display paths and bundle filenames remain — completed

The same version-4 schema change removes stored `repositories.project_name`.
[index.py](../src/cache22/index.py) derives it from the final component of
`display_path` in the SQL inventory view using SQLite built-ins. Sorting, search,
filtering, and presentation continue to use `project_name`; independent writes
to it are rejected.

`display_path` preserves the first display spelling, which normalized keys and
effective clone URLs can lose. `archive_file` retains the selected bundle generation
filename, which cannot be reconstructed from the key. Neither is removed, and
inventory needs no archive access, including when drives are disconnected.

Tests cover mixed-case and Unicode names, duplicate registrations, SQL sorting,
manager filtering/selection, and disconnected inventory reads. Version 3 is rejected
without migration or automatic reset. The [README](../README.md) and
[guarantees](guarantees.md#browser-and-index-lifecycle) describe backing up and
rebuilding the index, including loss of registrations, schedules, and job history.

## 4. Shared inventory queries and focused web application — completed

`inventory_service` owns typed filters, validated sorting, parameterized predicates,
page snapshots, disjunctive facet counts, exports, and bounded ID selection. CLI
listing exposes the same operational filters, including literal name/key search,
repeatable categorical options, and positive/negative boolean flags. Page defaults
are 100 rows, capped at 500; ordering uses repository ID to break ties.

The Starlette/Jinja application replaces Datasette, its plugin loader/entry point,
private filter parser, and generated-page scraping. Cache22 owns templates, assets,
and a dedicated inventory refresh partial. The compact table links to repository
details, attempts, and existing actions. Tab-local captured IDs survive navigation,
filter changes, and refreshes; the maximum selection/submission remains 10,000.
Exports include every match in sort order and explicitly omit `source_url`.

Registration, commands, schedules, conversion, queue monitoring, and worker progress
reuse existing services. SQLite/configuration work runs outside the event loop.
Loopback binding, Host/Origin checks, mutation method/Fetch Metadata boundaries,
local assets, and independent worker lifecycle remain. SQL/table browsing, database
downloads, and old route/parameter aliases are absent. Schema version remains 4.

Focused tests cover filter/list/export/selection agreement, facet counts, concurrent
snapshot consistency, disconnected roots, selection boundaries, malformed requests,
and HTTP mutation boundaries. Browser acceptance exercises captured selection,
pagination and empty pages, focus, refresh failures/recovery, registration, commands,
progress, attempt details, and no-worker messaging.

A reproducible smoke benchmark is available as
`python utils/benchmark_inventory.py` in the development environment. On 2026-09-25,
its temporary fixture had 10,000 repositories across ten hosts, unavailable archive
storage, 10,000 successful fetch attempts, 1,428 failed check attempts, 2,000 pending
checks, and 5,000 schedules. Each figure is the median of five local runs; HTTP
measurements use the in-process ASGI test client, excluding network/browser costs.

| Operation | Median ms |
| --- | ---: |
| First page, total and three facets | 48.3 |
| Deep page (offset 9,900), total and facets | 56.2 |
| Host/queued/scheduled filter and facets | 17.4 |
| Successful-fetch date sort and facets | 59.3 |
| Diagnostic filter and facets | 41.5 |
| Capture all 10,000 IDs | 3.8 |
| Search and capture matching IDs | 7.8 |
| Materialize 10,000 export records | 170.4 |
| HTTP inventory HTML | 50.2 |
| HTTP inventory refresh fragment | 52.2 |
| HTTP JSON export | 328.1 |
| HTTP CSV export | 367.2 |

These are smoke measurements, not latency guarantees. The earlier baseline used
fewer diagnostics and timed individual reads rather than complete page snapshots;
it is not a controlled before/after speed comparison.

## 5. Error classification and result presentation cross layer boundaries

**Evidence.** [repo_service.py](../src/cache22/repo_service.py) classifies failures
with `isinstance` checks and recursive cause inspection in `category_for`.
Adapters and services catch overlapping exception tuples. `ImportResult` and
`StorageFetchResult` carry formatted `INFO:` messages generated below the CLI.

**Recommendation — introduce domain errors and structured outcomes.** Define a
small shared error model for expected domain failures, with deliberate categories
for transport, unavailable storage, busy storage, and structural problems. Translate
OS/Git failures at the boundary that has enough context to classify them. Keep
retry/defer decisions in the scheduler. Preserve distinct claim-loss and interruption
control paths instead of turning them into ordinary retryable failures.

Return structured fetch/adoption facts and let CLI/web adapters phrase the output.
This makes category behavior and presentation easier to inspect. Avoid a class per
message and a catch-all wrapper that hides programming errors. Centralize
credential sanitization before diagnostics are persisted or returned.

**Acceptance.** Existing retry/defer schedules, structural blocking, interruption,
and lost-claim behavior remain correct. Credential-bearing diagnostics stay
sanitized. Text rendering and JSON output describe the same outcomes without
embedding CLI prose in storage results.

## 6. Operation code still combines pure diagnostics and execution context

**Evidence.** [operation.py](../src/cache22/operation.py) contains sanitization,
failure-message extraction, Git-progress parsing/publication, subprocess handling,
claim/deadline fencing, and inherited-lock context variables.
[job_operation.py](../src/cache22/job_operation.py) owns heartbeat and job lifecycle.

**Recommendation — make a small extraction first.** Move sanitization,
failure-message extraction, and a pure Git-progress parser into a diagnostics
module. Let the parser return a structured snapshot; keep publishing progress and
consulting the current operation in the execution layer. Preserve bounded output
and sanitization before truncation.

Defer wholesale replacement of context variables with explicit arguments.
`current_operation` fences inventory writes as well as subprocesses, and inherited
lock descriptors keep locks alive when a parent dies. Threading those dependencies
through every storage/Git path would be a separate high-risk change without a
currently demonstrated correctness benefit. Document these dependencies at the
service and storage boundaries instead.

**Acceptance.** Malformed progress remains harmless, credentials stay redacted,
claim loss prevents writes, cancellation kills Git before locks are released,
and child processes retain required lock descriptors. Reuse the existing
operation and locking tests; add only focused parser coverage where needed.

## 7. Interpretation of prior observation state is repeated

**Evidence.** [repo_service.py](../src/cache22/repo_service.py) and
[repo_audit.py](../src/cache22/repo_audit.py) repeatedly derive `previously_ready`
from `local_state` before calling storage observation helpers. Inventory also
uses `reconciliation_required` to decide whether remote status is trustworthy.

**Recommendation — centralize interpretation, retain the two dimensions.** Put
prior-state interpretation in one service-layer helper. Do not make the storage
facade depend on inventory records or scheduling policy merely to remove repeated
expressions.

Keep reconciliation/freshness independent of the last-known local state. A single
`stale` state would discard whether the repository was previously ready, missing,
or never fetched, unless that information were stored elsewhere. This is a small
readability improvement, not justification for a schema redesign.

**Acceptance.** Interrupted work makes observations untrusted without losing the
absent-versus-missing distinction; audit and normal operations interpret prior
state consistently.

## 8. Further progress and schedule schema consolidation has uncertain payoff

**Evidence.** Progress lives in a one-to-one `attempt_progress` table and requires
an upsert plus joins. Schedules store `enabled` and `blocked` independently in
[index.py](../src/cache22/index.py) and
[scheduler.py](../src/cache22/scheduler.py).

**Recommendation — defer further consolidation.** Merge progress into attempts
only if a measured query or maintenance benefit outweighs widening attempt records
and changing progress writes. Do not bundle it with diagnostic cleanup solely to
remove a table.

Schedule enablement represents user policy; blocking represents an operational
condition. A single on/off/blocked enum needs an explicit decision about retaining
blocked state while disabled. The current representation is understandable, and
job origin remains necessary for priority and cancellation policy. Keep it until
there is a concrete problem to solve. Likewise, retain worker availability records:
a live job lease cannot describe an available idle worker.

**Revisit when** query profiling or a specific state-transition defect demonstrates
a benefit. Any later change must preserve fenced progress, disable/retry races,
manual-job survival, and availability reporting.

## 9. Storage requirements and the mirror-first default are unresolved

**Evidence.** [requirements.md](requirements.md) requires avoiding many small files
and says to avoid standard Git repositories. New archives currently start as
mirrors; bundles require queued conversion. Bundles reduce persistent file count,
but updates restore temporary Git storage and rewrite and independently verify
the archive, as described in [guarantees.md](guarantees.md).

**Recommendation — settle the intended workflow before adding format settings.**
Retain the current default while evaluating whether bundles are the normal archival
end state or an optional storage tradeoff. Measure representative repeated updates,
peak temporary space, and restore/verification cost. Then align the requirements
with the decision. Do not simply weaken the requirement because implementation
currently differs.

A future bundle-first option must distinguish desired format for a new repository
from its currently published format, handle failed initial publication, and define
empty-repository behavior. Per-root/global defaults are premature until that
workflow is chosen. No setting should silently convert existing archives.

**Decision criteria.** The chosen workflow must satisfy the intended file-count,
original-Git-object, update, and recovery requirements, with explicit resource
tradeoffs and no ambiguity about when a valid archive first exists.

## 10. On-disk metadata consolidation needs a recovery design

**Evidence.** [archive_storage.py](../src/cache22/archive_storage.py),
[archive_layout.py](../src/cache22/archive_layout.py), and
[git_bundle.py](../src/cache22/git_bundle.py) use an ownership lock signature,
`source.json`, mirror completion state, and a bundle manifest. These files carry
related information but have different publication and recovery roles.

**Recommendation — defer a single versioned manifest until storage policy is
settled.** Consolidation may reduce metadata handling, but the design must cover
source binding before a clone completes, explicit adoption, incomplete mirrors,
selected bundle generation, and interrupted publication. Keep the ownership lock
independent of metadata replaced by rename; lock inode identity is significant.

Require a failure-point analysis before changing the layout. Preserve the durable
archive publication → fenced inventory publication → old-source retirement
sequence, and never retire the previous archive when inventory publication fails.
Do not combine this work with the UI rewrite or routine index cleanup.

**Acceptance for any later implementation.** Failed/interrupted writes preserve
recoverable data; adoption cannot claim unrelated storage; schema/index disagreement
is recoverable offline; unknown metadata versions fail explicitly; symlink rejection,
locking, and bundle verification remain intact. Use the existing publication and
archive-safety tests to identify meaningful additional cases.

## 11. Smaller naming and repository-organization work

**Evidence.** [import_service.py](../src/cache22/import_service.py) still names
its fetch entry point `import_repository` and overlaps registration/root-selection
logic with `registration_target`. URL selector normalization belongs to `Index`,
while URL parsing and effective remote casing belong to `repository_ref`.
The root [package.json](../package.json) serves the unrelated
[editor experiment](../utils/editor/README.md).

**Recommendation — handle opportunistically.** Use fetch-oriented names and
consolidate registration decisions when those paths are next changed. Preserve
the distinction between fetch's import/adoption binding rules and register-only
validation; sharing code must not erase it. Keep selector normalization in one
shared resolver rather than duplicating it in CLI and web adapters. Moving it out
of `Index` is worthwhile only alongside a clearer service interface.

If the editor continues growing, move it and its package tooling under an explicit
experiment boundary. Do not create a separate repository merely to shorten the
main tree.

Keep the current TOML configuration and atomic configuration writes. Keep effective
URL normalization: storing raw input verbatim would change default remote-path
casing. Keep internal `RepositoryStorage`/path helpers; their existence alone is
not an outstanding facade problem.

**Acceptance.** Naming/organization changes preserve source binding, root selection,
configuration concurrency, and command behavior. Remove abandoned internal names
rather than adding aliases, and update editor commands if its tooling moves.
