# cache22

## Python virtual environment

```sh
# Activate the venv:
source .venv/bin/activate
```

Always activate the existing virtual environment before running Python tasks like:

```sh
ruff format
ruff check
pyright
pytest
```

### Dependencies

```sh
# To add runtime Python dependencies:
uv add packagename
# To add development dependencies:
uv add --group dev packagename
# To install/update the venv:
uv sync
```

## Development

This is a greenfield project. Do not preserve any backwards compatibility.

When completing a task, always run the following commands and resolve any issues:

```sh
ruff format
ruff check --fix
pyright
pytest
```
