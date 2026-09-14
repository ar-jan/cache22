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
