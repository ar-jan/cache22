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
cache22 config archive add /absolute/path/to/archive

# Import a repository (remote paths are lowercased by default)
cache22 import repo https://github.com/ar-jan/cache22.git
```

See [docs/use.md](docs/use.md) for further documentation.

## Browser manager

```sh
cache22 manager run
# Open http://127.0.0.1:8001/
```

The Datasette manager browses the index, registers repositories, submits bulk
checks/fetches, changes schedules, and monitors queue progress. The launcher owns
one web process and one continuous worker; closing the browser does not stop work.
Use `cache22 manager run --web-only` with an independently managed
`cache22 worker run --continuous`. All index data is available for inspection;
changes go through Cache22 services. Assets are bundled locally.

This greenfield schema replaces the earlier index layout without a version bump
or migration. Before using an index created before the manager, stop Cache22
processes and discard that index and its `-wal`/`-shm` sidecars. A fresh index is
created on the next command. This loses registrations, schedules, queued work,
and history, but leaves archive files intact. `cache22 repo audit --fix` can
rediscover managed mirrors. Normal startup never resets an index automatically.

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
