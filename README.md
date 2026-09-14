# cache22

Cache22 currently supports Linux and macOS on local POSIX filesystems.

## Installation

```sh
uv sync
source .venv/bin/activate
```

## Getting Started

```sh
# Configure where archives are stored
cache22 config archive add /absolute/path/to/archive

# Optional: switch the default archival format from git to fossil
cache22 config archive-type set fossil

# Import a repository
cache22 import repo https://github.com/ar-jan/cache22.git
```

By default, imports are treated as Git repositories and stored as Git mirror clones. If you switch the archive type to `fossil`, cache22 keeps the Git mirror and also creates a Fossil archive alongside it.

If an import is interrupted and leaves partial state behind, clean it up with:

```sh
# Clean one repository by URL
cache22 import clean repo https://github.com/ar-jan/cache22.git

# Clean all configured archive directories
cache22 import clean all
```

## Archive storage and recovery

For `https://Git.Example.ORG/Team/Project.git`, files are stored under
`ARCHIVE/git.example.org/team/project/.cache22/`, including `project.git`, the
`.clone-complete` marker, and the persistent `.lock` file. Optional Fossil output
and staging data also live there. The `.cache22` namespace or repository name
is reserved. Previous layouts are not migrated or discovered.

Imports and cleanup fail immediately with a repository-busy error when another
Cache22 command holds its lock. Cleanup keeps completed archives and lock files.
`clean all` also visits nested repositories, skips directory symlinks, and stops
on a busy repository without rolling back earlier cleanup. Targeted operations
reject symlinked storage paths. Missing archive roots are errors and must be
restored before importing or cleaning.

Empty Git repositories can be converted to Fossil. Configuration updates use
atomic replacement so a failed write preserves the previous configuration.

## Development

```sh
# Create .venv and install Python dependencies
uv sync --group dev
# Upgrade Python dependencies
uv lock --upgrade
# Install NPM dependencies for utils/editor
npm install
# Upgrade NPM dependencies
npm update
# Install git hooks for this repo
uv run --group dev pre-commit install
# Check yaml:
uv run --group dev yamllint docs/related-works.yaml
# Or run all pre-commit hooks:
uv run --group dev pre-commit run --all-files
```

## Related Works Editor

```sh
# Run Vite dev server, then open:
# http://127.0.0.1:5173/utils/editor/
npm run editor:dev
```

If the editor shows `File Access: Not supported in this browser` in Brave, enable `brave://flags/#file-system-access-api` and relaunch the browser.

Use `Save In Place` to write changes back to disk. On first save it will ask you to pick the target YAML file (choose `docs/related-works.yaml`), then it will reuse that file handle for later saves.
The selected file handle is persisted in browser storage; if permission is still granted, the editor auto-loads that same local file on next page load.
