# Usage

## Archiving

```sh
# Configure where archives are stored
cache22 config archive add /absolute/path/to/archive

# Import a repository (remote paths are lowercased by default)
cache22 import repo https://github.com/ar-jan/cache22.git
```

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

If an import is interrupted and leaves partial state behind, clean it up with:

```sh
# Clean one repository by URL
cache22 import clean repo https://github.com/ar-jan/cache22.git

# Clean all configured archive directories
cache22 import clean all
```

## Details

For `https://Git.Example.ORG/Team/Project.git`, files are stored under `ARCHIVE/git.example.org/team/project/.cache22/`, including `project.git`, the `.clone-complete` marker, and the persistent `.lock` file.
Optional Fossil output and staging data also live there.
The `.cache22` namespace or repository name is reserved.
Previous layouts are not migrated or discovered.

Imports and cleanup fail immediately with a repository-busy error when another Cache22 command holds its lock.
Cleanup keeps completed archives and lock files.
`clean all` also visits nested repositories, skips directory symlinks, and stops on a busy repository without rolling back earlier cleanup.
Targeted operations reject symlinked storage paths.
Missing archive roots are errors and must be restored before importing or cleaning.

Configuration updates use atomic replacement so a failed write preserves the previous configuration.
