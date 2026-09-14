# Usage

## Archiving

```sh
# Configure where archives are stored
cache22 config archive add /absolute/path/to/archive

# Import a repository (remote paths are lowercased by default)
cache22 import repo https://github.com/ar-jan/cache22.git
```

In Git archive mode, repeating an import fetches updates into the existing mirror.
Updates include new refs, forced changes, and pruning branches and tags deleted
upstream. A failed fetch keeps the initialized mirror for retry.

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
Fossil mode does not support adoption and retains its existing reuse behavior.

### Case-sensitive sources

Use `cache22 import repo https://host/Team/Repo --case-sensitive` to preserve remote path casing on a case-sensitive server.
Local identity stays lowercase.
Each local path binds to one source; conflicting casing is rejected before reuse or Git work.
Failed imports release that binding once cleanup leaves no archive or partial state.

### Fossil

By default, imports are treated as Git repositories and stored as Git mirror clones.
If you switch the archive type to `fossil`, cache22 keeps the Git mirror and also creates a Fossil archive alongside it.

```sh
# Optional: switch the default archival format from git to fossil
cache22 config archive-type set fossil
```

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
Optional Fossil output and staging data also live there.
Repository directories are terminal containers. For example, importing both
`host/team/project` and `host/team/project/child` into the same archive is rejected
in either order, including simultaneous imports. Subgroup namespaces and sibling
repositories remain supported.
Previous layouts are unsupported and are not migrated.

Imports and cleanup fail immediately with a repository-busy error when another Cache22 command holds its lock.
Cleanup keeps completed archives and lock files.
The retained lock keeps the path reserved as a repository even after its archive data is removed.
`clean all` visits subgroup namespaces, stops traversal at repository boundaries,
skips directory symlinks, and stops on a busy repository without rolling back earlier cleanup.
Targeted operations reject symlinked storage paths.
Missing archive roots are errors and must be restored before importing or cleaning.

Configuration updates use atomic replacement so a failed write preserves the previous configuration.
