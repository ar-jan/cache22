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
