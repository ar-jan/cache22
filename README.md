# cache22

This is a playground for experimenting with curating and archiving source code on local filesystems.
It's also meant as an exercise for trying out AI-assisted development workflows.
Cache22 currently works on local POSIX filesystems (Linux and macOS).

## Installation

```sh
uv sync
source .venv/bin/activate
```

## Getting Started

```sh
# Configure where archives are stored
cache22 config root add /absolute/path/to/archive

# Fetch a repository (remote paths are lowercased by default)
cache22 fetch https://github.com/ar-jan/cache22.git
```

See [docs/use.md](docs/use.md) for further documentation.

## Browser manager

Run these in separate terminals:

```sh
cache22 web
# Open http://127.0.0.1:8001/
```

```sh
cache22 worker
```

The Datasette manager browses the index, registers repositories, submits bulk
checks/fetches, changes schedules, and monitors progress. Web and worker run
independently; stopping either does not stop the other. Jobs wait when no worker
is available. Use `cache22 worker --once` for timer-driven operation. All index
data is available for inspection; changes go through Cache22 services. Assets
are bundled locally. See [usage](docs/use.md) for commands and service examples.

The index uses schema version 4. Other versions are rejected without migration
or automatic reset. To replace an older index, stop all Cache22 processes, back
up the index, and remove it and its `-wal`/`-shm` sidecars. This loses registrations,
schedules, queued work, and history, but leaves archive files intact. A fresh index
is created by the next command that modifies inventory, or by web/worker startup.
With archive roots connected, `cache22 audit --fix` can rediscover managed mirrors
and bundles. Re-register repositories without discoverable archives using
`cache22 add URL`, then restore any desired schedules.

`list`, `show`, `jobs`, and audit without repair require an existing index;
they fail without creating files when it is missing.

Inventory success timestamps and error summaries come from job attempts.
Job records do not store duplicate errors. Inventory project names are derived
from the retained display path, preserving the first registered spelling.
Only a successful check job advances the last-check timestamp; fetches and
conversions have their own success timestamps. Terminal job history expires
after 30 days, except the latest successful job per repository and kind, which
is retained to preserve those timestamps. See [usage](docs/use.md) for status
and recovery details.

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
# Or run all pre-commit hooks:
uv run --group dev pre-commit run --all-files
```

# Utils

Other related experiments:

[Basic YAML editor](utils/editor/README.md) for storing relevant projects.
